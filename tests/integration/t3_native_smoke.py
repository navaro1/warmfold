#!/usr/bin/env python3
"""Provision one disposable T3 Claude thread for a native compaction smoke.

This script deliberately stops after the tiny prompt completes.  It prints
the exact hook session id and thread id needed by a caller running warmfold's
watcher, without dispatching compaction itself.  The caller can then trigger
the normal adapter and inspect the same thread's transcript.  Pass a unique
workspace under /tmp and remove the project through T3 after the smoke.

Example (credential is read from the private enrollment file, or supplied
through the process environment so it never appears in argv or output)::

    python3 tests/integration/t3_native_smoke.py --workspace /tmp/warmfold-t3-smoke-unique
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import sqlite3
import sys
import time
import uuid
from urllib import request as _request
from urllib.parse import quote as _quote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts"))
from warmfoldlib.t3_auth import read_saved_token_for_config  # noqa: E402


DEFAULT_ORIGIN = "http://127.0.0.1:3773"
DEFAULT_DB = os.path.expanduser("~/.t3/userdata/state.sqlite")
PROMPT = "Reply with exactly READY and do not use tools."


def _now():
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _post(origin, token, payload):
    req = _request.Request(
        origin.rstrip("/") + "/api/orchestration/dispatch",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with _request.urlopen(req, timeout=8) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not isinstance(body, dict) or not isinstance(body.get("sequence"), int):
        raise RuntimeError("T3 dispatch acknowledgement malformed")
    return body


def _get_thread(origin, token, thread_id):
    req = _request.Request(
        origin.rstrip("/") + "/api/orchestration/threads/" + _quote(thread_id),
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
    )
    with _request.urlopen(req, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _session_from_db(db_path, thread_id, workspace):
    uri = "file:%s?mode=ro" % _quote(os.path.abspath(db_path), safe="/")
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT r.resume_cursor_json, r.runtime_payload_json "
            "FROM provider_session_runtime AS r WHERE r.thread_id = ?",
            (thread_id,),
        ).fetchone()
    if not row:
        return None
    try:
        cursor = json.loads(row[0] or "{}")
        payload = json.loads(row[1] or "{}")
    except (TypeError, ValueError):
        return None
    resume = cursor.get("resume")
    cwd = payload.get("cwd")
    if not isinstance(resume, str) or not isinstance(cwd, str):
        return None
    if os.path.realpath(cwd) != os.path.realpath(workspace):
        return None
    return resume


def _turn_ready(db_path, thread_id):
    uri = "file:%s?mode=ro" % _quote(os.path.abspath(db_path), safe="/")
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute(
            "SELECT t.state, m.text FROM projection_turns AS t "
            "LEFT JOIN projection_thread_messages AS m ON m.message_id = t.assistant_message_id "
            "WHERE t.thread_id = ? ORDER BY t.row_id DESC LIMIT 1",
            (thread_id,),
        ).fetchone()
    return bool(row and row[0] == "completed" and isinstance(row[1], str) and "READY" in row[1])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=DEFAULT_ORIGIN)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--data-dir", default=os.environ.get("WARMFOLD_DATA_DIR", ""))
    parser.add_argument("--token-path", default=os.environ.get("WARMFOLD_T3_TOKEN_PATH", ""))
    parser.add_argument("--wait-seconds", type=float, default=180.0)
    args = parser.parse_args(argv)
    args.token = os.environ.get("WARMFOLD_T3_BEARER_TOKEN")
    if not args.token:
        saved = read_saved_token_for_config(
            {"data_dir": args.data_dir, "t3_token_path": args.token_path}
        )
        args.token = saved.token if saved is not None else None
    if not args.token:
        parser.error("an enrolled T3 bearer token is required")
    workspace = os.path.realpath(os.path.abspath(os.path.expanduser(args.workspace)))
    project_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    _post(args.origin, args.token, {
        "type": "project.create",
        "commandId": str(uuid.uuid4()),
        "projectId": project_id,
        "title": "warmfold disposable T3 smoke",
        "workspaceRoot": workspace,
        "createWorkspaceRootIfMissing": True,
        "createdAt": _now(),
    })
    _post(args.origin, args.token, {
        "type": "thread.create",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "projectId": project_id,
        "title": "warmfold disposable compaction smoke",
        "modelSelection": {"instanceId": "claudeAgent", "model": "claude-haiku-4-5"},
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "branch": None,
        "worktreePath": None,
        "createdAt": _now(),
    })
    _post(args.origin, args.token, {
        "type": "thread.turn.start",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "message": {
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "text": PROMPT,
            "attachments": [],
        },
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "createdAt": _now(),
    })
    deadline = time.time() + max(1.0, args.wait_seconds)
    session_id = None
    while time.time() < deadline:
        session_id = _session_from_db(args.db, thread_id, workspace)
        if session_id and _turn_ready(args.db, thread_id):
            break
        time.sleep(1.0)
    if not session_id:
        raise RuntimeError("T3 Claude session did not become addressable")
    print(json.dumps({
        "project_id": project_id,
        "thread_id": thread_id,
        "workspace": workspace,
        "hook_session_id": session_id,
        "prompt": PROMPT,
        "next": "run warmfold watcher/adapter with this hook_session_id; inspect this thread before cleanup",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception as exc:
        print("t3 smoke failed: %s" % type(exc).__name__, file=sys.stderr)
        raise SystemExit(1)
