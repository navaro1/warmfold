import contextlib
import io
import json
import os
import sys
import time
import types

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts")
)

from warmfoldlib import config, events, ledger, log, state, transcript  # noqa: E402
import warmfoldlib  # noqa: E402

import pytest  # noqa: E402

T = 1789974000.0  # 2026-09-21T07:00:00Z


def iso(t):
    parts = time.gmtime(t)
    return "%04d-%02d-%02dT%02d:%02d:%02d.000Z" % (
        parts.tm_year, parts.tm_mon, parts.tm_mday,
        parts.tm_hour, parts.tm_min, parts.tm_sec,
    )


def user_line(ts):
    return {"type": "user", "timestamp": iso(ts)}


def asst_line(ts, model="claude-sonnet-5", inp=0, create=1000, read=399000,
              w1h=1000, w5m=0):
    return {
        "type": "assistant",
        "timestamp": iso(ts),
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": create,
                "cache_read_input_tokens": read,
                "output_tokens": 100,
                "cache_creation": {
                    "ephemeral_1h_input_tokens": w1h,
                    "ephemeral_5m_input_tokens": w5m,
                },
            },
        },
    }


def write_transcript(path, lines):
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(json.dumps(line) + "\n")


def append_line(path, line):
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("WARMFOLD_", "CLAUDE_PLUGIN_OPTION_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    # TTL detection reads auth and TTL signals from the environment; scrub
    # them so watch and status tests stay deterministic on any machine
    for name in (
        "FORCE_PROMPT_CACHING_5M",
        "CLAUDE_CODE_PROMPT_CACHE_TTL",
        "ENABLE_PROMPT_CACHING_1H",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def clear_transcript_cache():
    transcript._CACHE.clear()
    yield
    transcript._CACHE.clear()


@pytest.fixture()
def env(tmp_path):
    """Standard test environment: a data dir, a transcript, a config."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    tpath = tmp_path / "t.jsonl"
    # user at T-100, assistant at T-90: a 400k-token warm 1h session
    write_transcript(str(tpath), [user_line(T - 100), asst_line(T - 90)])
    cfg = dict(config.DEFAULTS)
    cfg["data_dir"] = str(data_dir)
    # channel detection is WP2 code; tests that need a channel install a
    # stub, so the default env disables detection for determinism
    cfg["force_channel"] = "none"
    payload = {
        "session_id": "sess-test",
        "transcript_path": str(tpath),
        "cwd": str(tmp_path),
    }
    return types.SimpleNamespace(
        tmp_path=tmp_path,
        data_dir=str(data_dir),
        tpath=str(tpath),
        cfg=cfg,
        payload=payload,
    )


def install_clock(monkeypatch, start=T):
    clock = types.SimpleNamespace(t=start)
    monkeypatch.setattr(events, "now", lambda: clock.t)
    return clock


def run_watch(monkeypatch, clock, cfg, payload, step_actions=None):
    """Drive events.watch with a fake sleep that advances the clock.

    Each entry of step_actions runs once, right after the matching sleep.
    """
    counter = {"n": 0}

    def fake_sleep(seconds):
        clock.t += seconds
        if step_actions and counter["n"] < len(step_actions):
            step_actions[counter["n"]]()
            counter["n"] += 1

    monkeypatch.setattr(events, "sleep", fake_sleep)
    err = io.StringIO()
    monkeypatch.setattr(sys, "stderr", err)
    out, code = events.watch(payload, cfg)
    return out, code, err.getvalue()


def load_state(env):
    return state.load(env.data_dir, "sess-test")


def save_state(env, **fields):
    st = dict(state.DEFAULTS)
    st.update(fields)
    state.save(env.data_dir, "sess-test", st)


def log_text(env):
    path = log.log_path(env.data_dir)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# watch


def test_watch_arms_and_exits_on_superseded_token(monkeypatch, env):
    clock = install_clock(monkeypatch)
    my_token = clock.t

    def supersede():
        st = load_state(env)
        st["armed_token"] = my_token + 1
        state.save(env.data_dir, "sess-test", st)

    out, code, err = run_watch(
        monkeypatch, clock, env.cfg, env.payload, [supersede]
    )
    assert code == 0
    st = load_state(env)
    assert st["armed_token"] == my_token + 1
    assert "superseded" in log_text(env)


def test_watch_exits_on_user_activity(monkeypatch, env):
    clock = install_clock(monkeypatch)

    def user_typed():
        st = load_state(env)
        st["activity_at"] = clock.t + 5
        state.save(env.data_dir, "sess-test", st)

    out, code, err = run_watch(
        monkeypatch, clock, env.cfg, env.payload, [user_typed]
    )
    assert code == 0
    assert "user activity" in log_text(env)


def test_watch_exits_when_parent_dies(monkeypatch, env):
    clock = install_clock(monkeypatch)
    calls = {"n": 0}

    def fake_ppid():
        calls["n"] += 1
        return 4242 if calls["n"] == 1 else 9999

    monkeypatch.setattr(os, "getppid", fake_ppid)
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert "parent died" in log_text(env)
    assert calls["n"] >= 2


def test_watch_exits_on_newer_turn(monkeypatch, env):
    clock = install_clock(monkeypatch)

    def newer_turn():
        append_line(env.tpath, asst_line(clock.t + 60))

    out, code, err = run_watch(
        monkeypatch, clock, env.cfg, env.payload, [newer_turn]
    )
    assert code == 0
    assert "newer turn" in log_text(env)


def test_watch_cold_exit_sets_phase_cold(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 60
    env.cfg["safety_margin_minutes"] = 0
    # expires = (T-100) + 60 = T-40, already past at arm time
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    st = load_state(env)
    assert st["phase"] == "cold"
    assert st["expires_at"] == (T - 100) + 60.0


def test_watch_due_no_channel_requests_handoff(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg.update(
        {
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
            "mode": "auto",
            "force_channel": "none",
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert "handoff summary" in err
    assert "Goal" in err
    st = load_state(env)
    assert st["phase"] == "handoff_pending"
    assert st["channel"] == "none"


def test_watch_handoff_reply_saves_and_does_not_arm(monkeypatch, env):
    clock = install_clock(monkeypatch)
    save_state(env, phase="handoff_pending", armed_token=555.0, cwd=str(env.tmp_path))
    payload = dict(env.payload)
    payload["last_assistant_message"] = "# Goal\nFinish the refactor.\n"
    out, code, err = run_watch(monkeypatch, clock, env.cfg, payload)
    assert code == 0
    st = load_state(env)
    assert st["phase"] == "handoff_done"
    assert st["armed_token"] == 555.0  # not re-armed
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    assert latest is not None
    assert latest["session_id"] == "sess-test"
    assert latest["consumed_at"] is None
    with open(latest["path"], encoding="utf-8") as handle:
        assert "# Goal" in handle.read()
    assert "handoff saved" in log_text(env)


def test_watch_handoff_save_failure_keeps_pending(monkeypatch, env):
    clock = install_clock(monkeypatch)
    save_state(env, phase="handoff_pending", armed_token=555.0, cwd=str(env.tmp_path))
    payload = dict(env.payload)
    payload["last_assistant_message"] = "# Goal\nFinish it.\n"
    real_write = state.write_json_atomic

    def fail_latest(path, obj):
        if path.endswith("latest.json"):
            raise OSError("disk full")
        real_write(path, obj)

    monkeypatch.setattr(state, "write_json_atomic", fail_latest)
    out, code, err = run_watch(monkeypatch, clock, env.cfg, payload)
    assert code == 0
    assert err == ""
    # the phase stays pending so the next Stop can retry the save
    assert load_state(env)["phase"] == "handoff_pending"
    assert "handoff save failed" in log_text(env)
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    assert latest is None


def test_watch_keepalive_counts_wakes(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg.update(
        {
            "mode": "keepalive",
            "keepalive_hours": 1.0,
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert "keep-alive" in err
    assert load_state(env)["phase"] == "keepalive_pending"

    # Claude replied, Stop fires again: the wake is counted and it re-arms.
    clock.t += 60.0

    def user_replied_ok():
        st = load_state(env)
        st["activity_at"] = clock.t + 5
        state.save(env.data_dir, "sess-test", st)

    out, code, err = run_watch(
        monkeypatch, clock, env.cfg, env.payload, [user_replied_ok]
    )
    assert code == 0
    st = load_state(env)
    assert st["wake_count"] == 1
    assert st["phase"] == "idle"
    assert st["armed_token"] == pytest.approx(clock.t - 15.0, abs=1)


def test_watch_keepalive_budget_spent_falls_back_to_handoff(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg.update(
        {
            "mode": "keepalive",
            "keepalive_hours": 0.0,
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert load_state(env)["phase"] == "handoff_pending"


def test_watch_ttl_5m_warn_does_nothing(monkeypatch, env):
    clock = install_clock(monkeypatch)
    write_transcript(
        env.tpath, [user_line(T - 100), asst_line(T - 90, w1h=0, w5m=50000)]
    )
    env.cfg.update(
        {
            "mode": "compact",
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "ttl_5m_policy": "warn",
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert load_state(env)["phase"] == "idle"


def test_watch_ttl_5m_off_does_nothing(monkeypatch, env):
    clock = install_clock(monkeypatch)
    write_transcript(
        env.tpath, [user_line(T - 100), asst_line(T - 90, w1h=0, w5m=50000)]
    )
    env.cfg.update(
        {
            "mode": "compact",
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "ttl_5m_policy": "off",
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert load_state(env)["phase"] == "idle"


def test_watch_ttl_5m_compact_at_4m_acts_before_expiry(monkeypatch, env):
    # user at T-5, assistant at T-4, armed at T: the 5m cache expires at
    # T+295; compact_at_4m becomes due at idle 240 -> T+240, before cold.
    write_transcript(
        env.tpath, [user_line(T - 5), asst_line(T - 4, w1h=0, w5m=50000)]
    )
    clock = install_clock(monkeypatch, start=T)
    env.cfg.update(
        {
            "mode": "handoff",
            "force_channel": "none",
            "safety_margin_minutes": 0,
            "ttl_5m_policy": "compact_at_4m",
            "poll_seconds": 15,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert load_state(env)["phase"] == "handoff_pending"
    assert "handoff summary" in err


def test_watch_ttl_5m_compact_at_4m_with_default_margin(monkeypatch, env):
    # Same policy with the default 5-minute margin. The deadline sits one
    # effective margin (min(margin, ttl/5) = 60 s) before the cold time, so
    # the policy still fires at idle 240 s instead of exiting cold at once.
    write_transcript(
        env.tpath, [user_line(T - 5), asst_line(T - 4, w1h=0, w5m=50000)]
    )
    clock = install_clock(monkeypatch, start=T)
    env.cfg.update(
        {
            "mode": "handoff",
            "force_channel": "none",
            "ttl_5m_policy": "compact_at_4m",
            "poll_seconds": 15,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    st = load_state(env)
    assert st["phase"] == "handoff_pending"
    # expires_at is the action deadline, not the cold time
    assert st["expires_at"] == (T - 5.0) + 300.0 - 60.0
    assert "handoff summary" in err


def test_watch_small_context_takes_no_action(monkeypatch, env):
    clock = install_clock(monkeypatch)
    write_transcript(
        env.tpath,
        [user_line(T - 100), asst_line(T - 90, create=500, read=0, w1h=500)],
    )
    env.cfg.update(
        {
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
            "mode": "handoff",
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert load_state(env)["phase"] == "idle"


def test_watch_unknown_auth_assumes_5m(monkeypatch, env):
    # No transcript split and no auth signal: detection assumes 5m,
    # the warn policy fires, and the watcher stays idle.
    clock = install_clock(monkeypatch)
    write_transcript(
        env.tpath,
        [
            user_line(T - 100),
            asst_line(T - 90, create=1000, read=399000, w1h=0, w5m=0),
        ],
    )
    env.cfg.update(
        {"idle_minutes": 0, "safety_margin_minutes": 0, "mode": "handoff"}
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert "ttl=300s (unknown auth; assume 5m)" in log_text(env)
    assert load_state(env)["phase"] == "idle"


def test_watch_env_ttl_1h_detected_and_logged(monkeypatch, env):
    # No transcript evidence, but the 1h opt-in env var decides.
    clock = install_clock(monkeypatch)
    write_transcript(
        env.tpath,
        [
            user_line(T - 100),
            asst_line(T - 90, create=1000, read=399000, w1h=0, w5m=0),
        ],
    )
    monkeypatch.setenv("ENABLE_PROMPT_CACHING_1H", "1")
    env.cfg.update(
        {"idle_minutes": 0, "safety_margin_minutes": 0, "mode": "handoff"}
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert "ttl=3600s (env ENABLE_PROMPT_CACHING_1H)" in log_text(env)
    assert load_state(env)["phase"] == "handoff_pending"


def test_watch_5m_policy_applies_to_env_detected_ttl(monkeypatch, env):
    # A 5m TTL from the env (not the transcript) still gets ttl_5m_policy.
    write_transcript(
        env.tpath,
        [
            user_line(T - 5),
            asst_line(T - 4, create=1000, read=399000, w1h=0, w5m=0),
        ],
    )
    monkeypatch.setenv("CLAUDE_CODE_PROMPT_CACHE_TTL", "5m")
    clock = install_clock(monkeypatch, start=T)
    env.cfg.update(
        {
            "mode": "handoff",
            "force_channel": "none",
            "ttl_5m_policy": "compact_at_4m",
            "poll_seconds": 15,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert "ttl=300s (env CLAUDE_CODE_PROMPT_CACHE_TTL)" in log_text(env)
    assert load_state(env)["phase"] == "handoff_pending"


def test_watch_project_setting_drives_ttl(monkeypatch, env):
    # The watcher passes the payload cwd as the project directory, so a
    # project setting decides the TTL even though the plugin process runs
    # in a different directory.
    clock = install_clock(monkeypatch)
    write_transcript(
        env.tpath,
        [
            user_line(T - 100),
            asst_line(T - 90, create=1000, read=399000, w1h=0, w5m=0),
        ],
    )
    write_json(
        os.path.join(str(env.tmp_path), ".claude", "settings.local.json"),
        {"promptCacheTtl": "1h"},
    )
    env.cfg.update(
        {"idle_minutes": 0, "safety_margin_minutes": 0, "mode": "handoff"}
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert "ttl=3600s (setting promptCacheTtl)" in log_text(env)
    assert load_state(env)["phase"] == "handoff_pending"


def test_prompt_guard_uses_project_dir_ttl(monkeypatch, env):
    # Without a setting the unknown 5m default makes the submit cold and
    # blocked. With promptCacheTtl=1h in the payload cwd the same submit
    # is warm and passes.
    clock = install_clock(monkeypatch)
    write_transcript(
        env.tpath,
        [
            user_line(T - 2000),
            asst_line(T - 1990, create=1000, read=399000, w1h=0, w5m=0),
        ],
    )
    env.cfg.update({"guard_min_usd": 0.5})  # the 5m cold cost is $0.80
    out, code = events.prompt(dict(env.payload), env.cfg)
    assert code == 0
    assert out is not None and out["decision"] == "block"

    write_json(
        os.path.join(str(env.tmp_path), ".claude", "settings.local.json"),
        {"promptCacheTtl": "1h"},
    )
    out, code = events.prompt(dict(env.payload), env.cfg)
    assert (out, code) == (None, 0)


def test_status_report_uses_saved_state_cwd_for_ttl(monkeypatch, env):
    # Status has no event payload here; it falls back to the cwd in the
    # saved state and reads the project setting from that directory.
    install_clock(monkeypatch)
    write_transcript(
        env.tpath,
        [
            user_line(T - 100),
            asst_line(T - 90, create=1000, read=399000, w1h=0, w5m=0),
        ],
    )
    save_state(
        env,
        armed_token=T - 300.0,
        transcript_path=env.tpath,
        cwd=str(env.tmp_path),
    )
    write_json(
        os.path.join(str(env.tmp_path), ".claude", "settings.json"),
        {"promptCacheTtl": "5m"},
    )
    out, code = events.status({}, env.cfg)
    assert code == 0
    assert "TTL: 5m (setting promptCacheTtl)" in out


def install_stub_channel(monkeypatch, inject_result=True, name="stub"):
    class StubChannel:
        def __init__(self):
            self.name = name

        def capture(self):
            return ""

        def verify(self, markers):
            return True

        def inject_compact(self):
            return inject_result

    stub = types.ModuleType("warmfoldlib.channels")
    stub.Channel = StubChannel
    stub.detect = lambda cfg, log=None: StubChannel()
    monkeypatch.setitem(sys.modules, "warmfoldlib.channels", stub)
    # the real channels module may exist (WP2) and be bound on the package;
    # replace the attribute so the stub always wins
    monkeypatch.setattr(warmfoldlib, "channels", stub, raising=False)
    return stub


def test_watch_auto_uses_compact_channel(monkeypatch, env):
    clock = install_clock(monkeypatch)
    install_stub_channel(monkeypatch, inject_result=True)
    env.cfg.update(
        {
            "mode": "auto",
            "force_channel": "",
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    st = load_state(env)
    assert st["phase"] == "compact_sent"
    assert st["channel"] == "stub"
    assert st["last_action_at"] > 0


def test_watch_compact_failure_falls_back_to_handoff(monkeypatch, env):
    clock = install_clock(monkeypatch)
    install_stub_channel(monkeypatch, inject_result=False)
    env.cfg.update(
        {
            "mode": "auto",
            "force_channel": "",
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    st = load_state(env)
    assert st["phase"] == "handoff_pending"
    assert "fall back to handoff" in log_text(env)


def test_watch_channel_none_disables_detection(monkeypatch, env):
    install_stub_channel(monkeypatch)
    assert events._detect_channel(dict(env.cfg, force_channel="none")) is None
    assert events._detect_channel(dict(env.cfg, force_channel="")).name == "stub"


def test_t3_channel_requires_exact_payload_identity_and_preserves_uncertain(
    monkeypatch, env
):
    class Result:
        status = "uncertain"
        reason = "dispatch outcome uncertain"
        accepted = False

    seen = {}
    fake_t3 = types.ModuleType("warmfoldlib.t3")
    fake_t3.REJECTED = "rejected"
    fake_t3.UNCERTAIN = "uncertain"
    fake_t3.resolve_target = lambda session, cwd, db: seen.update(
        {"session": session, "cwd": cwd, "db": db}
    ) or types.SimpleNamespace(accepted=True, reason="target resolved")
    fake_t3.request_compact = lambda session, cwd, cfg, log=None: Result()
    monkeypatch.setitem(sys.modules, "warmfoldlib.t3", fake_t3)
    monkeypatch.setattr(warmfoldlib, "t3", fake_t3, raising=False)
    stub = types.ModuleType("warmfoldlib.channels")
    stub.detect = lambda cfg, log=None: None
    monkeypatch.setitem(sys.modules, "warmfoldlib.channels", stub)
    monkeypatch.setattr(warmfoldlib, "channels", stub, raising=False)

    payload = {"session_id": "sdk-session", "cwd": str(env.tmp_path)}
    channel = events._detect_channel(
        dict(env.cfg, force_channel=""), str(env.tmp_path), payload
    )
    assert channel.name == "t3"
    assert seen == {"session": "sdk-session", "cwd": str(env.tmp_path), "db": None}
    assert channel.inject_compact() is False
    assert channel.outcome == "uncertain"


def test_watch_no_rearm_after_postcompact_in_same_idle_period(monkeypatch, env):
    # PostCompact stamped last_compact_at after the user last typed; the
    # next Stop must not arm again.
    clock = install_clock(monkeypatch)
    save_state(env, last_compact_at=clock.t)
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    st = load_state(env)
    assert st["armed_token"] == 0.0
    assert st["phase"] == "idle"
    assert "already acted this idle period" in log_text(env)
    assert "armed token" not in log_text(env)


def test_watch_no_rearm_after_handoff_done(monkeypatch, env):
    clock = install_clock(monkeypatch)
    save_state(env, phase="handoff_done", last_action_at=clock.t)
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert load_state(env)["armed_token"] == 0.0
    assert "already acted this idle period" in log_text(env)


def test_watch_rearms_after_user_activity(monkeypatch, env):
    # The user typed after the last action, so a fresh Stop may arm again.
    clock = install_clock(monkeypatch)
    save_state(env, last_compact_at=clock.t - 1000.0, activity_at=clock.t)

    def user_typed_again():
        st = load_state(env)
        st["activity_at"] = clock.t + 5
        state.save(env.data_dir, "sess-test", st)

    out, code, err = run_watch(
        monkeypatch, clock, env.cfg, env.payload, [user_typed_again]
    )
    assert code == 0
    st = load_state(env)
    assert st["armed_token"] == pytest.approx(clock.t - 15.0, abs=1)
    assert "already acted this idle period" not in log_text(env)
    assert "armed token" in log_text(env)
    assert "user activity" in log_text(env)


def test_watch_keepalive_resume_skips_the_action_gate(monkeypatch, env):
    # The keep-alive wake stamped last_action_at; the Stop that carries the
    # reply must still resolve the wake and arm again.
    clock = install_clock(monkeypatch)
    env.cfg.update(
        {
            "mode": "keepalive",
            "keepalive_hours": 1.0,
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    st = load_state(env)
    assert st["phase"] == "keepalive_pending"
    assert st["last_action_at"] == clock.t

    # the model's reply fires Stop; no user prompt ran, so activity_at is old
    clock.t += 60.0

    def user_typed():
        st = load_state(env)
        st["activity_at"] = clock.t + 5
        state.save(env.data_dir, "sess-test", st)

    out, code, err = run_watch(
        monkeypatch, clock, env.cfg, env.payload, [user_typed]
    )
    assert code == 0
    st = load_state(env)
    assert st["phase"] == "idle"
    assert st["wake_count"] == 1
    assert st["armed_token"] == pytest.approx(clock.t - 15.0, abs=1)
    assert "already acted this idle period" not in log_text(env)


def test_post_compact_keeps_last_action_at(monkeypatch, env):
    clock = install_clock(monkeypatch)
    save_state(env, last_action_at=123.0)
    events.post_compact(env.payload, env.cfg)
    st = load_state(env)
    assert st["last_action_at"] == 123.0
    assert st["last_compact_at"] == clock.t


def test_watch_compact_stamps_action_before_injection(monkeypatch, env):
    clock = install_clock(monkeypatch)
    seen_at_injection = {"last_action_at": None}

    class RecordingChannel:
        name = "recorder"

        def capture(self):
            return ""

        def verify(self, markers):
            return True

        def inject_compact(self):
            st = load_state(env)
            seen_at_injection["last_action_at"] = st.get("last_action_at")
            return True

    stub = types.ModuleType("warmfoldlib.channels")
    stub.Channel = RecordingChannel
    stub.detect = lambda cfg, log=None: RecordingChannel()
    monkeypatch.setitem(sys.modules, "warmfoldlib.channels", stub)
    monkeypatch.setattr(warmfoldlib, "channels", stub, raising=False)
    env.cfg.update(
        {
            "mode": "auto",
            "force_channel": "",
            "idle_minutes": 0,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
        }
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert load_state(env)["phase"] == "compact_sent"
    # the stamp was on disk before the keystrokes were sent
    assert seen_at_injection["last_action_at"] == clock.t


# ---------------------------------------------------------------------------
# races: a rival writer between the due decision and the action


def install_race_lock(monkeypatch, interfere):
    """Wrap _session_lock so interfere() runs just before the second lock
    acquisition (the action point), as a rival writer would."""
    real_lock = events._session_lock
    calls = {"n": 0}

    @contextlib.contextmanager
    def wrapped(data_dir, session_id):
        calls["n"] += 1
        if calls["n"] == 2:
            interfere()
        with real_lock(data_dir, session_id):
            yield

    monkeypatch.setattr(events, "_session_lock", wrapped)


HANDOFF_DUE_CFG = {
    "mode": "handoff",
    "idle_minutes": 0,
    "safety_margin_minutes": 0,
    "force_ttl_seconds": 3600,
    "force_channel": "none",
}


def test_watch_rival_rearm_blocks_action(monkeypatch, env):
    # Watcher B re-arms between A's due decision and A's action. A must
    # drop out under the lock: no stderr wake, no state overwrite.
    clock = install_clock(monkeypatch)
    env.cfg.update(HANDOFF_DUE_CFG)
    rival_token = clock.t + 1.0

    def rival_watcher_b():
        st = load_state(env)
        st["armed_token"] = rival_token
        st["phase"] = "idle"
        state.save(env.data_dir, "sess-test", st)

    install_race_lock(monkeypatch, rival_watcher_b)
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert err == ""
    assert "superseded before action" in log_text(env)
    st = load_state(env)
    assert st["armed_token"] == rival_token  # B keeps the session
    assert st["phase"] == "idle"
    assert st["last_action_at"] == 0.0


def test_watch_user_activity_blocks_action(monkeypatch, env):
    # The user types between A's due decision and A's action. A must not
    # wake the terminal after fresh user activity.
    clock = install_clock(monkeypatch)
    env.cfg.update(HANDOFF_DUE_CFG)
    typed_at = clock.t + 5.0

    def user_types_mid_race():
        st = load_state(env)
        st["activity_at"] = typed_at
        state.save(env.data_dir, "sess-test", st)

    install_race_lock(monkeypatch, user_types_mid_race)
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert err == ""
    assert "user activity before action" in log_text(env)
    st = load_state(env)
    assert st["activity_at"] == typed_at
    assert st["phase"] == "idle"
    assert st["last_action_at"] == 0.0


def test_watch_exits_after_post_compact_during_watch(monkeypatch, env):
    # A manual compaction fires while a watcher polls: the cleared
    # armed_token must stop the watcher on its next poll.
    clock = install_clock(monkeypatch)
    env.cfg.update(
        {
            "mode": "handoff",
            "idle_minutes": 30,
            "safety_margin_minutes": 0,
            "force_ttl_seconds": 3600,
            "force_channel": "none",
        }
    )

    def manual_compact_fires():
        events.post_compact(env.payload, env.cfg)

    out, code, err = run_watch(
        monkeypatch, clock, env.cfg, env.payload, [manual_compact_fires]
    )
    assert code == 0
    assert "superseded" in log_text(env)
    st = load_state(env)
    assert st["armed_token"] == 0.0
    assert st["phase"] == "idle"


# ---------------------------------------------------------------------------
# prompt


def test_prompt_status_interception(monkeypatch, env):
    install_clock(monkeypatch)
    payload = dict(env.payload, prompt="/warmfold:status")
    out, code = events.prompt(payload, env.cfg)
    assert code == 0
    assert "decision" not in out
    assert "suppressOriginalPrompt" not in out
    assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "warmfold status" in context
    assert "Session: sess-test" in context
    assert "Cold return cost:" in context
    assert load_state(env)["activity_at"] == T


def test_prompt_records_activity_and_resets_phase(monkeypatch, env):
    clock = install_clock(monkeypatch)
    save_state(env, phase="cold", wake_count=4)
    out, code = events.prompt(dict(env.payload, prompt="continue"), env.cfg)
    assert (out, code) == (None, 0)
    st = load_state(env)
    assert st["activity_at"] == clock.t
    assert st["phase"] == "idle"
    assert st["wake_count"] == 0


def test_prompt_guard_blocks_first_cold_submit(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    env.cfg["guard_min_usd"] = 1.0
    # user at T-4000: the 1h cache is cold at T
    write_transcript(env.tpath, [user_line(T - 4000), asst_line(T - 3990)])
    payload = dict(env.payload, prompt="keep going")
    out, code = events.prompt(payload, env.cfg)
    assert code == 0
    assert out["decision"] == "block"
    assert out["suppressOriginalPrompt"] is False
    assert "the prompt cache is cold" in out["reason"]
    assert "Resend to continue at full cost" in out["reason"]
    assert "1.60 USD" in out["reason"]
    st = load_state(env)
    assert st["guard_ack_until"] == clock.t + env.cfg["guard_ack_seconds"]

    # the ack lets exactly the next submit pass and spends itself
    out2, _ = events.prompt(payload, env.cfg)
    assert out2 is None
    assert load_state(env)["guard_ack_until"] == 0.0

    # the third cold submit blocks again, even inside the old window
    out3, _ = events.prompt(payload, env.cfg)
    assert out3["decision"] == "block"


def test_prompt_guard_mentions_fresh_handoff(monkeypatch, env):
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    write_transcript(env.tpath, [user_line(T - 4000), asst_line(T - 3990)])
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nold work\n", when=T - 600
    )
    out, _ = events.prompt(dict(env.payload, prompt="go on"), env.cfg)
    assert out is not None
    assert "handoff from" in out["reason"]
    assert str(env.tmp_path) in out["reason"]


def test_prompt_guard_passes_when_warm(monkeypatch, env):
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    out, code = events.prompt(dict(env.payload, prompt="hello"), env.cfg)
    assert (out, code) == (None, 0)


def test_prompt_guard_passes_below_cost_threshold(monkeypatch, env):
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 300
    # 50k tokens on sonnet-5, 5m write price 2 $/MTok -> $0.10 < $1.0
    write_transcript(
        env.tpath,
        [
            user_line(T - 400),
            asst_line(T - 390, create=50000, read=0, w1h=0, w5m=50000),
        ],
    )
    out, _ = events.prompt(dict(env.payload, prompt="hello"), env.cfg)
    assert out is None


def test_prompt_guard_passes_below_min_context(monkeypatch, env):
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    env.cfg["min_context_tokens"] = 100000
    # 50k tokens on fable: $1.00 clears the cost gate, but the context
    # stays below the 100k threshold.
    write_transcript(
        env.tpath,
        [
            user_line(T - 4000),
            asst_line(
                T - 3990, model="claude-fable-5", create=50000, read=0, w1h=50000
            ),
        ],
    )
    out, _ = events.prompt(dict(env.payload, prompt="hello"), env.cfg)
    assert out is None


def test_prompt_guard_disabled(monkeypatch, env):
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    env.cfg["guard"] = False
    write_transcript(env.tpath, [user_line(T - 4000), asst_line(T - 3990)])
    out, _ = events.prompt(dict(env.payload, prompt="hello"), env.cfg)
    assert out is None


# ---------------------------------------------------------------------------
# session-start


def start_payload(env, source, session="sess-new"):
    return {
        "session_id": session,
        "source": source,
        "transcript_path": env.tpath,
        "cwd": str(env.tmp_path),
    }


def test_session_start_clear_loads_handoff(monkeypatch, env):
    install_clock(monkeypatch)
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nFinish it.\n", when=T - 600
    )
    out, code = events.session_start(start_payload(env, "clear"), env.cfg)
    assert code == 0
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "# Goal" in ctx
    assert "Finish it." in ctx
    assert out["systemMessage"].startswith("warmfold: loaded handoff from")
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    assert latest["consumed_at"] is not None

    # a second clear does not inject again
    out2, _ = events.session_start(start_payload(env, "clear", "sess-new2"), env.cfg)
    assert out2 is None


def test_session_start_ignores_stale_handoff(monkeypatch, env):
    install_clock(monkeypatch)
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nold\n", when=T - 25 * 3600
    )
    out, _ = events.session_start(start_payload(env, "clear"), env.cfg)
    assert out is None
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    assert latest["consumed_at"] is None


def test_session_start_startup_shows_pointer_only(monkeypatch, env):
    install_clock(monkeypatch)
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nsecret plan\n", when=T - 600
    )
    out, _ = events.session_start(start_payload(env, "startup"), env.cfg)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("A warmfold handoff from")
    assert "Read it only if the user continues that work." in ctx
    assert "secret plan" not in ctx
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    assert latest["consumed_at"] is None  # not consumed by the pointer


def test_session_start_startup_full_load_when_configured(monkeypatch, env):
    install_clock(monkeypatch)
    env.cfg["handoff_autoload"] = "clear+startup"
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nsecret plan\n", when=T - 600
    )
    out, _ = events.session_start(start_payload(env, "startup"), env.cfg)
    assert "secret plan" in out["hookSpecificOutput"]["additionalContext"]
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    assert latest["consumed_at"] is not None


def test_session_start_autoload_off(monkeypatch, env):
    install_clock(monkeypatch)
    env.cfg["handoff_autoload"] = "off"
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nx\n", when=T - 600
    )
    out, _ = events.session_start(start_payload(env, "clear"), env.cfg)
    assert out is None


def test_session_start_resume_does_not_inject(monkeypatch, env):
    install_clock(monkeypatch)
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nx\n", when=T - 600
    )
    out, _ = events.session_start(start_payload(env, "resume"), env.cfg)
    assert out is None


def test_session_start_resets_watcher_state(monkeypatch, env):
    install_clock(monkeypatch)
    save_state(env, phase="handoff_pending", armed_token=999.0, wake_count=7)
    events.session_start(start_payload(env, "clear", session="sess-test"), env.cfg)
    st = load_state(env)
    assert st["phase"] == "idle"
    assert st["armed_token"] == 0.0
    assert st["wake_count"] == 0


def test_session_start_clear_and_startup_build_fresh_state(monkeypatch, env):
    install_clock(monkeypatch)
    dirty = dict(
        phase="cold",
        armed_token=999.0,
        activity_at=T - 50.0,
        guard_ack_until=T - 40.0,
        last_action_at=T - 30.0,
        last_compact_at=T - 20.0,
        idle_confirmed_at=T - 10.0,
        wake_count=3,
        ttl_seconds=3600,
        expires_at=T + 100.0,
    )

    def assert_fresh():
        st = load_state(env)
        assert st["phase"] == "idle"
        assert st["armed_token"] == 0.0
        assert st["wake_count"] == 0
        assert st["activity_at"] == 0.0
        assert st["guard_ack_until"] == 0.0
        assert st["last_action_at"] == 0.0
        assert st["last_compact_at"] == 0.0
        assert st["idle_confirmed_at"] == 0.0
        assert st["ttl_seconds"] is None
        assert st["expires_at"] is None

    save_state(env, **dirty)
    events.session_start(start_payload(env, "clear", session="sess-test"), env.cfg)
    assert_fresh()

    # startup builds fresh state too, so a reused id leaks nothing
    save_state(env, **dirty)
    events.session_start(start_payload(env, "startup", session="sess-test"), env.cfg)
    assert_fresh()


def test_session_start_resume_and_compact_keep_timestamps(monkeypatch, env):
    install_clock(monkeypatch)
    save_state(
        env,
        phase="cold",
        armed_token=999.0,
        activity_at=T - 50.0,
        last_action_at=T - 30.0,
        wake_count=2,
    )
    events.session_start(start_payload(env, "resume", session="sess-test"), env.cfg)
    st = load_state(env)
    # resume stops any watcher but keeps the session history
    assert st["phase"] == "idle"
    assert st["armed_token"] == 0.0
    assert st["activity_at"] == T - 50.0
    assert st["last_action_at"] == T - 30.0
    assert st["wake_count"] == 2

    events.session_start(start_payload(env, "compact", session="sess-test"), env.cfg)
    st = load_state(env)
    assert st["phase"] == "idle"
    assert st["armed_token"] == 0.0
    assert st["activity_at"] == T - 50.0
    assert st["last_action_at"] == T - 30.0
    assert st["wake_count"] == 2


# ---------------------------------------------------------------------------
# small handlers


def test_session_end_sets_done(monkeypatch, env):
    install_clock(monkeypatch)
    save_state(env, armed_token=31.0)
    out, code = events.session_end(env.payload, env.cfg)
    assert (out, code) == (None, 0)
    st = load_state(env)
    assert st["phase"] == "done"
    assert st["armed_token"] == 0.0


def test_notification_records_idle_confirm(monkeypatch, env):
    clock = install_clock(monkeypatch)
    events.notification(env.payload, env.cfg)
    assert load_state(env)["idle_confirmed_at"] == clock.t


def test_pre_compact_logs_trigger(monkeypatch, env):
    install_clock(monkeypatch)
    save_state(env, armed_token=32.0)
    events.pre_compact(dict(env.payload, trigger="manual"), env.cfg)
    assert "pre-compact trigger=manual" in log_text(env)
    assert load_state(env)["armed_token"] == 0.0


def test_post_compact_sets_idle(monkeypatch, env):
    clock = install_clock(monkeypatch)
    save_state(env, armed_token=77.0)
    events.post_compact(env.payload, env.cfg)
    st = load_state(env)
    assert st["last_compact_at"] == clock.t
    assert st["phase"] == "idle"
    assert st["armed_token"] == 0.0


def test_stop_failure_sets_idle_and_stops_watcher(monkeypatch, env):
    install_clock(monkeypatch)
    save_state(env, phase="handoff_pending", armed_token=42.0)
    events.stop_failure(env.payload, env.cfg)
    st = load_state(env)
    assert st["phase"] == "idle"
    assert st["armed_token"] == 0.0  # a running watcher exits on its next poll


# ---------------------------------------------------------------------------
# status


def test_status_report_fields(monkeypatch, env):
    install_clock(monkeypatch)
    save_state(env, armed_token=T - 300.0, transcript_path=env.tpath)
    out, code = events.status({}, env.cfg)
    assert code == 0
    for fragment in (
        "warmfold status",
        "Session: sess-test",
        "Data dir:",
        "Ledger:",
        "Model: claude-sonnet-5",
        "Context: 400000 tokens",
        "TTL: 1h (transcript)",
        "Cache: warm",
        "Cache expires in:",
        "Idle:",
        "Channel: none",
        "Mode:",
        "Next action:",
        "Cold return cost: $1.60",
        "Warm compaction cost: $0.11",
        "Handoff: none",
    ):
        assert fragment in out, fragment


def test_status_report_shows_handoff_path(monkeypatch, env):
    install_clock(monkeypatch)
    save_state(
        env,
        armed_token=T - 300.0,
        transcript_path=env.tpath,
        cwd=str(env.tmp_path),
    )
    path = events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nx\n", when=T - 100
    )
    out, _ = events.status({}, env.cfg)
    assert "Handoff: %s" % path in out


# ---------------------------------------------------------------------------
# savings ledger


WATCH_DUE_CFG = {
    "mode": "auto",
    "idle_minutes": 0,
    "safety_margin_minutes": 0,
    "force_ttl_seconds": 3600,
}


def test_watch_handoff_writes_ledger_action(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg.update(dict(WATCH_DUE_CFG, force_channel="none"))
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2  # the deliberate handoff wake
    records = ledger.load(env.data_dir)
    assert len(records) == 1
    act = records[0]
    assert act["type"] == "action"
    assert act["action"] == "handoff"
    assert act["session_id"] == "sess-test"
    assert act["cwd"] == str(env.tmp_path)
    assert act["model"] == "claude-sonnet-5"
    assert act["context_tokens"] == 400000
    assert act["ttl_seconds"] == 3600
    assert act["ts"] == pytest.approx(T + 15.0)  # the first 15 s poll
    assert act["cold_at"] == pytest.approx(T - 100.0 + 3600.0)
    assert act["paid_usd"] == pytest.approx(0.11)
    assert act["avoid_usd"] == pytest.approx(1.6)


def test_watch_compact_writes_ledger_action(monkeypatch, env):
    clock = install_clock(monkeypatch)
    install_stub_channel(monkeypatch, inject_result=True)
    env.cfg.update(dict(WATCH_DUE_CFG, force_channel=""))
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 0
    assert load_state(env)["phase"] == "compact_sent"
    records = ledger.load(env.data_dir)
    assert [r["action"] for r in records] == ["compact"]
    assert records[0]["paid_usd"] == pytest.approx(0.11)
    assert records[0]["avoid_usd"] == pytest.approx(1.6)


def test_watch_keepalive_writes_ledger_action(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg.update(
        dict(WATCH_DUE_CFG, mode="keepalive", keepalive_hours=1.0)
    )
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert load_state(env)["phase"] == "keepalive_pending"
    records = ledger.load(env.data_dir)
    assert [r["action"] for r in records] == ["keepalive"]
    # keepalive pays only the cache read: 400k tokens at $0.20/MTok
    assert records[0]["paid_usd"] == pytest.approx(0.08)
    assert records[0]["avoid_usd"] == pytest.approx(1.6)


def test_watch_compact_failure_writes_only_the_handoff_record(monkeypatch, env):
    clock = install_clock(monkeypatch)
    install_stub_channel(monkeypatch, inject_result=False)
    env.cfg.update(dict(WATCH_DUE_CFG, force_channel=""))
    out, code, err = run_watch(monkeypatch, clock, env.cfg, env.payload)
    assert code == 2
    assert load_state(env)["phase"] == "handoff_pending"
    # the failed compact must not leave a phantom action record
    assert [r["action"] for r in ledger.load(env.data_dir)] == ["handoff"]


def test_prompt_settles_early_outcome_once(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg.update(dict(WATCH_DUE_CFG, force_channel="none"))
    run_watch(monkeypatch, clock, env.cfg, env.payload)
    # the user returns while the 1h cache is still warm
    out, code = events.prompt(dict(env.payload, prompt="continue"), env.cfg)
    assert (out, code) == (None, 0)
    records = ledger.load(env.data_dir)
    outcomes = [r for r in records if r["type"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "early"
    assert outcomes[0]["ref"] == records[0]["id"]
    assert outcomes[0]["session_id"] == "sess-test"
    assert outcomes[0]["saved_usd"] == pytest.approx(-0.11)
    # a second prompt must not settle the same action again
    events.prompt(dict(env.payload, prompt="again"), env.cfg)
    outcomes = [r for r in ledger.load(env.data_dir) if r["type"] == "outcome"]
    assert len(outcomes) == 1


def test_prompt_settles_paid_cold_on_guard_pass(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    env.cfg["guard_min_usd"] = 0.5  # the 1h cold return is $1.60
    write_transcript(env.tpath, [user_line(T - 4000), asst_line(T - 3990)])
    act = ledger.append_action(
        env.data_dir,
        ts=T - 4000.0,
        action="handoff",
        session_id="sess-test",
        cwd=str(env.tmp_path),
        model="claude-sonnet-5",
        context_tokens=400000,
        ttl_seconds=3600,
        cold_at=T - 4000.0 + 3600.0,
        paid_usd=0.11,
        avoid_usd=1.6,
    )
    payload = dict(env.payload, prompt="go on")
    out, _ = events.prompt(payload, env.cfg)
    assert out is not None and out["decision"] == "block"
    # a blocked submit is not a user return: it settles nothing
    assert ledger.load(env.data_dir) == [act]
    # the ack lets exactly one submit pass; that pass is paid_cold
    out2, _ = events.prompt(payload, env.cfg)
    assert out2 is None
    outcomes = [r for r in ledger.load(env.data_dir) if r["type"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "paid_cold"
    assert outcomes[0]["ref"] == act["id"]
    assert outcomes[0]["saved_usd"] == pytest.approx(-0.11)


def test_prompt_settles_realized_with_rebuild(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    ledger.append_action(
        env.data_dir,
        ts=T - 4000.0,
        action="compact",
        session_id="sess-test",
        cwd=str(env.tmp_path),
        model="claude-sonnet-5",
        context_tokens=400000,
        ttl_seconds=3600,
        cold_at=T - 4000.0 + 3600.0,
        paid_usd=0.11,
        avoid_usd=1.6,
    )
    # the current return is itself cold on a small context, so the rebuild
    # cost comes off the saving; 1000 tokens stays below the guard floor
    write_transcript(
        env.tpath,
        [
            user_line(T - 4000),
            asst_line(T - 3990, create=100, read=900, w1h=100, w5m=0),
        ],
    )
    out, _ = events.prompt(dict(env.payload, prompt="continue"), env.cfg)
    assert out is None
    outcomes = [r for r in ledger.load(env.data_dir) if r["type"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "realized"
    # avoid 1.60 - paid 0.11 - rebuild (1k tokens at the 1h write rate 4
    # $/MTok = 0.004)
    assert outcomes[0]["saved_usd"] == pytest.approx(1.486)


def test_session_start_clear_settles_consumed_handoff_cold(monkeypatch, env):
    install_clock(monkeypatch)
    ledger.append_action(
        env.data_dir,
        ts=T - 100.0,
        action="handoff",
        session_id="sess-old",
        cwd=str(env.tmp_path),
        model="claude-sonnet-5",
        context_tokens=400000,
        ttl_seconds=3600,
        cold_at=T - 10.0,
        paid_usd=0.11,
        avoid_usd=1.6,
    )
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nx\n", when=T - 100
    )
    out, _ = events.session_start(start_payload(env, "clear"), env.cfg)
    assert out is not None  # the handoff text is injected
    outcomes = [r for r in ledger.load(env.data_dir) if r["type"] == "outcome"]
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "realized"
    # the cleared context is never rebuilt, so the rebuild term is zero
    assert outcomes[0]["saved_usd"] == pytest.approx(1.49)
    assert outcomes[0]["session_id"] == "sess-old"


def test_session_start_clear_settles_consumed_handoff_early(monkeypatch, env):
    install_clock(monkeypatch)
    ledger.append_action(
        env.data_dir,
        ts=T - 100.0,
        action="handoff",
        session_id="sess-old",
        cwd=str(env.tmp_path),
        model="claude-sonnet-5",
        context_tokens=400000,
        ttl_seconds=3600,
        cold_at=T + 1000.0,
        paid_usd=0.11,
        avoid_usd=1.6,
    )
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nx\n", when=T - 100
    )
    out, _ = events.session_start(start_payload(env, "clear"), env.cfg)
    assert out is not None
    outcomes = [r for r in ledger.load(env.data_dir) if r["type"] == "outcome"]
    assert [r["outcome"] for r in outcomes] == ["early"]
    assert outcomes[0]["saved_usd"] == pytest.approx(-0.11)


def test_prompt_report_commands_follow_normal_bookkeeping(monkeypatch, env):
    install_clock(monkeypatch)
    save_state(env, armed_token=T - 300.0, transcript_path=env.tpath)
    for command in ("/warmfold:status", "/warmfold:savings"):
        out, code = events.prompt(dict(env.payload, prompt=command), env.cfg)
        assert code == 0
        assert "decision" not in out
        assert "suppressOriginalPrompt" not in out
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert load_state(env)["activity_at"] == T
    assert not os.path.exists(
        os.path.join(env.data_dir, "ledger.jsonl")
    )


def test_prompt_savings_settles_pending_action_before_rendering_report(
    monkeypatch, env
):
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    append_handoff_action(env, cold_at=T - 400.0)
    write_transcript(env.tpath, [user_line(T - 10), asst_line(T - 5)])

    out, code = events.prompt(
        dict(env.payload, prompt="/warmfold:savings"), env.cfg
    )

    assert code == 0
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "This session: handoff at" in context
    assert "realized, saved" in context
    assert outcomes(env)[0]["outcome"] == "realized"


def test_prompt_report_respects_guard_setting(monkeypatch, env):
    install_clock(monkeypatch)
    env.cfg.update({"force_ttl_seconds": 3600, "guard_min_usd": 1.0})
    write_transcript(env.tpath, [user_line(T - 4000), asst_line(T - 3990)])

    out, _ = events.prompt(
        dict(env.payload, prompt="/warmfold:status"), env.cfg
    )
    assert out["decision"] == "block"
    assert "hookSpecificOutput" not in out

    env.cfg["guard"] = False
    out, _ = events.prompt(
        dict(env.payload, prompt="/warmfold:status"), env.cfg
    )
    assert "decision" not in out
    assert "warmfold status" in out["hookSpecificOutput"]["additionalContext"]


def test_prompt_savings_interception_shows_the_report(monkeypatch, env):
    install_clock(monkeypatch)
    out, code = events.prompt(
        dict(env.payload, prompt="  /warmfold:savings  "), env.cfg
    )
    assert code == 0
    assert "decision" not in out
    assert "suppressOriginalPrompt" not in out
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "warmfold savings" in context
    assert "This session: no actions yet" in context


def test_prompt_report_command_requires_an_exact_match(monkeypatch, env):
    install_clock(monkeypatch)
    out, code = events.prompt(
        dict(env.payload, prompt="/warmfold:statuswhatever"), env.cfg
    )
    assert (out, code) == (None, 0)
    assert load_state(env)["activity_at"] == T


# ---------------------------------------------------------------------------
# ledger review follow-ups


def append_handoff_action(env, session_id="sess-test", cold_at=None, ts=None):
    return ledger.append_action(
        env.data_dir,
        ts=T - 4000.0 if ts is None else ts,
        action="handoff",
        session_id=session_id,
        cwd=str(env.tmp_path),
        model="claude-sonnet-5",
        context_tokens=400000,
        ttl_seconds=3600,
        cold_at=T - 4000.0 + 3600.0 if cold_at is None else cold_at,
        paid_usd=0.11,
        avoid_usd=1.6,
    )


def outcomes(env):
    return [r for r in ledger.load(env.data_dir) if r["type"] == "outcome"]


def test_prompt_realized_skips_rebuild_when_current_context_is_warm(
    monkeypatch, env
):
    # The compact action is past its cold_at, but the compact summary
    # request refreshed the transcript: the current context stays warm,
    # so no rebuild cost comes off the saving.
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    append_handoff_action(env, cold_at=T - 400.0)
    write_transcript(env.tpath, [user_line(T - 10), asst_line(T - 5)])
    out, _ = events.prompt(dict(env.payload, prompt="continue"), env.cfg)
    assert out is None
    assert len(outcomes(env)) == 1
    assert outcomes(env)[0]["outcome"] == "realized"
    assert outcomes(env)[0]["saved_usd"] == pytest.approx(1.49)
    assert "estimate" not in outcomes(env)[0]


def test_prompt_realized_after_cold_at_with_cold_current_context(
    monkeypatch, env
):
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    append_handoff_action(env, cold_at=T - 400.0)
    # 90k tokens stays below the guard floor but still has a real
    # rebuild cost: 90k at the 1h write rate is $0.36.
    write_transcript(
        env.tpath,
        [
            user_line(T - 4000),
            asst_line(T - 3990, create=1000, read=89000, w1h=1000, w5m=0),
        ],
    )
    out, _ = events.prompt(dict(env.payload, prompt="continue"), env.cfg)
    assert out is None
    assert outcomes(env)[0]["outcome"] == "realized"
    assert outcomes(env)[0]["saved_usd"] == pytest.approx(1.6 - 0.11 - 0.36)


def test_prompt_realized_marks_estimate_without_transcript(monkeypatch, env):
    # No transcript anywhere: the rebuild is assumed zero and the outcome
    # carries an estimate note.
    install_clock(monkeypatch)
    env.cfg["force_ttl_seconds"] = 3600
    append_handoff_action(env, cold_at=T - 400.0)
    payload = dict(env.payload)
    del payload["transcript_path"]
    out, _ = events.prompt(payload, env.cfg)
    assert out is None  # the guard has nothing to read, so it passes
    assert len(outcomes(env)) == 1
    assert outcomes(env)[0]["outcome"] == "realized"
    assert outcomes(env)[0]["saved_usd"] == pytest.approx(1.49)
    assert outcomes(env)[0]["estimate"] == "no transcript"


def test_session_start_settle_failure_keeps_handoff_unconsumed(monkeypatch, env):
    # The outcome must land before the handoff is marked consumed; a
    # failed append leaves the pointer untouched so the next clear retries.
    install_clock(monkeypatch)
    append_handoff_action(
        env, session_id="sess-old", ts=T - 100.0, cold_at=T - 10.0
    )
    events.save_handoff(
        env.cfg, "sess-old", str(env.tmp_path), "# Goal\nx\n", when=T - 100
    )
    real_append = ledger.append_record

    def broken_append(data_dir, record):
        raise OSError("disk full")

    monkeypatch.setattr(ledger, "append_record", broken_append)
    out, _ = events.session_start(start_payload(env, "clear"), env.cfg)
    assert out is not None  # the user still gets the handoff text
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    assert latest["consumed_at"] is None
    assert "ledger outcome failed" in log_text(env)

    monkeypatch.setattr(ledger, "append_record", real_append)
    out2, _ = events.session_start(start_payload(env, "clear"), env.cfg)
    assert out2 is not None
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    assert latest["consumed_at"] is not None
    settled = outcomes(env)
    assert [r["outcome"] for r in settled] == ["realized"]
    assert settled[0]["saved_usd"] == pytest.approx(1.49)


def test_savings_interception_survives_a_corrupt_ledger(monkeypatch, env):
    install_clock(monkeypatch)
    write_raw_ledger(env, {
        "type": "outcome", "ref": [], "ts": 1.0, "session_id": "s",
        "outcome": "early", "saved_usd": -0.5,
    })
    out, code = events.prompt(
        dict(env.payload, prompt="/warmfold:savings"), env.cfg
    )
    assert code == 0
    assert "decision" not in out
    assert "suppressOriginalPrompt" not in out
    assert "warmfold savings" in out["hookSpecificOutput"]["additionalContext"]


def test_broken_report_reaches_claude_as_a_useful_response(monkeypatch, env):
    install_clock(monkeypatch)

    def boom(cfg, session_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(events, "_savings_report", boom)
    out, _ = events.prompt(
        dict(env.payload, prompt="/warmfold:savings"), env.cfg
    )
    assert "decision" not in out
    assert "suppressOriginalPrompt" not in out
    assert "warmfold: report failed: boom" in out["hookSpecificOutput"]["additionalContext"]


def test_watch_handoff_reply_stores_the_action_id(monkeypatch, env):
    clock = install_clock(monkeypatch)
    env.cfg.update(dict(WATCH_DUE_CFG, force_channel="none"))
    run_watch(monkeypatch, clock, env.cfg, env.payload)  # acts: handoff pending
    save_state(
        env, phase="handoff_pending", armed_token=clock.t, cwd=str(env.tmp_path)
    )
    payload = dict(env.payload, last_assistant_message="# Goal\nFinish it.\n")
    out, code, err = run_watch(monkeypatch, clock, env.cfg, payload)
    assert code == 0
    latest = events.load_latest_handoff(env.cfg, str(env.tmp_path))
    records = ledger.load(env.data_dir)
    assert [r["type"] for r in records] == ["action"]
    assert latest["action_id"] == records[0]["id"]


def write_raw_ledger(env, obj):
    os.makedirs(env.data_dir, exist_ok=True)
    with open(
        os.path.join(env.data_dir, "ledger.jsonl"), "a", encoding="utf-8"
    ) as handle:
        handle.write(json.dumps(obj) + "\n")


# ---------------------------------------------------------------------------
# log


def test_log_rotation_truncates_to_last_megabyte(tmp_path):
    data_dir = str(tmp_path)
    path = log.log_path(data_dir)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("x" * 100 + "\n")
        for _ in range(3 * 1024 * 10):  # about 3 MB of 100-byte lines
            handle.write("y" * 99 + "\n")
    log.append(data_dir, "test", "after rotation")
    size = os.path.getsize(path)
    assert size < 1024 * 1024 + 200
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().strip().split("\n")
    assert lines[-1].endswith("after rotation")


def test_log_line_format(tmp_path):
    data_dir = str(tmp_path)
    log.append(data_dir, "watch", "armed now")
    with open(log.log_path(data_dir), encoding="utf-8") as handle:
        line = handle.read()
    assert line.endswith(" watch armed now\n")
    assert line.split(" ")[0].endswith("Z")  # ISO timestamp in UTC


def test_log_message_newlines_flattened(tmp_path):
    data_dir = str(tmp_path)
    log.append(data_dir, "test", "two\nlines")
    with open(log.log_path(data_dir), encoding="utf-8") as handle:
        content = handle.read()
    assert content.count("\n") == 1


# ---------------------------------------------------------------------------
# handler isolation: never raise


def test_handler_error_is_swallowed(monkeypatch, env):
    import warmfold

    install_clock(monkeypatch)
    monkeypatch.setenv("WARMFOLD_DATA_DIR", env.data_dir)

    def boom(payload, cfg):
        raise RuntimeError("boom")

    monkeypatch.setitem(warmfold.EVENTS, "prompt", boom)
    code = warmfold._run("prompt", {"session_id": "x"})
    assert code == 0
    assert "handler error" in log_text(env)


# ---------------------------------------------------------------------------
# CLI end-to-end through scripts/warmfold.py


SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts", "warmfold.py"
)


def cli_env(tmp_path):
    env_vars = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("WARMFOLD_", "CLAUDE_PLUGIN_"))
    }
    env_vars["WARMFOLD_DATA_DIR"] = str(tmp_path / "data")
    env_vars["HOME"] = str(tmp_path)
    return env_vars


def run_cli(args, payload_text, tmp_path):
    import subprocess

    return subprocess.run(
        [sys.executable, SCRIPT] + args,
        input=payload_text,
        capture_output=True,
        text=True,
        env=cli_env(tmp_path),
        timeout=60,
    )


def test_cli_prompt_warm_writes_no_stdout(tmp_path, env):
    # The CLI subprocess uses the real wall clock, so anchor the transcript
    # at now instead of the fixed T constant: the cache must stay warm.
    now = time.time()
    tpath = tmp_path / "cli-warm.jsonl"
    write_transcript(str(tpath), [user_line(now - 100.0), asst_line(now - 90.0)])
    result = run_cli(
        ["prompt"],
        json.dumps(
            {
                "session_id": "cli-1",
                "prompt": "hello there",
                "transcript_path": str(tpath),
                "cwd": str(tmp_path),
            }
        ),
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert (tmp_path / "data" / "sessions" / "cli-1.json").exists()


def test_cli_prompt_status_interception(tmp_path, env):
    tpath = str(tmp_path / "cli-status.jsonl")
    current = time.time()
    write_transcript(tpath, [user_line(current - 100.0), asst_line(current - 90.0)])
    result = run_cli(
        ["prompt"],
        json.dumps(
            {
                "session_id": "cli-2",
                "prompt": "/warmfold:status",
                    "transcript_path": tpath,
                "cwd": str(tmp_path),
            }
        ),
        tmp_path,
    )
    assert result.returncode == 0
    obj = json.loads(result.stdout)
    assert "decision" not in obj
    assert "suppressOriginalPrompt" not in obj
    assert obj["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert "warmfold status" in obj["hookSpecificOutput"]["additionalContext"]


def test_cli_status_event_prints_report(tmp_path, env):
    state.save(
        str(tmp_path / "data"),
        "cli-3",
        dict(state.DEFAULTS, transcript_path=env.tpath, armed_token=T - 60),
    )
    result = run_cli(["status"], "{}", tmp_path)
    assert result.returncode == 0
    assert "warmfold status" in result.stdout
    assert "Session: cli-3" in result.stdout


def test_cli_empty_stdin_status(tmp_path):
    result = run_cli(["status"], "", tmp_path)
    assert result.returncode == 0
    assert "warmfold status" in result.stdout


def test_cli_savings_event_prints_report(tmp_path):
    result = run_cli(["savings"], "{}", tmp_path)
    assert result.returncode == 0
    assert "warmfold savings" in result.stdout
    assert "Estimates use list prices." in result.stdout


def test_cli_unknown_event_exits_zero(tmp_path):
    result = run_cli(["nonsense"], "{}", tmp_path)
    assert result.returncode == 0
    assert "unknown event" in result.stderr


def test_cli_missing_event_prints_usage(tmp_path):
    result = run_cli([], "", tmp_path)
    assert result.returncode == 0
    assert "usage" in result.stderr


def test_cli_malformed_stdin_is_tolerated(tmp_path, env):
    result = run_cli(["prompt"], "{broken json", tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""
