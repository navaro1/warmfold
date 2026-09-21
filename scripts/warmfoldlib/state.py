"""Per-session state files, written atomically.

State lives in ``<data_dir>/sessions/<session_id>.json``. A corrupt file is
treated as empty: the reader returns the defaults and the next save repairs
the file.
"""

import json
import os
import tempfile

DEFAULTS = {
    "armed_token": 0.0,
    "phase": "idle",
    "activity_at": 0.0,
    "idle_confirmed_at": 0.0,
    "guard_ack_until": 0.0,
    "last_action_at": 0.0,
    "wake_count": 0,
    "last_compact_at": 0.0,
    "channel": "",
    "cwd": None,
    "transcript_path": None,
    "model": None,
    "context_tokens": None,
    "ttl_seconds": None,
    "expires_at": None,
}

_PHASES = frozenset(
    (
        "idle",
        "handoff_pending",
        "handoff_done",
        "keepalive_pending",
        "compact_sent",
        "cold",
        "done",
    )
)
_FLOAT_KEYS = frozenset(
    (
        "armed_token",
        "activity_at",
        "idle_confirmed_at",
        "guard_ack_until",
        "last_action_at",
        "last_compact_at",
    )
)
_STR_KEYS = frozenset(("channel",))
_OPT_NUM_KEYS = frozenset(("context_tokens", "ttl_seconds", "expires_at"))


def _is_number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _validated(key, value):
    """Return value fitted to the key's type, or the default on mismatch."""
    if key == "phase":
        return value if value in _PHASES else "idle"
    if key in _FLOAT_KEYS:
        return float(value) if _is_number(value) else 0.0
    if key == "wake_count":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return int(value)
    if key in _STR_KEYS:
        return value if isinstance(value, str) else ""
    if key in _OPT_NUM_KEYS:
        return float(value) if _is_number(value) else None
    # cwd, transcript_path, model: optional strings
    return value if isinstance(value, str) else None


def _safe_id(session_id):
    """Reduce a session id to a safe file name component."""
    text = str(session_id) if session_id else "unknown"
    out = []
    for ch in text:
        if ch.isascii() and (ch.isalnum() or ch in "-_"):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out)[:120] or "unknown"


def session_path(data_dir, session_id):
    return os.path.join(data_dir, "sessions", _safe_id(session_id) + ".json")


def lock_path(data_dir, session_id):
    """Path of the per-session watcher lock file."""
    return os.path.join(data_dir, "sessions", _safe_id(session_id) + ".lock")


def load(data_dir, session_id):
    """Load one session state; missing, corrupt, or mistyped fields give
    the defaults."""
    result = dict(DEFAULTS)
    path = session_path(data_dir, session_id)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            values = json.load(handle)
    except (OSError, ValueError):
        return result
    if not isinstance(values, dict):
        return result
    for key in DEFAULTS:
        if key in values and values[key] is not None:
            result[key] = _validated(key, values[key])
    return result


def write_json_atomic(path, obj):
    """Write JSON to a temp file in the same directory, then rename."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".warmfold-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save(data_dir, session_id, values):
    """Persist one session state atomically."""
    write_json_atomic(session_path(data_dir, session_id), values)


def latest_session_id(data_dir):
    """Return the session id of the most recently modified state file."""
    directory = os.path.join(data_dir, "sessions")
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    best = None
    best_time = -1.0
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > best_time:
            best_time = mtime
            best = name[:-len(".json")]
    return best
