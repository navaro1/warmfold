"""Hook event handlers, one function per hook event.

Each handler takes ``(payload, cfg)`` and returns ``(output, exit_code)``.
``output`` is None, a dict (printed as one JSON object on stdout), or a
plain string (printed verbatim; manual ``status`` only). Handlers never
raise: the CLI wrapper catches everything, logs it, and exits 0. Exit code
2 is reserved for the deliberate keep-alive wake.

``now()`` and ``sleep()`` are module-level so tests can monkeypatch them
and drive the watcher loop without real time.
"""

import contextlib
import os
import sys
import time

try:
    import fcntl
except ImportError:  # pragma: no cover - platforms without flock
    fcntl = None

from . import cost
from . import ledger
from . import log
from . import state
from . import transcript
from . import ttl as ttl_detect

LOOP_MAX_ITERATIONS = 200000
STATUS_COMMAND = "/warmfold:status"
SAVINGS_COMMAND = "/warmfold:savings"

KEEPALIVE_REMINDER = "warmfold keep-alive. Reply with exactly: ok\n"


def now():
    return time.time()


def sleep(seconds):
    time.sleep(seconds)


@contextlib.contextmanager
def _session_lock(data_dir, session_id):
    """Hold the per-session watcher lock (fcntl.flock) while inside.

    The lock serializes state reads, decisions, and action stamps between
    concurrent hook processes (a running watcher, a Stop, a prompt).
    """
    if fcntl is None:  # pragma: no cover - platforms without flock
        yield None
        return
    directory = os.path.join(data_dir, "sessions")
    os.makedirs(directory, exist_ok=True)
    handle = open(state.lock_path(data_dir, session_id), "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _num(cfg, key, default):
    value = cfg.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _local_hm(epoch):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return "unknown"


class _T3Channel:
    """Adapter that keeps T3 dispatch outcomes distinct from keystrokes."""

    name = "t3"

    def __init__(self, session_id, cwd, cfg, log_fn=None):
        self.session_id = session_id
        self.cwd = cwd
        self.cfg = cfg
        self.log_fn = log_fn
        self.outcome = None
        self.reason = ""

    def inject_compact(self):
        try:
            from . import t3

            result = t3.request_compact(
                self.session_id,
                self.cwd,
                self.cfg,
                log=self.log_fn,
            )
            self.outcome = result.status
            self.reason = result.reason
            return result.accepted
        except Exception as exc:
            self.outcome = "rejected"
            self.reason = "T3 dispatch failed"
            if self.log_fn:
                try:
                    self.log_fn("t3: dispatch failed: %s" % type(exc).__name__)
                except Exception:
                    pass
            return False


def _detect_channel(cfg, data_dir=None, payload=None):
    """Return a verified channel or None.

    WP2 provides warmfoldlib.channels; until it exists (or when detection
    fails) there is no channel and the handlers safely defer the action.
    """
    if (cfg.get("force_channel") or "") == "none":
        return None
    sink = None
    if data_dir:
        sink = lambda msg: log.append(data_dir, "channel", str(msg))  # noqa: E731
    force = str(cfg.get("force_channel") or "").strip().lower()
    if force != "t3":
        try:
            from . import channels

            channel = channels.detect(cfg, log=sink)
            if channel is not None:
                return channel
        except Exception:
            pass
    if force not in ("", "t3"):
        return None
    payload = payload or {}
    session_id = str(payload.get("session_id") or "").strip()
    cwd = str(payload.get("cwd") or "").strip()
    if not session_id or not cwd:
        return None
    try:
        from . import t3

        result = t3.resolve_target(
            session_id,
            cwd,
            _t3_db_path(cfg),
        )
        if result.accepted:
            return _T3Channel(session_id, cwd, cfg, sink)
        if sink:
            sink("t3: skipped; %s" % result.reason)
    except Exception:
        pass
    return None


def _t3_db_path(cfg):
    value = str(cfg.get("t3_db_path") or "").strip()
    return value or None


def _resolve_mode(cfg, payload=None):
    mode = cfg.get("mode") or "auto"
    if mode == "auto":
        return "compact"
    if mode == "handoff":
        return "compact"
    return mode


# ---------------------------------------------------------------------------
# watch (Stop, async + asyncRewake)


def _stamp_info(st, info):
    """Copy the transcript-derived fields onto a state dict."""
    st["ttl_seconds"] = info.ttl_seconds
    st["context_tokens"] = info.context_tokens
    if info.model:
        st["model"] = info.model


def _resolve_ttl(info, cfg, project_dir=None):
    """Return (ttl_seconds, source) for the session.

    force_ttl_seconds is a test switch: the integration harness pins the
    TTL with it, so it sits above every detection rule. Keep it unset in
    production configuration.
    """
    forced = cfg.get("force_ttl_seconds")
    if forced:
        try:
            return float(forced), "forced (test override)"
        except (TypeError, ValueError):
            pass
    return ttl_detect.detect_ttl(info, project_dir=project_dir)


def _f(value):
    """A float, or 0.0 for anything non-numeric."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _ledger_action(data_dir, session_id, st, payload, info, ttl, cold_at,
                   t_now, action):
    """Write the savings action record for an action that just happened."""
    model = info.model or st.get("model") or ""
    ctx = info.context_tokens or 0
    if action == "keepalive":
        paid = (float(ctx) / 1e6) * cost.price(model)[1]
    else:
        paid = cost.warm_compact_usd(ctx, model)
    ledger.append_action(
        data_dir,
        ts=t_now,
        action=action,
        session_id=session_id,
        cwd=st.get("cwd") or payload.get("cwd") or "",
        model=model,
        context_tokens=ctx,
        ttl_seconds=ttl,
        cold_at=cold_at,
        paid_usd=paid,
        avoid_usd=cost.cold_return_usd(ctx, model, ttl),
    )


def _outcome_rules(act, info, cfg, payload, t_now):
    """(outcome, saved, estimate) for one pending action, per DESIGN.md.

    The rebuild term uses the current context's cold time: the
    transcript's newest request start plus the TTL. A compact summary
    request and the native compaction turn both refresh it, so a still-warm
    compacted context pays no rebuild. Missing transcript data assumes a
    zero rebuild and marks the outcome with an estimate note.
    """
    paid = _f(act.get("paid_usd"))
    avoid = _f(act.get("avoid_usd"))
    if t_now <= _f(act.get("cold_at")):
        return "early", -paid, None
    rebuild = 0.0
    estimate = None
    if info is None or info.last_request_start <= 0.0 or not info.context_tokens:
        estimate = "no transcript"
    else:
        ttl, _src = _resolve_ttl(info, cfg, payload.get("cwd") or None)
        if t_now > info.last_request_start + ttl:
            # The return itself is cold, so the new context must be
            # rebuilt too; that cost comes off the saving.
            model = info.model or act.get("model") or ""
            rebuild = cost.cold_return_usd(info.context_tokens, model, ttl)
    return "realized", avoid - paid - rebuild, estimate


def _settle_outcomes(data_dir, cfg, session_id, payload, st, t_now):
    """Write one outcome per pending action of this session.

    The ledger lock is held across the pending check and the appends, so
    two concurrent sessions cannot settle the same action twice.
    """
    tpath = payload.get("transcript_path") or st.get("transcript_path")
    with ledger.locked(data_dir):
        pending = ledger.pending_actions(
            ledger.read_records(data_dir), session_id
        )
        if not pending:
            return
        info = (
            transcript.read_transcript(tpath)
            if tpath
            else transcript.TranscriptInfo()
        )
        for act in pending:
            outcome, saved, estimate = _outcome_rules(
                act, info, cfg, payload, t_now
            )
            ledger.append_record(
                data_dir,
                ledger.outcome_record(
                    ref=act.get("id"),
                    session_id=session_id,
                    outcome=outcome,
                    saved_usd=saved,
                    ts=t_now,
                    estimate=estimate,
                ),
            )
            log.append(
                data_dir,
                "prompt",
                "ledger outcome=%s saved=%.2f" % (outcome, saved),
            )


def watch(payload, cfg):
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or "unknown"
    my_token = now()
    parent_pid = os.getppid()

    with _session_lock(data_dir, session_id):
        st = state.load(data_dir, session_id)

        # A keep-alive wake returns here; count it and arm again.
        resumed_keepalive = False
        if st.get("phase") == "keepalive_pending":
            st["phase"] = "idle"
            st["wake_count"] = int(st.get("wake_count") or 0) + 1
            resumed_keepalive = (cfg.get("mode") or "auto") == "keepalive"

        # 3. One action per idle period: a Stop that follows a sent action
        #    or a compaction must not arm again until the user types
        #    something. The keep-alive wake resolution is the one exception.
        if not resumed_keepalive:
            activity = st.get("activity_at") or 0.0
            acted = (st.get("last_compact_at") or 0.0) > activity or (
                st.get("last_action_at") or 0.0
            ) > activity
            if acted:
                log.append(
                    data_dir,
                    "watch",
                    "already acted this idle period; not arming",
                )
                return (None, 0)

        # 4. Arm.
        channel_payload = dict(payload)
        if not channel_payload.get("session_id"):
            channel_payload["session_id"] = session_id
        if not channel_payload.get("cwd"):
            channel_payload["cwd"] = st.get("cwd") or ""
        channel = _detect_channel(cfg, cfg.get("data_dir"), channel_payload)
        st["armed_token"] = my_token
        st["phase"] = "idle"
        st["channel"] = channel.name if channel is not None else "none"
        for key in ("cwd", "transcript_path", "model"):
            value = payload.get(key)
            if value:
                st[key] = value
        state.save(data_dir, session_id, st)

    tpath = st.get("transcript_path") or payload.get("transcript_path")
    info = (
        transcript.read_transcript(tpath)
        if tpath
        else transcript.TranscriptInfo()
    )
    ttl_value, ttl_source = _resolve_ttl(
        info, cfg, st.get("cwd") or payload.get("cwd")
    )
    log.append(data_dir, "watch", "ttl=%ds (%s)" % (int(ttl_value), ttl_source))
    log.append(
        data_dir, "watch", "armed token=%s channel=%s" % (my_token, st["channel"])
    )

    poll = _num(cfg, "poll_seconds", 15.0) or 15.0
    margin = _num(cfg, "safety_margin_minutes", 5.0) * 60.0
    idle_need = _num(cfg, "idle_minutes", 30.0) * 60.0
    min_ctx = _num(cfg, "min_context_tokens", 100000.0)

    for _i in range(LOOP_MAX_ITERATIONS):
        sleep(poll)
        if os.getppid() != parent_pid:
            log.append(data_dir, "watch", "exit: parent died")
            return (None, 0)
        cur = state.load(data_dir, session_id)
        if cur.get("armed_token") != my_token:
            log.append(data_dir, "watch", "exit: superseded")
            return (None, 0)
        if (cur.get("activity_at") or 0.0) > my_token:
            log.append(data_dir, "watch", "exit: user activity")
            return (None, 0)

        info = (
            transcript.read_transcript(tpath)
            if tpath
            else transcript.TranscriptInfo()
        )
        if info.last_assistant_at > my_token + 2.0:
            log.append(data_dir, "watch", "exit: newer turn happened")
            return (None, 0)

        t_now = now()
        idle = t_now - my_token
        ttl, _ = _resolve_ttl(info, cfg, cur.get("cwd") or payload.get("cwd"))
        # The cache is cold at cold_at; the action deadline sits one
        # effective margin earlier. The margin never exceeds ttl / 5, so a
        # 5-minute TTL still leaves an actionable window.
        cold_at = info.last_request_start + ttl
        effective_margin = min(margin, ttl / 5.0)
        deadline = cold_at - effective_margin
        cur["ttl_seconds"] = info.ttl_seconds
        cur["expires_at"] = deadline
        cur["context_tokens"] = info.context_tokens
        if info.model:
            cur["model"] = info.model

        if info.last_request_start <= 0.0:
            # No readable request timestamp: nothing safe to decide.
            log.append(data_dir, "watch", "exit: no transcript data")
            return (None, 0)
        if t_now >= cold_at:
            with _session_lock(data_dir, session_id):
                cur = state.load(data_dir, session_id)
                if cur.get("armed_token") != my_token:
                    return (None, 0)
                _stamp_info(cur, info)
                cur["expires_at"] = deadline
                cur["phase"] = "cold"
                state.save(data_dir, session_id, cur)
            log.append(data_dir, "watch", "phase=cold")
            return (None, 0)
        if info.context_tokens is None or info.context_tokens < min_ctx:
            return (None, 0)

        due = idle >= idle_need or (deadline - t_now) <= 60.0
        if ttl == 300.0:
            policy = cfg.get("ttl_5m_policy") or "warn"
            if policy in ("warn", "off"):
                return (None, 0)
            if policy == "compact_at_4m":
                due = idle >= 240.0
        if not due:
            continue

        channel_payload = dict(payload)
        if not channel_payload.get("session_id"):
            channel_payload["session_id"] = session_id
        if not channel_payload.get("cwd"):
            channel_payload["cwd"] = cur.get("cwd") or st.get("cwd") or ""
        mode = _resolve_mode(cfg, channel_payload)
        if mode == "warn":
            return (None, 0)

        # One writer at a time: re-check the token and the activity under
        # the lock, right before any action leaves this process.
        with _session_lock(data_dir, session_id):
            cur = state.load(data_dir, session_id)
            if cur.get("armed_token") != my_token:
                log.append(data_dir, "watch", "exit: superseded before action")
                return (None, 0)
            if (cur.get("activity_at") or 0.0) > my_token:
                log.append(data_dir, "watch", "exit: user activity before action")
                return (None, 0)
            _stamp_info(cur, info)
            cur["expires_at"] = deadline

            if mode == "keepalive":
                hours = _num(cfg, "keepalive_hours", 0.0)
                wakes = int(cur.get("wake_count") or 0)
                if wakes * 55.0 * 60.0 < hours * 3600.0:
                    cur["phase"] = "keepalive_pending"
                    cur["last_action_at"] = t_now
                    state.save(data_dir, session_id, cur)
                    try:
                        _ledger_action(
                            data_dir, session_id, cur, payload, info, ttl,
                            cold_at, t_now, "keepalive"
                        )
                    except Exception as exc:
                        log.append(
                            data_dir, "watch", "ledger action failed: %r" % (exc,)
                        )
                    sys.stderr.write(KEEPALIVE_REMINDER)
                    log.append(
                        data_dir,
                        "watch",
                        "phase=keepalive_pending wake=%d" % wakes,
                    )
                    return (None, 2)
                log.append(data_dir, "watch", "keepalive budget spent; attempting compact")
                mode = "compact"

            if mode == "compact":
                ok = False
                channel = _detect_channel(cfg, cfg.get("data_dir"), channel_payload)
                previous_last_action = cur.get("last_action_at") or 0.0
                if channel is not None:
                    # Stamp before the injection: sending the keystrokes is
                    # the action, even when the channel reports failure.
                    cur["last_action_at"] = t_now
                    state.save(data_dir, session_id, cur)
                    try:
                        ok = bool(channel.inject_compact())
                    except Exception:
                        ok = False
                if ok:
                    cur["phase"] = "compact_sent"
                    state.save(data_dir, session_id, cur)
                    try:
                        _ledger_action(
                            data_dir, session_id, cur, payload, info, ttl,
                            cold_at, t_now, "compact"
                        )
                    except Exception as exc:
                        log.append(
                            data_dir, "watch", "ledger action failed: %r" % (exc,)
                        )
                    log.append(data_dir, "watch", "phase=compact_sent")
                    return (None, 0)
                outcome = getattr(channel, "outcome", "") if channel is not None else ""
                if outcome in ("uncertain", "deferred"):
                    cur["phase"] = (
                        "compact_pending" if outcome == "uncertain" else "compact_deferred"
                    )
                    if outcome == "deferred":
                        cur["last_action_at"] = previous_last_action
                    state.save(data_dir, session_id, cur)
                    log.append(
                        data_dir,
                        "watch",
                        "compact outcome %s; no fallback" % outcome,
                    )
                    return (None, 0)
                log.append(data_dir, "watch", "compact unavailable or failed; action deferred")
                cur["phase"] = "compact_deferred"
                cur["last_action_at"] = previous_last_action
                state.save(data_dir, session_id, cur)
                return (None, 0)

    log.append(data_dir, "watch", "exit: iteration cap reached")
    return (None, 0)


# ---------------------------------------------------------------------------
# prompt (UserPromptSubmit, sync)


def prompt(payload, cfg):
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or "unknown"
    text = str(payload.get("prompt") or "")
    text = text.strip()
    report_command = text if text in (STATUS_COMMAND, SAVINGS_COMMAND) else None

    # 1. Every prompt, including report commands, records activity and
    #    settles the first return after a pending action. The report is computed only after this bookkeeping
    #    so /warmfold:savings includes the outcome from this turn.
    t_now = now()
    with _session_lock(data_dir, session_id):
        st = state.load(data_dir, session_id)
        st["activity_at"] = t_now
        if st.get("phase") in (
            "keepalive_pending",
            "compact_sent",
            "compact_pending",
            "compact_deferred",
            "cold",
            "done",
        ):
            st["phase"] = "idle"
            st["wake_count"] = 0
        state.save(data_dir, session_id, st)

    # 2. The submit is the first user return after any pending
    #    action: settle its savings outcome before building a report.
    try:
        _settle_outcomes(data_dir, cfg, session_id, payload, st, t_now)
    except Exception as exc:
        try:
            log.append(data_dir, "prompt", "ledger outcome failed: %r" % (exc,))
        except Exception:
            pass

    if report_command is not None:
        try:
            if report_command == SAVINGS_COMMAND:
                report = _savings_report(cfg, session_id)
                log.append(data_dir, "prompt", "savings context added")
            else:
                report = status_report(payload, cfg)
                log.append(data_dir, "prompt", "status context added")
        except Exception as exc:
            report = "warmfold: report failed: %s" % (exc,)
            log.append(data_dir, "prompt", "report failed: %r" % (exc,))
        return (
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": _report_context(report_command, report),
                }
            },
            0,
        )
    return (None, 0)


def _report_context(command, report):
    """Give Claude a report and an exact rendering instruction.

    UserPromptSubmit hooks cannot write an assistant message themselves.
    Suppressing the command with ``decision=block`` makes Claude Code show a
    hook error, so the report travels as additional context and the command
    markdown asks the normal response path to print it verbatim.
    """
    label = "status" if command == STATUS_COMMAND else "savings"
    return (
        "warmfold {label} report (computed locally):\n"
        "<warmfold-report>\n"
        "{report}\n"
        "</warmfold-report>\n\n"
        "The slash command instruction asks you to display this report. "
        "Output only the text between the report tags, preserving its line "
        "breaks exactly. Do not call tools, modify files, or add commentary."
    ).format(label=label, report=report.rstrip("\n"))


# ---------------------------------------------------------------------------
# session-start / session-end / notification / compact / stop-failure


def session_start(payload, cfg):
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or "unknown"
    source = payload.get("source") or "startup"

    with _session_lock(data_dir, session_id):
        if source in ("startup", "clear"):
            # A fresh start or /clear: old times must not leak into the
            # new session.
            st = dict(state.DEFAULTS)
        else:
            # resume and compact keep the timestamps but stop any watcher.
            st = state.load(data_dir, session_id)
            st["phase"] = "idle"
            st["armed_token"] = 0.0
        if payload.get("cwd"):
            st["cwd"] = payload.get("cwd")
        if payload.get("transcript_path"):
            st["transcript_path"] = payload.get("transcript_path")
        state.save(data_dir, session_id, st)
    log.append(data_dir, "session-start", "source=%s" % source)

    # SessionStart only resets watcher state. Legacy files remain untouched
    # and are never read or injected.
    return (None, 0)


def session_end(payload, cfg):
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or "unknown"
    with _session_lock(data_dir, session_id):
        st = state.load(data_dir, session_id)
        st["phase"] = "done"
        st["armed_token"] = 0.0
        state.save(data_dir, session_id, st)
    log.append(data_dir, "session-end", "phase=done")
    return (None, 0)


def notification(payload, cfg):
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or "unknown"
    with _session_lock(data_dir, session_id):
        st = state.load(data_dir, session_id)
        st["idle_confirmed_at"] = now()
        state.save(data_dir, session_id, st)
    log.append(data_dir, "notification", "idle_prompt confirmed")
    return (None, 0)


def pre_compact(payload, cfg):
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or "unknown"
    with _session_lock(data_dir, session_id):
        st = state.load(data_dir, session_id)
        st["armed_token"] = 0.0
        state.save(data_dir, session_id, st)
    log.append(
        data_dir,
        "pre-compact",
        "trigger=%s" % (payload.get("trigger") or "unknown"),
    )
    return (None, 0)


def post_compact(payload, cfg):
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or "unknown"
    with _session_lock(data_dir, session_id):
        st = state.load(data_dir, session_id)
        st["last_compact_at"] = now()
        st["phase"] = "idle"
        st["armed_token"] = 0.0
        state.save(data_dir, session_id, st)
    log.append(data_dir, "post-compact", "phase=idle")
    return (None, 0)


def stop_failure(payload, cfg):
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or "unknown"
    with _session_lock(data_dir, session_id):
        st = state.load(data_dir, session_id)
        st["phase"] = "idle"
        st["armed_token"] = 0.0
        state.save(data_dir, session_id, st)
    log.append(data_dir, "stop-failure", "phase=idle")
    return (None, 0)


# ---------------------------------------------------------------------------
# status


def status(payload, cfg):
    return (status_report(payload, cfg), 0)


def _next_action_text(cfg, st, info, ttl, t_now, payload=None):
    phase = st.get("phase") or "idle"
    if phase == "compact_sent":
        return "waiting for the compaction to finish"
    if phase == "compact_pending":
        return "waiting for an uncertain native compaction result"
    if phase == "compact_deferred":
        return "compaction deferred until the native terminal is idle"
    if phase == "keepalive_pending":
        return "waiting for the keep-alive reply"
    if phase == "done":
        return "none (session ended)"
    if not (st.get("armed_token") or 0.0):
        return "none (watcher not armed)"
    ctx = info.context_tokens if info is not None else st.get("context_tokens")
    if ctx is None or ctx < _num(cfg, "min_context_tokens", 100000.0):
        return "none (context below the threshold)"
    if ttl == 300.0 and (cfg.get("ttl_5m_policy") in ("warn", "off")):
        return "none (ttl_5m_policy=%s)" % cfg.get("ttl_5m_policy")
    ttl_eff = ttl or 3600.0
    # Same effective margin as the watcher: never more than ttl / 5.
    margin = min(_num(cfg, "safety_margin_minutes", 5.0) * 60.0, ttl_eff / 5.0)
    expires = (info.last_request_start if info is not None else 0.0) + (
        ttl_eff - margin
    )
    due_at = min(
        st["armed_token"] + _num(cfg, "idle_minutes", 30.0) * 60.0,
        expires - 60.0,
    )
    if due_at <= t_now:
        return "due now"
    return "%s about %s" % (_action_noun(cfg, payload), _local_hm(due_at))


def _action_noun(cfg, payload=None):
    mode = _resolve_mode(cfg, payload)
    return {
        "compact": "compaction",
        "keepalive": "keep-alive wake",
        "warn": "warning",
    }.get(mode, mode)


def status_report(payload, cfg):
    """Plain-text report for /warmfold:status and the manual command."""
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or state.latest_session_id(data_dir)
    st = state.load(data_dir, session_id) if session_id else dict(state.DEFAULTS)
    tpath = payload.get("transcript_path") or st.get("transcript_path")
    info = transcript.read_transcript(tpath) if tpath else None
    t_now = now()
    cwd = payload.get("cwd") or st.get("cwd") or ""

    ttl, ttl_source = _resolve_ttl(info, cfg, cwd)

    lines = ["warmfold status"]
    lines.append("Session: %s" % (session_id or "unknown"))
    lines.append("Data dir: %s" % data_dir)
    lines.append("Ledger: %s" % ledger.ledger_path(data_dir))
    model = None
    if info is not None:
        model = info.model
    model = model or st.get("model")
    lines.append("Model: %s" % (model or "unknown"))

    ctx = info.context_tokens if info is not None else st.get("context_tokens")
    lines.append("Context: %s" % ("%d tokens" % ctx if ctx else "unknown"))

    if ttl == 3600.0:
        lines.append("TTL: 1h (%s)" % ttl_source)
    elif ttl == 300.0:
        lines.append("TTL: 5m (%s)" % ttl_source)
    else:
        lines.append("TTL: %d s (%s)" % (int(ttl), ttl_source))

    req_start = info.last_request_start if info is not None else 0.0
    if req_start > 0.0 and ttl:
        cold = t_now > req_start + ttl
        lines.append("Cache: %s" % ("cold" if cold else "warm"))
        left_min = int((req_start + ttl - t_now) // 60)
        lines.append(
            "Cache expires in: %s"
            % ("%d minutes" % left_min if left_min > 0 else "expired")
        )
    else:
        lines.append("Cache: unknown")
        lines.append("Cache expires in: unknown")

    last_act = max(st.get("activity_at") or 0.0, st.get("armed_token") or 0.0)
    if last_act > 0.0:
        lines.append("Idle: %d minutes" % int(max(0.0, t_now - last_act) // 60))
    else:
        lines.append("Idle: unknown")

    channel = _detect_channel(cfg, cfg.get("data_dir"), payload)
    lines.append("Channel: %s" % ("none" if channel is None else channel.name))

    mode = cfg.get("mode") or "auto"
    if mode == "auto":
        resolved = "compact"
        lines.append("Mode: auto (resolves to %s)" % resolved)
    else:
        lines.append("Mode: %s" % mode)

    lines.append(
        "Next action: %s" % _next_action_text(cfg, st, info, ttl, t_now, payload)
    )

    if ctx and model:
        effective_ttl = ttl or 3600.0
        lines.append(
            "Cold return cost: $%.2f" % cost.cold_return_usd(ctx, model, effective_ttl)
        )
        lines.append(
            "Warm compaction cost: $%.2f" % cost.warm_compact_usd(ctx, model)
        )
    else:
        lines.append("Cold return cost: unknown")
        lines.append("Warm compaction cost: unknown")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# savings


def _savings_report(cfg, session_id):
    """The /warmfold:savings text for one session scope."""
    data_dir = cfg["data_dir"]
    return ledger.format_report(
        ledger.load(data_dir), now_ts=now(), session_id=session_id
    )


def savings(payload, cfg):
    """Plain-text savings report; also the manual `savings` CLI event."""
    data_dir = cfg["data_dir"]
    session_id = payload.get("session_id") or state.latest_session_id(data_dir)
    return (_savings_report(cfg, session_id), 0)
