"""Read-only T3 Code compaction dispatch.

T3 does not expose a separate ``compact`` command in its orchestration API.
Claude's adapter advertises compaction as the slash command ``/compact``, so
this module dispatches one ordinary ``thread.turn.start`` command to the
exact T3 thread that owns the Claude session.

The module deliberately stops at dispatch acknowledgement.  T3 runs the
compaction asynchronously and its hooks may need the same session lock held
by the caller, so waiting for the turn here can deadlock the watcher.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
from urllib import error as _urlerror
from urllib import request as _urlrequest
from urllib.parse import quote as _quote


COMPACT_COMMAND = "/compact"
ACCEPTED = "accepted"
UNCERTAIN = "uncertain"
REJECTED = "rejected"

DEFAULT_RUNTIME_PATH = os.path.expanduser("~/.t3/userdata/server-runtime.json")
DEFAULT_DB_PATH = os.path.expanduser("~/.t3/userdata/state.sqlite")
DEFAULT_ORIGIN = "http://127.0.0.1:3773"
DEFAULT_TIMEOUT = 5.0
_TERMINAL_TURN_STATES = ("completed", "error", "interrupted")
_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))


class _NoRedirect(_urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _urlerror.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _safe_log(log: Optional[Callable[[str], Any]], message: str) -> None:
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass


def _cfg_value(cfg: Any, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    try:
        if hasattr(cfg, key):
            return getattr(cfg, key)
    except Exception:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _normal_path(value: Any) -> str:
    # realpath also handles a symlinked checkout without requiring it to exist.
    text = _string(value)
    if not text:
        return ""
    return os.path.realpath(os.path.abspath(os.path.expanduser(text)))


def _loopback_origin(value: Any) -> Optional[str]:
    """Return a safe local HTTP origin, or None.

    The runtime file is local state, but validating its origin prevents a
    damaged or replaced file from turning the adapter into a remote sender.
    """
    raw = _string(value).rstrip("/")
    if not raw:
        return None
    try:
        from urllib.parse import urlsplit

        parsed = urlsplit(raw)
        if parsed.scheme != "http" or parsed.username or parsed.password:
            return None
        if parsed.hostname not in _LOOPBACK_HOSTS or parsed.path not in ("", "/"):
            return None
        if parsed.query or parsed.fragment:
            return None
        return raw
    except Exception:
        return None


def runtime_origin(path: Optional[str] = None) -> Optional[str]:
    """Read and validate the loopback origin from T3's runtime file."""
    runtime_path = path or DEFAULT_RUNTIME_PATH
    try:
        with open(runtime_path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    # A desktop launch can leave runtime metadata behind after its process
    # exits.  Never send a credential to a stale port from such a record.  A
    # missing pid is accepted for older runtime files and test fixtures.
    if "pid" in state and state.get("pid") not in (None, ""):
        try:
            pid = int(state.get("pid"))
        except (TypeError, ValueError):
            return None
        if pid <= 0:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        except PermissionError:
            # The process exists but cannot be signalled by this user.
            pass
        except OSError:
            return None
    origin = _loopback_origin(state.get("origin"))
    if origin:
        return origin
    # Older runtime files may contain only host and port.
    host = _string(state.get("host"))
    port = state.get("port")
    if host not in _LOOPBACK_HOSTS:
        return None
    try:
        port_number = int(port)
    except (TypeError, ValueError):
        return None
    if not 1 <= port_number <= 65535:
        return None
    return _loopback_origin("http://%s:%d" % (host, port_number))


@dataclass(frozen=True)
class T3Result:
    """Outcome of target resolution or dispatch.

    ``uncertain`` means the request may have reached T3 and must not be
    retried automatically.  Only ``rejected`` is a safe signal for a caller
    to consider another channel.
    """

    status: str
    reason: str
    thread_id: Optional[str] = None
    command_id: Optional[str] = None
    sequence: Optional[int] = None

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED

    @property
    def uncertain(self) -> bool:
        return self.status == UNCERTAIN


@dataclass(frozen=True)
class T3Target:
    thread_id: str
    cwd: str
    resume_session_id: str
    session_status: str
    active_turn_id: Optional[str]
    provider_runtime_status: Optional[str]
    runtime_mode: str
    interaction_mode: str
    pending_approval_count: int
    pending_user_input_count: int
    pending_turn_count: int
    latest_user_message_at: Optional[str]


def _parse_json(value: Any) -> dict:
    if not isinstance(value, str) or not value:
        return {}
    try:
        result = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return result if isinstance(result, dict) else {}


def _iso_datetime(value: Any) -> Optional[_datetime.datetime]:
    text = _string(value)
    if not text:
        return None
    try:
        parsed = _datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_datetime.timezone.utc)
    return parsed.astimezone(_datetime.timezone.utc)


def _db_connect(path: str) -> sqlite3.Connection:
    # mode=ro is intentional: target resolution must never migrate or mutate
    # the active T3 database.
    uri = "file:%s?mode=ro" % _quote(os.path.abspath(os.path.expanduser(path)), safe="/")
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _pending_turn_count(connection: sqlite3.Connection, thread_id: str) -> int:
    marks = ",".join("?" for _ in _TERMINAL_TURN_STATES)
    row = connection.execute(
        "SELECT count(*) AS count FROM projection_turns "
        "WHERE thread_id = ? AND state NOT IN (%s)" % marks,
        (thread_id,) + _TERMINAL_TURN_STATES,
    ).fetchone()
    return int(row["count"] if row else 0)


def _has_prior_user_message(connection: sqlite3.Connection, thread_id: str) -> bool:
    row = connection.execute(
        "SELECT count(*) AS count FROM projection_thread_messages "
        "WHERE thread_id = ? AND role = 'user' "
        "AND lower(trim(text)) <> ?",
        (thread_id, COMPACT_COMMAND),
    ).fetchone()
    return bool(row and int(row["count"]))


def resolve_target(
    session_id: str,
    cwd: str,
    db_path: Optional[str] = None,
    *,
    now: Optional[_datetime.datetime] = None,
    min_idle_seconds: Optional[float] = None,
) -> T3Result:
    """Resolve one idle T3 thread for a Claude hook session.

    The Claude SDK session id is matched against T3's persisted resume cursor
    and the working directory is matched as a second independent identity
    check.  Cwd alone is intentionally insufficient because several threads
    may share a checkout.
    """
    session_id = _string(session_id)
    requested_cwd = _normal_path(cwd)
    if not session_id or not requested_cwd:
        return T3Result(REJECTED, "missing session identity")
    path = db_path or DEFAULT_DB_PATH
    try:
        connection = _db_connect(path)
    except (OSError, sqlite3.Error):
        return T3Result(REJECTED, "T3 state database unavailable")
    try:
        rows = connection.execute(
            "SELECT s.thread_id, s.status AS session_status, s.provider_name "
            ", s.active_turn_id, s.runtime_mode AS session_runtime_mode "
            ", r.provider_name AS runtime_provider_name "
            ", r.status AS provider_runtime_status, r.resume_cursor_json "
            ", r.runtime_payload_json, r.runtime_mode AS runtime_runtime_mode "
            ", t.runtime_mode AS thread_runtime_mode, t.interaction_mode "
            ", t.pending_approval_count, t.pending_user_input_count "
            ", t.latest_user_message_at "
            "FROM projection_thread_sessions AS s "
            "JOIN provider_session_runtime AS r ON r.thread_id = s.thread_id "
            "JOIN projection_threads AS t ON t.thread_id = s.thread_id "
            "WHERE s.provider_name = 'claudeAgent' "
            "AND r.provider_name = 'claudeAgent' "
            "AND t.deleted_at IS NULL AND t.archived_at IS NULL"
        ).fetchall()
    except sqlite3.Error:
        connection.close()
        return T3Result(REJECTED, "T3 state schema unavailable")

    matches = []
    for row in rows:
        cursor = _parse_json(row["resume_cursor_json"])
        payload = _parse_json(row["runtime_payload_json"])
        resume = _string(cursor.get("resume"))
        payload_thread_id = _string(cursor.get("threadId"))
        runtime_cwd = _normal_path(payload.get("cwd"))
        if resume != session_id or runtime_cwd != requested_cwd:
            continue
        if payload_thread_id and payload_thread_id != _string(row["thread_id"]):
            continue
        matches.append((row, resume))

    if len(matches) != 1:
        connection.close()
        reason = "no matching T3 thread" if not matches else "ambiguous T3 thread identity"
        return T3Result(REJECTED, reason)

    row, resume = matches[0]
    thread_id = _string(row["thread_id"])
    pending_turns = _pending_turn_count(connection, thread_id)
    has_prior_message = _has_prior_user_message(connection, thread_id)
    connection.close()

    if _string(row["session_status"]).lower() != "ready":
        return T3Result(REJECTED, "T3 thread is not idle", thread_id=thread_id)
    if _string(row["active_turn_id"]):
        return T3Result(REJECTED, "T3 thread has an active turn", thread_id=thread_id)
    if _string(row["provider_runtime_status"]).lower() in ("error", "stopped"):
        return T3Result(REJECTED, "T3 Claude session is unavailable", thread_id=thread_id)
    if int(row["pending_approval_count"] or 0) or int(row["pending_user_input_count"] or 0):
        return T3Result(REJECTED, "T3 thread needs user input", thread_id=thread_id)
    if pending_turns:
        return T3Result(REJECTED, "T3 thread has queued work", thread_id=thread_id)
    if not has_prior_message:
        return T3Result(REJECTED, "T3 thread has no prior user message", thread_id=thread_id)

    if min_idle_seconds is not None:
        latest = _iso_datetime(row["latest_user_message_at"])
        current = now or _datetime.datetime.now(_datetime.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=_datetime.timezone.utc)
        if latest is None or (current.astimezone(_datetime.timezone.utc) - latest).total_seconds() < float(min_idle_seconds):
            return T3Result(REJECTED, "T3 thread is not idle long enough", thread_id=thread_id)

    return T3Result(
        ACCEPTED,
        "target resolved",
        thread_id=thread_id,
    )


def _target_from_result(result: T3Result, db_path: str, session_id: str, cwd: str) -> Optional[T3Target]:
    """Compatibility helper reserved for callers that need a full target.

    ``resolve_target`` intentionally returns a compact public result.  The
    dispatch entry point performs one combined lookup below so it can retain
    the full row without exposing database internals to callers.
    """
    return None


def _find_target(
    session_id: str,
    cwd: str,
    db_path: str,
    *,
    now: Optional[_datetime.datetime] = None,
    min_idle_seconds: Optional[float] = None,
) -> tuple:
    """Resolve and return (T3Target, failure result). Internal combined query."""
    session_id = _string(session_id)
    requested_cwd = _normal_path(cwd)
    if not session_id or not requested_cwd:
        return None, T3Result(REJECTED, "missing session identity")
    try:
        connection = _db_connect(db_path)
        rows = connection.execute(
            "SELECT s.thread_id, s.status AS session_status, s.provider_name "
            ", s.active_turn_id, s.runtime_mode AS session_runtime_mode "
            ", r.provider_name AS runtime_provider_name "
            ", r.status AS provider_runtime_status, r.resume_cursor_json "
            ", r.runtime_payload_json, r.runtime_mode AS runtime_runtime_mode "
            ", t.runtime_mode AS thread_runtime_mode, t.interaction_mode "
            ", t.pending_approval_count, t.pending_user_input_count "
            ", t.latest_user_message_at "
            "FROM projection_thread_sessions AS s "
            "JOIN provider_session_runtime AS r ON r.thread_id = s.thread_id "
            "JOIN projection_threads AS t ON t.thread_id = s.thread_id "
            "WHERE s.provider_name = 'claudeAgent' AND r.provider_name = 'claudeAgent' "
            "AND t.deleted_at IS NULL AND t.archived_at IS NULL"
        ).fetchall()
    except (OSError, sqlite3.Error):
        return None, T3Result(REJECTED, "T3 state database unavailable")

    matches = []
    for row in rows:
        cursor = _parse_json(row["resume_cursor_json"])
        payload = _parse_json(row["runtime_payload_json"])
        if _string(cursor.get("resume")) != session_id:
            continue
        if _normal_path(payload.get("cwd")) != requested_cwd:
            continue
        if _string(cursor.get("threadId")) not in ("", _string(row["thread_id"])):
            continue
        matches.append(row)
    if len(matches) != 1:
        connection.close()
        return None, T3Result(
            REJECTED,
            "no matching T3 thread" if not matches else "ambiguous T3 thread identity",
        )
    row = matches[0]
    thread_id = _string(row["thread_id"])
    try:
        pending = _pending_turn_count(connection, thread_id)
        prior = _has_prior_user_message(connection, thread_id)
    except sqlite3.Error:
        connection.close()
        return None, T3Result(REJECTED, "T3 state schema unavailable")
    connection.close()
    if _string(row["session_status"]).lower() != "ready":
        return None, T3Result(REJECTED, "T3 thread is not idle", thread_id=thread_id)
    if _string(row["active_turn_id"]):
        return None, T3Result(REJECTED, "T3 thread has an active turn", thread_id=thread_id)
    if _string(row["provider_runtime_status"]).lower() in ("error", "stopped"):
        return None, T3Result(REJECTED, "T3 Claude session is unavailable", thread_id=thread_id)
    if int(row["pending_approval_count"] or 0) or int(row["pending_user_input_count"] or 0):
        return None, T3Result(REJECTED, "T3 thread needs user input", thread_id=thread_id)
    if pending:
        return None, T3Result(REJECTED, "T3 thread has queued work", thread_id=thread_id)
    if not prior:
        return None, T3Result(REJECTED, "T3 thread has no prior user message", thread_id=thread_id)
    if min_idle_seconds is not None:
        latest = _iso_datetime(row["latest_user_message_at"])
        current = now or _datetime.datetime.now(_datetime.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=_datetime.timezone.utc)
        if latest is None or (current.astimezone(_datetime.timezone.utc) - latest).total_seconds() < float(min_idle_seconds):
            return None, T3Result(REJECTED, "T3 thread is not idle long enough", thread_id=thread_id)
    runtime_mode = (
        _string(row["thread_runtime_mode"])
        or _string(row["session_runtime_mode"])
        or _string(row["runtime_runtime_mode"])
        or "full-access"
    )
    return T3Target(
        thread_id=thread_id,
        cwd=requested_cwd,
        resume_session_id=_string(_parse_json(row["resume_cursor_json"]).get("resume")),
        session_status=_string(row["session_status"]),
        active_turn_id=_string(row["active_turn_id"]) or None,
        provider_runtime_status=_string(row["provider_runtime_status"]) or None,
        runtime_mode=runtime_mode,
        interaction_mode=_string(row["interaction_mode"]) or "default",
        pending_approval_count=int(row["pending_approval_count"] or 0),
        pending_user_input_count=int(row["pending_user_input_count"] or 0),
        pending_turn_count=pending,
        latest_user_message_at=_string(row["latest_user_message_at"]) or None,
    ), None


def _auth_headers(cfg: Any, bearer_token: Optional[str]) -> dict:
    # Explicit bearer injection is retained for isolated callers/tests. Normal
    # operation reads the private enrolled token below; cookies and config/env
    # bearer strings are intentionally not accepted.
    token = _string(bearer_token)
    if not token:
        # Native setup stores the token outside the general config so it is
        # never copied into logs or hook payloads. Import lazily to keep the
        # low-level resolver usable on Python 3.9 and avoid an import cycle.
        try:
            from .t3_auth import read_saved_token_for_config

            saved = read_saved_token_for_config(cfg)
            token = saved.token if saved is not None else ""
        except Exception:
            token = ""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if token:
        if token.lower().startswith("bearer "):
            headers["Authorization"] = token
        else:
            headers["Authorization"] = "Bearer " + token
    return headers


def dispatch_compact(
    target: T3Target,
    cfg: Any = None,
    *,
    origin: Optional[str] = None,
    bearer_token: Optional[str] = None,
    opener: Optional[Callable[..., Any]] = None,
    renew_runner: Optional[Callable[..., Any]] = None,
    renew_opener: Optional[Callable[..., Any]] = None,
    runtime_path: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    log: Optional[Callable[[str], Any]] = None,
) -> T3Result:
    """Fire one T3 dispatch request and return without waiting for the turn."""
    selected_origin = _loopback_origin(origin) if origin else None
    if not selected_origin:
        selected_origin = _loopback_origin(_cfg_value(cfg, "t3_origin"))
    if not selected_origin:
        selected_origin = runtime_origin(runtime_path)
    if not selected_origin:
        return T3Result(REJECTED, "T3 server origin unavailable", thread_id=target.thread_id)
    # An expired/near-expiry private record may be renewed through the native
    # T3 pairing CLI before dispatch.  This path is gated by renew_native_bearer
    # on an existing well-formed record; missing or malformed credentials are
    # never enrolled from an idle hook. A dispatch 401/5xx never retries here.
    if not bearer_token:
        try:
            from .t3_auth import read_saved_token_for_config, renew_native_bearer

            saved = read_saved_token_for_config(cfg)
            if saved is None:
                renewed = renew_native_bearer(
                    cfg,
                    db_path=_cfg_value(cfg, "t3_db_path"),
                    origin=selected_origin,
                    runner=renew_runner,
                    opener=renew_opener,
                    log=log,
                )
                if renewed.accepted and renewed.token:
                    bearer_token = renewed.token
        except Exception as exc:
            _safe_log(log, "T3 bearer renewal outcome uncertain: %s" % type(exc).__name__)
    headers = _auth_headers(cfg, bearer_token)
    if "Authorization" not in headers and "Cookie" not in headers:
        return T3Result(REJECTED, "T3 dispatch credential unavailable", thread_id=target.thread_id)

    command_id = str(uuid.uuid4())
    message_id = str(uuid.uuid4())
    created_at = _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    body = {
        "type": "thread.turn.start",
        "commandId": command_id,
        "threadId": target.thread_id,
        "message": {
            "messageId": message_id,
            "role": "user",
            "text": COMPACT_COMMAND,
            "attachments": [],
        },
        "runtimeMode": target.runtime_mode,
        "interactionMode": target.interaction_mode,
        "createdAt": created_at,
    }
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    request = _urlrequest.Request(
        selected_origin + "/api/orchestration/dispatch",
        data=data,
        headers=headers,
        method="POST",
    )
    opener = opener or _urlrequest.build_opener(_NoRedirect()).open
    try:
        response = opener(request, timeout=timeout)
        try:
            status = getattr(response, "status", None)
            status_code = int(status if status is not None else response.getcode())
        except Exception:
            status_code = 200
        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except _urlerror.HTTPError as exc:
        status_code = int(getattr(exc, "code", 0) or 0)
        if 500 <= status_code <= 599:
            _safe_log(log, "T3 compaction dispatch outcome uncertain (HTTP %d)" % status_code)
            return T3Result(UNCERTAIN, "T3 dispatch outcome uncertain", thread_id=target.thread_id, command_id=command_id)
        _safe_log(log, "T3 compaction dispatch rejected (HTTP %d)" % status_code)
        return T3Result(REJECTED, "T3 dispatch rejected", thread_id=target.thread_id, command_id=command_id)
    except Exception as exc:
        # The request could have reached T3 before the client observed the
        # failure; automatic fallback would risk a duplicate compaction.
        _safe_log(log, "T3 compaction dispatch outcome uncertain: %s" % type(exc).__name__)
        return T3Result(UNCERTAIN, "T3 dispatch outcome uncertain", thread_id=target.thread_id, command_id=command_id)

    if status_code < 200 or status_code >= 300:
        if 500 <= status_code <= 599:
            _safe_log(log, "T3 compaction dispatch outcome uncertain (HTTP %d)" % status_code)
            return T3Result(UNCERTAIN, "T3 dispatch outcome uncertain", thread_id=target.thread_id, command_id=command_id)
        _safe_log(log, "T3 compaction dispatch rejected (HTTP %d)" % status_code)
        return T3Result(REJECTED, "T3 dispatch rejected", thread_id=target.thread_id, command_id=command_id)
    try:
        decoded = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        sequence = int(decoded["sequence"])
        if sequence < 0:
            raise ValueError("negative sequence")
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        _safe_log(log, "T3 compaction dispatch acknowledgement malformed")
        return T3Result(UNCERTAIN, "T3 dispatch acknowledgement malformed", thread_id=target.thread_id, command_id=command_id)
    _safe_log(log, "T3 compaction dispatch accepted")
    return T3Result(ACCEPTED, "T3 compaction dispatch accepted", thread_id=target.thread_id, command_id=command_id, sequence=sequence)


def request_compact(
    session_id: str,
    cwd: str,
    cfg: Any = None,
    *,
    db_path: Optional[str] = None,
    now: Optional[_datetime.datetime] = None,
    min_idle_seconds: Optional[float] = None,
    **dispatch_options: Any
) -> T3Result:
    """Resolve an exact idle T3 session, then dispatch ``/compact``."""
    selected_db = db_path or _string(_cfg_value(cfg, "t3_db_path")) or DEFAULT_DB_PATH
    target, failure = _find_target(
        session_id,
        cwd,
        selected_db,
        now=now,
        min_idle_seconds=min_idle_seconds,
    )
    if target is None:
        return failure
    return dispatch_compact(target, cfg, **dispatch_options)


__all__ = [
    "ACCEPTED",
    "UNCERTAIN",
    "REJECTED",
    "COMPACT_COMMAND",
    "T3Result",
    "T3Target",
    "runtime_origin",
    "resolve_target",
    "dispatch_compact",
    "request_compact",
]
