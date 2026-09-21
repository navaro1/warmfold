"""Read Claude Code session transcripts.

``read_transcript`` scans the file from the end, so a poll never loads a
huge session file. Results are cached per path, keyed by (size, mtime).
Timestamps are ISO 8601 with ``Z`` and optional fractional seconds. The
transcript format is unstable between Claude Code versions, so every field
parse is defensive: a missing field means "unknown", never a crash.
"""

import collections
import json
import os
from datetime import datetime, timezone

TranscriptInfo = collections.namedtuple(
    "TranscriptInfo",
    [
        "context_tokens",
        "model",
        "ttl_seconds",
        "last_request_start",
        "last_assistant_at",
        "caching_observed",
    ],
)
TranscriptInfo.__new__.__defaults__ = (None, None, None, 0.0, 0.0, False)

_BLOCK = 8192
_MAX_USAGE_LINES = 20
_MAX_SCAN_BYTES = 64 * 1024 * 1024
_CACHE_MAX_ENTRIES = 64

# module-level cache: path -> ((size, mtime_ns), TranscriptInfo)
_CACHE = {}


def parse_ts(value):
    """Parse an ISO 8601 UTC timestamp with Z to epoch seconds. None on error."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=timezone.utc).timestamp()
    return None


def _number(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return value
    return 0


def _token_count(value):
    """Return the number, or None when the field is missing or not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _parse_line(raw):
    text = raw.strip().decode("utf-8", "replace")
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _tail_lines(handle):
    """Yield complete lines from the end of a binary file, newest first."""
    handle.seek(0, os.SEEK_END)
    pos = handle.tell()
    buf = b""
    while pos > 0:
        step = min(_BLOCK, pos)
        pos -= step
        handle.seek(pos)
        buf = handle.read(step) + buf
        parts = buf.split(b"\n")
        buf = parts[0]
        for index in range(len(parts) - 1, 0, -1):
            if parts[index].strip():
                yield parts[index]
    if buf.strip():
        yield buf


def _scan(path):
    usage_lines = []       # newest first: (ts, usage dict, model)
    boundaries_after = []  # compact_boundary lines seen before the first usage
    boundary = None        # the boundary closest to the end, if after usage
    found_usage = False
    last_asst_at = 0.0
    user_before = None
    scanned = 0

    try:
        handle = open(path, "rb")
    except OSError:
        return TranscriptInfo()
    with handle:
        for raw in _tail_lines(handle):
            scanned += len(raw) + 1
            if scanned > _MAX_SCAN_BYTES:
                break
            line = _parse_line(raw)
            if line is None:
                continue
            ltype = line.get("type")
            ts = parse_ts(line.get("timestamp"))
            if ltype == "assistant":
                if last_asst_at == 0.0 and ts is not None:
                    last_asst_at = ts
                message = line.get("message")
                usage = message.get("usage") if isinstance(message, dict) else None
                if isinstance(usage, dict):
                    if not found_usage:
                        found_usage = True
                        boundary = boundaries_after[0] if boundaries_after else None
                    if len(usage_lines) < _MAX_USAGE_LINES:
                        usage_lines.append((ts, usage, message.get("model")))
            elif ltype == "user":
                if found_usage and user_before is None and ts is not None:
                    user_before = ts
            elif ltype == "system" and line.get("subtype") == "compact_boundary":
                if not found_usage:
                    boundaries_after.append(line)
            # Stop only when the essentials are in hand: the newest usage,
            # the user line that started the request, and a TTL verdict
            # (a nonzero cache write within the searched lines, or the
            # search exhausted its line budget).
            ttl_found = any(
                _number(u.get("cache_creation_input_tokens")) > 0
                for _ts, u, _m in usage_lines
            )
            if (
                found_usage
                and user_before is not None
                and (ttl_found or len(usage_lines) >= _MAX_USAGE_LINES)
            ):
                break

    if not usage_lines:
        return TranscriptInfo(last_assistant_at=last_asst_at)

    newest_ts, newest_usage, newest_model = usage_lines[0]
    # The context is known only when all three usage fields are numbers;
    # a partial usage object must not read as zero.
    counts = [
        _token_count(newest_usage.get(field))
        for field in (
            "input_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
    ]
    context = sum(counts) if None not in counts else None
    if boundary is not None:
        meta = boundary.get("compactMetadata")
        post = meta.get("postTokens") if isinstance(meta, dict) else None
        context = _token_count(post)

    # TTL comes from the newest assistant usage with a nonzero cache write.
    # When that line has no usable ephemeral split, the TTL is unknown; an
    # older split must not leak through.
    ttl = None
    for _ts, usage, _model in usage_lines:
        if _number(usage.get("cache_creation_input_tokens")) <= 0:
            continue
        cache_creation = usage.get("cache_creation")
        if isinstance(cache_creation, dict):
            write_1h = _number(cache_creation.get("ephemeral_1h_input_tokens"))
            write_5m = _number(cache_creation.get("ephemeral_5m_input_tokens"))
            if write_1h > 0:
                ttl = 3600
            elif write_5m > 0:
                ttl = 300
        break

    caching = any(
        _number(usage.get("cache_read_input_tokens")) > 0
        or _number(usage.get("cache_creation_input_tokens")) > 0
        for _ts, usage, _model in usage_lines
    )

    request_start = user_before if user_before is not None else (newest_ts or 0.0)
    assistant_at = last_asst_at or newest_ts or 0.0
    return TranscriptInfo(
        context_tokens=context,
        model=newest_model if isinstance(newest_model, str) else None,
        ttl_seconds=ttl,
        last_request_start=request_start,
        last_assistant_at=assistant_at,
        caching_observed=caching,
    )


def read_transcript(path):
    """Read the tail of a transcript and return a TranscriptInfo."""
    if not path:
        return TranscriptInfo()
    try:
        stat = os.stat(path)
    except OSError:
        return TranscriptInfo()
    key = (stat.st_size, stat.st_mtime_ns)
    hit = _CACHE.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    info = _scan(path)
    if len(_CACHE) >= _CACHE_MAX_ENTRIES:
        for old in list(_CACHE)[:_CACHE_MAX_ENTRIES // 2]:
            _CACHE.pop(old, None)
    _CACHE[path] = (key, info)
    return info
