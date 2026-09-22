"""Unit tests for the read-only T3 Code compaction adapter."""

import datetime
import json
import os
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from warmfoldlib import t3  # noqa: E402


NOW = datetime.datetime(2026, 9, 22, 10, 0, tzinfo=datetime.timezone.utc)


def _make_db(path, *, session_status="ready", active_turn=None, approvals=0,
             user_input=0, queued_state=None, resume="sdk-session", cwd=None,
             provider="claudeAgent", runtime_status="running", prior=True,
             latest="2026-09-22T08:00:00Z", second=False):
    cwd = cwd or os.path.join(os.path.dirname(path), "project")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE projection_thread_sessions (
          thread_id TEXT PRIMARY KEY, status TEXT, provider_name TEXT,
          provider_session_id TEXT, provider_thread_id TEXT, active_turn_id TEXT,
          runtime_mode TEXT, updated_at TEXT
        );
        CREATE TABLE provider_session_runtime (
          thread_id TEXT PRIMARY KEY, provider_name TEXT, adapter_key TEXT,
          runtime_mode TEXT, status TEXT, last_seen_at TEXT,
          resume_cursor_json TEXT, runtime_payload_json TEXT
        );
        CREATE TABLE projection_threads (
          thread_id TEXT PRIMARY KEY, runtime_mode TEXT, interaction_mode TEXT,
          pending_approval_count INTEGER, pending_user_input_count INTEGER,
          latest_user_message_at TEXT, deleted_at TEXT, archived_at TEXT
        );
        CREATE TABLE projection_turns (
          row_id INTEGER PRIMARY KEY, thread_id TEXT, state TEXT
        );
        CREATE TABLE projection_thread_messages (
          message_id TEXT PRIMARY KEY, thread_id TEXT, role TEXT, text TEXT
        );
        """
    )
    rows = [("thread-1", session_status, provider, None, None, active_turn,
             "full-access", latest)]
    if second:
        rows.append(("thread-2", session_status, provider, None, None, active_turn,
                     "full-access", latest))
    conn.executemany("INSERT INTO projection_thread_sessions VALUES (?,?,?,?,?,?,?,?)", rows)
    runtime_rows = []
    thread_rows = []
    for thread_id, *_ in rows:
        runtime_rows.append((thread_id, provider, "claude", "full-access", runtime_status,
                             latest, json.dumps({"resume": resume}),
                             json.dumps({"cwd": cwd})))
        thread_rows.append((thread_id, "full-access", "default", approvals, user_input,
                            latest, None, None))
    conn.executemany("INSERT INTO provider_session_runtime VALUES (?,?,?,?,?,?,?,?)", runtime_rows)
    conn.executemany("INSERT INTO projection_threads VALUES (?,?,?,?,?,?,?,?)", thread_rows)
    if queued_state:
        conn.execute("INSERT INTO projection_turns VALUES (1,'thread-1',?)", (queued_state,))
    if prior:
        conn.execute("INSERT INTO projection_thread_messages VALUES ('message-1','thread-1','user','hello')")
    conn.commit()
    conn.close()
    return cwd


def _target(path, cwd):
    result, failure = t3._find_target("sdk-session", cwd, str(path))
    assert failure is None
    assert result is not None
    return result


class _Response:
    status = 200

    def __init__(self, body=b'{"sequence":12}'):
        self.body = body

    def read(self):
        return self.body

    def close(self):
        pass


def test_request_uses_exact_thread_and_acknowledges_without_waiting(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db))
    target = _target(db, cwd)
    seen = {}

    def opener(request, timeout):
        seen["timeout"] = timeout
        seen["url"] = request.full_url
        seen["headers"] = dict(request.header_items())
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _Response()

    result = t3.dispatch_compact(
        target,
        {"t3_origin": "http://127.0.0.1:3773"},
        bearer_token="secret-token",
        opener=opener,
    )
    assert result.status == t3.ACCEPTED
    assert result.sequence == 12
    assert result.thread_id == "thread-1"
    assert seen["url"].endswith("/api/orchestration/dispatch")
    assert seen["headers"]["Authorization"] == "Bearer secret-token"
    assert seen["body"]["type"] == "thread.turn.start"
    assert seen["body"]["threadId"] == "thread-1"
    assert seen["body"]["message"]["text"] == "/compact"
    assert seen["body"]["message"]["attachments"] == []


def test_network_failure_is_uncertain_and_does_not_fallback(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db))
    target = _target(db, cwd)

    def opener(request, timeout):
        raise TimeoutError("request timed out")

    result = t3.dispatch_compact(
        target,
        {"t3_origin": "http://127.0.0.1:3773"},
        bearer_token="token",
        opener=opener,
    )
    assert result.status == t3.UNCERTAIN


def test_server_error_is_uncertain_and_does_not_fallback(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db))
    target = _target(db, cwd)

    class _ServerErrorResponse:
        status = 503

        def read(self):
            return b"server error"

        def close(self):
            pass

    result = t3.dispatch_compact(
        target,
        {"t3_origin": "http://127.0.0.1:3773"},
        bearer_token="token",
        opener=lambda request, timeout: _ServerErrorResponse(),
    )
    assert result.status == t3.UNCERTAIN


def test_missing_credential_is_rejected_before_network(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db))
    target = _target(db, cwd)
    called = []

    result = t3.dispatch_compact(
        target,
        {
            "t3_origin": "http://127.0.0.1:3773",
            "data_dir": str(tmp_path / "plugin-data"),
            "t3_token_path": str(tmp_path / "missing-t3-token.json"),
        },
        opener=lambda *args, **kwargs: called.append(True),
    )
    assert result.status == t3.REJECTED
    assert "credential" in result.reason
    assert not called


def test_dispatch_uses_saved_native_bearer_token(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db))
    target = _target(db, cwd)
    token_path = tmp_path / "t3-token.json"
    from warmfoldlib.t3_auth import save_token

    save_token(str(token_path), "saved-bearer", 4_000_000_000)
    seen = {}

    def opener(request, timeout):
        seen["authorization"] = dict(request.header_items()).get("Authorization")
        return _Response()

    result = t3.dispatch_compact(
        target,
        {"t3_origin": "http://127.0.0.1:3773", "t3_token_path": str(token_path)},
        opener=opener,
    )
    assert result.status == t3.ACCEPTED
    assert seen["authorization"] == "Bearer saved-bearer"


def test_dispatch_uses_data_dir_native_bearer_token(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db))
    target = _target(db, cwd)
    from warmfoldlib.t3_auth import save_token

    data_dir = tmp_path / "plugin-data"
    save_token(str(data_dir / "t3-bearer.json"), "data-dir-bearer", 4_000_000_000)
    seen = {}

    def opener(request, timeout):
        seen["authorization"] = dict(request.header_items()).get("Authorization")
        return _Response()

    result = t3.dispatch_compact(
        target,
        {"t3_origin": "http://127.0.0.1:3773", "data_dir": str(data_dir)},
        opener=opener,
    )
    assert result.status == t3.ACCEPTED
    assert seen["authorization"] == "Bearer data-dir-bearer"


def test_dispatch_renews_existing_near_expiry_token_before_send(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db))
    target = _target(db, cwd)
    from warmfoldlib.t3_auth import save_token

    app = tmp_path / "T3 Code"
    server = tmp_path / "bin.mjs"
    app.write_text("app", encoding="utf-8")
    server.write_text("server", encoding="utf-8")
    token_path = tmp_path / "t3-token.json"
    save_token(str(token_path), "near-expiry-bearer", time.time() + 30)
    pairing_calls = []
    renewal_requests = []
    dispatch_headers = {}

    def renew_runner(command, **kwargs):
        pairing_calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "pairing-id", "credential": "one-time-secret"}),
            stderr="",
        )

    def renew_opener(request, timeout):
        renewal_requests.append(request.full_url)
        if len(renewal_requests) == 1:
            return _Response(b'{"authenticated":true,"scopes":["orchestration:read","orchestration:operate"]}')
        return _Response(
            b'{"access_token":"renewed-bearer","token_type":"Bearer",'
            b'"expires_in":3600,"scope":"orchestration:read orchestration:operate"}'
        )

    def dispatch_opener(request, timeout):
        dispatch_headers.update(dict(request.header_items()))
        return _Response()

    result = t3.dispatch_compact(
        target,
        {
            "t3_origin": "http://127.0.0.1:3773",
            "t3_token_path": str(token_path),
            "t3_app_path": str(app),
            "t3_server_bin": str(server),
        },
        opener=dispatch_opener,
        renew_runner=renew_runner,
        renew_opener=renew_opener,
    )
    assert result.status == t3.ACCEPTED
    assert pairing_calls
    assert dispatch_headers["Authorization"] == "Bearer renewed-bearer"


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"session_status": "running"}, "not idle"),
        ({"active_turn": "turn-1"}, "active turn"),
        ({"approvals": 1}, "user input"),
        ({"user_input": 1}, "user input"),
        ({"queued_state": "running"}, "queued work"),
        ({"prior": False}, "no prior"),
        ({"runtime_status": "stopped"}, "unavailable"),
    ],
)
def test_idle_and_safety_guards(tmp_path, kwargs, reason):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db), **kwargs)
    result = t3.resolve_target("sdk-session", cwd, str(db))
    assert result.status == t3.REJECTED
    assert reason in result.reason


def test_identity_requires_both_resume_id_and_cwd(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db), resume="different-session")
    result = t3.resolve_target("sdk-session", cwd, str(db))
    assert result.status == t3.REJECTED
    assert "matching" in result.reason


def test_ambiguous_identity_is_rejected(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db), second=True)
    result = t3.resolve_target("sdk-session", cwd, str(db))
    assert result.status == t3.REJECTED
    assert "ambiguous" in result.reason


def test_idle_threshold_uses_latest_user_message(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db), latest="2026-09-22T09:55:00Z")
    result = t3.resolve_target(
        "sdk-session", cwd, str(db), now=NOW, min_idle_seconds=600
    )
    assert result.status == t3.REJECTED
    assert "idle" in result.reason


def test_runtime_origin_rejects_non_loopback(tmp_path):
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"origin": "https://example.invalid"}), encoding="utf-8")
    assert t3.runtime_origin(str(runtime)) is None


def test_runtime_origin_rejects_dead_server_pid(tmp_path):
    runtime = tmp_path / "runtime.json"
    runtime.write_text(
        json.dumps({"pid": 2147483647, "origin": "http://127.0.0.1:3773"}),
        encoding="utf-8",
    )
    assert t3.runtime_origin(str(runtime)) is None


def test_database_is_opened_read_only(tmp_path):
    db = tmp_path / "state.sqlite"
    cwd = _make_db(str(db))
    before = db.read_bytes()
    t3.resolve_target("sdk-session", cwd, str(db))
    assert db.read_bytes() == before
