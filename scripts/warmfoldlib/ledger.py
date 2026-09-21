"""Savings ledger: append-only record of actions and their outcomes.

The ledger lives at ``<data_dir>/ledger.jsonl`` and is shared by all
sessions on the machine, so the report is cross-session. One JSON object
per line:

- an action record, written by the watcher right after it acts;
- an outcome record, written once per action at the first user return.

Every append takes an exclusive lock on ``<data_dir>/ledger.lock`` and
writes one ``O_APPEND`` os.write, so concurrent sessions cannot
interleave or lose records. The lock also guards the settlement step
("find the pending actions, then append the outcome"); callers hold it
with :func:`locked` and use the raw ``read_records`` and
``append_record`` helpers inside. Reading takes a shared lock and parses
at most the newest 20 MB; corrupt lines and malformed records are
skipped, never fatal.
"""

import contextlib
import json
import math
import os
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal

try:
    import fcntl
except ImportError:  # pragma: no cover - platforms without flock
    fcntl = None

MAX_READ_BYTES = 20 * 1024 * 1024

_OUTCOMES = ("realized", "early", "paid_cold")

_CENT = Decimal("0.01")


def ledger_path(data_dir):
    """The ledger file inside the data dir."""
    return os.path.join(data_dir, "ledger.jsonl")


def lock_path(data_dir):
    """The lock file guarding ledger appends and settlement."""
    return os.path.join(data_dir, "ledger.lock")


@contextlib.contextmanager
def locked(data_dir, shared=False):
    """Hold the ledger lock (fcntl.flock) while inside.

    Exclusive by default; ``shared=True`` is for readers. Callers that
    check-then-append must hold the exclusive lock and use the raw
    ``read_records`` / ``append_record`` helpers, so no other process can
    append an outcome in between.
    """
    if fcntl is None:  # pragma: no cover - platforms without flock
        yield None
        return
    os.makedirs(data_dir, exist_ok=True)
    handle = open(lock_path(data_dir), "a+")
    try:
        mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), mode)
        try:
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def append_action(data_dir, ts=None, **fields):
    """Append one action record and return it.

    The caller supplies ``action``, ``session_id``, ``cwd``, ``model``,
    ``context_tokens``, ``ttl_seconds``, ``cold_at``, ``paid_usd``, and
    ``avoid_usd`` as keyword arguments.
    """
    record = {
        "type": "action",
        "id": uuid.uuid4().hex,
        "ts": _now(ts),
    }
    record.update(fields)
    with locked(data_dir):
        append_record(data_dir, record)
    return record


def outcome_record(ref, session_id, outcome, saved_usd, ts=None, estimate=None):
    """Build one outcome record; the caller appends it under the lock."""
    record = {
        "type": "outcome",
        "ref": ref,
        "ts": _now(ts),
        "session_id": session_id,
        "outcome": outcome,
        "saved_usd": float(saved_usd),
    }
    if estimate:
        record["estimate"] = estimate
    return record


def append_outcome(data_dir, ref, session_id, outcome, saved_usd, ts=None,
                   estimate=None):
    """Append one outcome record for the action with the given id."""
    record = outcome_record(
        ref, session_id, outcome, saved_usd, ts=ts, estimate=estimate
    )
    with locked(data_dir):
        append_record(data_dir, record)
    return record


def append_record(data_dir, record):
    """One atomic os.write on an O_APPEND fd. Caller holds the lock.

    A torn last line (a crashed writer left no newline) is repaired
    first, so the new record never joins the old one.
    """
    line = (json.dumps(record) + "\n").encode("utf-8")
    os.makedirs(data_dir, exist_ok=True)
    fd = os.open(
        ledger_path(data_dir), os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644
    )
    try:
        _repair_tail(fd)
        if os.write(fd, line) != len(line):
            raise OSError("short write to %s" % ledger_path(data_dir))
    finally:
        os.close(fd)


def load(data_dir, max_bytes=MAX_READ_BYTES):
    """Parse the ledger, oldest records first. Never raises."""
    if not os.path.isdir(data_dir):
        return []
    with locked(data_dir, shared=True):
        return read_records(data_dir, max_bytes)


def read_records(data_dir, max_bytes=MAX_READ_BYTES):
    """Raw read of a fixed tail snapshot. Caller holds the lock.

    At most the last ``max_bytes`` are read. The first line is dropped
    only when it truly continues before the snapshot, and a torn tail is
    dropped, so no complete record is lost at the boundary.
    """
    path = ledger_path(data_dir)
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    offset = max(0, size - max_bytes)
    try:
        with open(path, "rb") as handle:
            if offset > 0:
                handle.seek(offset - 1)
                complete_start = handle.read(1) == b"\n"
            else:
                complete_start = True
            blob = handle.read(max_bytes)
    except OSError:
        return []
    return _parse(blob, complete_start)


def _parse(blob, complete_start):
    parts = blob.split(b"\n")
    if not complete_start:
        parts = parts[1:]  # the first line continues before the snapshot
    if blob and not blob.endswith(b"\n"):
        parts = parts[:-1]  # a torn tail: the writer never finished it
    records = []
    for raw in parts:
        text = raw.strip().decode("utf-8", "replace")
        if not text:
            continue
        try:
            obj = json.loads(text)
        except (ValueError, RecursionError):
            continue
        if _valid(obj):
            records.append(obj)
    return records


def pending_actions(records, session_id=None, action=None):
    """Action records with no outcome yet, oldest first."""
    done = _settled_refs(records)
    out = []
    for record in records:
        if record.get("type") != "action" or record.get("id") in done:
            continue
        if session_id is not None and record.get("session_id") != session_id:
            continue
        if action is not None and record.get("action") != action:
            continue
        out.append(record)
    return out


def last_session_action(records, session_id):
    """The newest action record of one session, or None."""
    newest = None
    for record in records:
        if (
            record.get("type") == "action"
            and record.get("session_id") == session_id
        ):
            newest = record
    return newest


def aggregate(records, now_ts):
    """Sums per window: today, last 7 days, all time (local time).

    Money sums use Decimal. ``realized`` counts realized outcomes (the
    amount may be negative); ``wasted`` counts early and paid_cold
    outcomes by value, whatever the amount. A duplicate outcome for one
    action counts once; the first in file order wins.
    """
    settled = _settled_refs(records)
    result = {}
    for name, start in _windows(now_ts):
        counted = set()  # duplicate outcomes count once, first in file order
        realized_usd = Decimal(0)
        realized_n = 0
        wasted_usd = Decimal(0)
        wasted_n = 0
        net = Decimal(0)
        pending = 0
        sessions = set()
        for record in records:
            if _f(record.get("ts")) < start:
                continue
            if record.get("type") == "outcome":
                ref = record.get("ref")
                if ref in counted:
                    continue
                counted.add(ref)
                saved = _dec(record.get("saved_usd"))
                net += saved
                if record.get("outcome") == "realized":
                    realized_usd += saved
                    realized_n += 1
                elif record.get("outcome") in ("early", "paid_cold"):
                    wasted_usd += saved
                    wasted_n += 1
            else:
                sessions.add(str(record.get("session_id") or "unknown"))
                if record.get("id") not in settled:
                    pending += 1
        result[name] = {
            "realized_usd": realized_usd,
            "realized_n": realized_n,
            "wasted_usd": wasted_usd,
            "wasted_n": wasted_n,
            "net": net,
            "pending": pending,
            "sessions": len(sessions),
        }
    return result


def format_report(records, now_ts=None, session_id=None):
    """The /warmfold:savings report as plain text."""
    if now_ts is None:
        now_ts = time.time()
    agg = aggregate(records, now_ts)
    names = ("today", "last 7 days", "all time")
    lines = ["warmfold savings"]
    lines.append("%-17s%8s%14s%10s" % ("", "today", "last 7 days", "all time"))
    lines.append(
        "%-17s%8s%14s%10s   (%d actions)"
        % (
            "realized saved",
            _usd(agg["today"]["realized_usd"]),
            _usd(agg["last 7 days"]["realized_usd"]),
            _usd(agg["all time"]["realized_usd"]),
            agg["all time"]["realized_n"],
        )
    )
    lines.append(
        "%-17s%8s%14s%10s   (%d actions)"
        % (
            "wasted",
            _usd(agg["today"]["wasted_usd"]),
            _usd(agg["last 7 days"]["wasted_usd"]),
            _usd(agg["all time"]["wasted_usd"]),
            agg["all time"]["wasted_n"],
        )
    )
    lines.append(
        "%-17s%8s%14s%10s"
        % (
            "net",
            _usd(agg["today"]["net"]),
            _usd(agg["last 7 days"]["net"]),
            _usd(agg["all time"]["net"]),
        )
    )
    lines.append(
        "%-17s%8d%14d%10d"
        % (
            "pending actions",
            agg["today"]["pending"],
            agg["last 7 days"]["pending"],
            agg["all time"]["pending"],
        )
    )
    lines.append(
        "%-17s%8d%14d%10d"
        % (
            "sessions",
            agg["today"]["sessions"],
            agg["last 7 days"]["sessions"],
            agg["all time"]["sessions"],
        )
    )
    lines.append(_session_line(records, session_id, now_ts))
    lines.append(
        "Estimates use list prices. A cold return is priced at the "
        "1h or 5m cache-write rate."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------


def _now(ts):
    return float(time.time() if ts is None else ts)


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _dec(value):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(0)


def _usd(value):
    amount = _dec(value).quantize(_CENT, rounding=ROUND_HALF_UP)
    if amount == 0:
        amount = Decimal("0.00")  # never display -0.00
    if amount < 0:
        return "-$%s" % (-amount)
    return "$%s" % amount


def _windows(now_ts):
    """(name, start epoch) per window; the boundaries are local midnights.

    Each midnight is built as its own calendar date, so the 7-day
    boundary stays on local midnight across a DST change.
    """
    parts = time.localtime(now_ts)
    y, m, d = parts.tm_year, parts.tm_mon, parts.tm_mday
    midnight = time.mktime((y, m, d, 0, 0, 0, 0, 0, -1))
    week_midnight = time.mktime((y, m, d - 6, 0, 0, 0, 0, 0, -1))
    return (
        ("today", midnight),
        ("last 7 days", week_midnight),
        ("all time", float("-inf")),
    )


def _settled_refs(records):
    """One ref per settled action; later duplicate outcomes change nothing."""
    refs = set()
    for record in records:
        if record.get("type") == "outcome":
            refs.add(record.get("ref"))
    return refs


def _session_line(records, session_id, now_ts):
    if not session_id:
        return "This session: no actions yet"
    act = last_session_action(records, session_id)
    if act is None:
        return "This session: no actions yet"
    stamp = time.strftime("%H:%M", time.localtime(_f(act.get("ts"))))
    outcome = None
    for record in records:
        if record.get("type") == "outcome" and record.get("ref") == act.get("id"):
            outcome = record
    if outcome is None:
        tail = "pending"
    else:
        tail = "%s, saved %s" % (
            outcome.get("outcome"),
            _usd(outcome.get("saved_usd")),
        )
    return "This session: %s at %s, %s" % (act.get("action"), stamp, tail)


def _valid(record):
    """True when a parsed record has the field types the report needs."""
    if not isinstance(record, dict):
        return False
    if record.get("type") == "action":
        return (
            _is_str(record.get("id"))
            and _is_num(record.get("ts"))
            and _is_str(record.get("session_id"))
            and _is_str(record.get("action"))
        )
    if record.get("type") == "outcome":
        return (
            _is_str(record.get("ref"))
            and _is_num(record.get("ts"))
            and _is_str(record.get("session_id"))
            and record.get("outcome") in _OUTCOMES
            and _is_num(record.get("saved_usd"))
        )
    return False


def _is_str(value):
    return isinstance(value, str) and value != ""


def _is_num(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _repair_tail(fd):
    """End the file with a newline if a torn last line is present."""
    size = os.fstat(fd).st_size
    if size == 0:
        return
    os.lseek(fd, -1, os.SEEK_END)
    if os.read(fd, 1) != b"\n":
        os.write(fd, b"\n")  # O_APPEND puts the newline at the end
