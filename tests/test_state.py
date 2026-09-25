import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts")
)

from warmfoldlib import state  # noqa: E402

import pytest  # noqa: E402


def test_defaults_for_missing_file(tmp_path):
    st = state.load(str(tmp_path), "nope")
    assert st == dict(state.DEFAULTS)
    assert st["phase"] == "idle"
    assert st["armed_token"] == 0.0
    assert st["wake_count"] == 0


def test_legacy_handoff_phase_migrates_to_idle(tmp_path):
    st = dict(state.DEFAULTS)
    st["phase"] = "handoff_pending"
    st["armed_token"] = 1234.5
    st["wake_count"] = 3
    st["cwd"] = "/tmp/work"
    state.save(str(tmp_path), "sess-1", st)
    loaded = state.load(str(tmp_path), "sess-1")
    assert loaded["phase"] == "idle"
    assert loaded["armed_token"] == 1234.5
    assert loaded["wake_count"] == 3
    assert loaded["cwd"] == "/tmp/work"


def test_save_and_load_roundtrip(tmp_path):
    st = dict(state.DEFAULTS)
    st["phase"] = "idle"
    st["armed_token"] = 1234.5
    st["wake_count"] = 3
    st["cwd"] = "/tmp/work"
    state.save(str(tmp_path), "sess-roundtrip", st)
    loaded = state.load(str(tmp_path), "sess-roundtrip")
    assert loaded["phase"] == "idle"
    assert loaded["armed_token"] == 1234.5
    assert loaded["wake_count"] == 3
    assert loaded["cwd"] == "/tmp/work"


@pytest.mark.parametrize("phase", ["compact_pending", "compact_deferred"])
def test_native_compaction_outcomes_are_valid_phases(tmp_path, phase):
    state.save(str(tmp_path), "native", {"phase": phase})
    assert state.load(str(tmp_path), "native")["phase"] == phase


def test_corrupt_file_treated_as_empty(tmp_path):
    state.save(str(tmp_path), "sess-2", {"phase": "cold"})
    path = state.session_path(str(tmp_path), "sess-2")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{corrupt json{{{")
    loaded = state.load(str(tmp_path), "sess-2")
    assert loaded == dict(state.DEFAULTS)
    # a save repairs the file
    state.save(str(tmp_path), "sess-2", {"phase": "idle", "armed_token": 9.0})
    assert state.load(str(tmp_path), "sess-2")["armed_token"] == 9.0


def test_save_is_atomic_no_temp_leftovers(tmp_path):
    state.save(str(tmp_path), "sess-3", {"phase": "cold"})
    sessions = os.listdir(os.path.join(str(tmp_path), "sessions"))
    assert sessions == ["sess-3.json"]
    with open(state.session_path(str(tmp_path), "sess-3"), encoding="utf-8") as fh:
        assert json.load(fh)["phase"] == "cold"


def test_unknown_fields_survive_roundtrip(tmp_path):
    state.save(str(tmp_path), "sess-4", {"phase": "cold", "extra": "keep me"})
    path = state.session_path(str(tmp_path), "sess-4")
    with open(path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert on_disk["extra"] == "keep me"


def test_session_id_with_slash_stays_inside_dir(tmp_path):
    state.save(str(tmp_path), "../../evil", {"phase": "cold"})
    sessions_dir = os.path.join(str(tmp_path), "sessions")
    names = os.listdir(sessions_dir)
    # the id is flattened to one safe file name inside sessions/
    assert len(names) == 1
    assert names[0].startswith("_")
    assert names[0].endswith("evil.json")
    # no file escaped the sessions directory
    assert sorted(os.listdir(str(tmp_path))) == ["sessions"]


def test_write_json_atomic_replaces_existing(tmp_path):
    path = os.path.join(str(tmp_path), "sub", "doc.json")
    state.write_json_atomic(path, {"a": 1})
    state.write_json_atomic(path, {"a": 2})
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh) == {"a": 2}


def test_latest_session_id_picks_newest(tmp_path):
    state.save(str(tmp_path), "older", {"phase": "idle"})
    os.utime(state.session_path(str(tmp_path), "older"), (1000, 1000))
    state.save(str(tmp_path), "newer", {"phase": "idle"})
    assert state.latest_session_id(str(tmp_path)) == "newer"


def test_latest_session_id_empty_dir(tmp_path):
    assert state.latest_session_id(str(tmp_path)) is None


def test_wrong_types_fall_back_to_defaults(tmp_path):
    state.save(str(tmp_path), "sess-bad", {"phase": "cold"})
    path = state.session_path(str(tmp_path), "sess-bad")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "armed_token": "soon",
                "activity_at": [1],
                "phase": "bogus",
                "wake_count": "many",
                "channel": 5,
                "context_tokens": "big",
                "model": 7,
            },
            handle,
        )
    st = state.load(str(tmp_path), "sess-bad")
    assert st["armed_token"] == 0.0
    assert st["activity_at"] == 0.0
    assert st["phase"] == "idle"  # not in the allowed set
    assert st["wake_count"] == 0
    assert st["channel"] == ""
    assert st["context_tokens"] is None
    assert st["model"] is None


def test_numeric_coercions(tmp_path):
    state.save(
        str(tmp_path),
        "sess-num",
        {
            "armed_token": 5,
            "wake_count": 3.0,
            "context_tokens": 120000,
            "expires_at": 42,
            "phase": "cold",
        },
    )
    st = state.load(str(tmp_path), "sess-num")
    assert st["armed_token"] == 5.0
    assert st["wake_count"] == 3
    assert st["context_tokens"] == 120000.0
    assert st["expires_at"] == 42.0
    assert st["phase"] == "cold"


def test_bool_is_not_a_number(tmp_path):
    state.save(
        str(tmp_path),
        "sess-bool",
        {"armed_token": True, "wake_count": False, "context_tokens": True},
    )
    st = state.load(str(tmp_path), "sess-bool")
    assert st["armed_token"] == 0.0
    assert st["wake_count"] == 0
    assert st["context_tokens"] is None


def test_lock_path_sits_in_sessions_dir(tmp_path):
    path = state.lock_path(str(tmp_path), "../../evil")
    assert os.path.dirname(path) == os.path.join(str(tmp_path), "sessions")
    assert os.path.basename(path).endswith("evil.lock")
    assert "/" not in os.path.basename(path)[:-10]
