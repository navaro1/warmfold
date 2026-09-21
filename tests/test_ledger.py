import json
import multiprocessing
import os
import sys
import time
from decimal import Decimal

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts")
)

from warmfoldlib import ledger  # noqa: E402

import pytest  # noqa: E402

NOW = 1789974000.0  # 2026-09-21T07:00:00Z


@pytest.fixture(autouse=True)
def utc_clock(monkeypatch):
    # Pin the local zone so the window boundaries are exact.
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield


@pytest.fixture()
def data(tmp_path):
    return str(tmp_path / "data")


def action(data, session_id="s1", ts=NOW, **fields):
    base = {
        "action": "handoff",
        "session_id": session_id,
        "cwd": "/w",
        "model": "claude-sonnet-5",
        "context_tokens": 400000,
        "ttl_seconds": 3600,
        "cold_at": ts + 3600.0,
        "paid_usd": 0.11,
        "avoid_usd": 1.6,
    }
    base.update(fields)
    return ledger.append_action(data, ts=ts, **base)


def outcome(data, ref, saved, ts=NOW, session_id="s1", kind="realized"):
    return ledger.append_outcome(
        data, ref=ref, session_id=session_id, outcome=kind, saved_usd=saved, ts=ts
    )


def write_raw(data, text):
    os.makedirs(data, exist_ok=True)
    with open(ledger.ledger_path(data), "a", encoding="utf-8") as handle:
        handle.write(text)


def test_append_and_load_round_trip(data):
    act = action(data, ts=1000.5, action="compact")
    outcome(data, act["id"], 1.49, ts=1001.5)
    records = ledger.load(data)
    assert [r["type"] for r in records] == ["action", "outcome"]
    assert records[0]["id"] == act["id"]
    assert records[0]["ts"] == 1000.5
    assert records[0]["action"] == "compact"
    assert records[0]["paid_usd"] == 0.11
    assert records[1]["ref"] == act["id"]
    assert records[1]["saved_usd"] == 1.49
    assert records[1]["outcome"] == "realized"


def test_pending_actions_and_last_session_action(data):
    a1 = action(data, session_id="s1", ts=NOW - 100)
    a2 = action(data, session_id="s2", ts=NOW - 50, action="compact")
    a3 = action(data, session_id="s1", ts=NOW - 10, action="keepalive")
    outcome(data, a1["id"], 1.49)
    records = ledger.load(data)
    # s1: a1 has an outcome, a3 is pending; a2 belongs to s2
    assert [r["id"] for r in ledger.pending_actions(records, "s1")] == [a3["id"]]
    assert [r["id"] for r in ledger.pending_actions(records, "s2")] == [a2["id"]]
    assert ledger.pending_actions(records, "s1", action="handoff") == []
    assert ledger.last_session_action(records, "s1")["id"] == a3["id"]
    assert ledger.last_session_action(records, "nope") is None


def test_corrupt_lines_are_skipped(data):
    good = action(data, ts=1000.0)
    write_raw(data, "not json at all\n")
    write_raw(data, "[1, 2, 3]\n")
    write_raw(data, json.dumps({"type": "bogus"}) + "\n")
    write_raw(data, "\n")
    outcome(data, good["id"], 1.49, ts=1001.0)
    records = ledger.load(data)
    assert [r["type"] for r in records] == ["action", "outcome"]


def test_deeply_nested_line_is_skipped(data):
    write_raw(data, "[" * 60000 + "]" * 60000 + "\n")
    good = action(data, ts=1000.0)
    records = ledger.load(data)
    assert [r["id"] for r in records] == [good["id"]]


def test_read_cap_keeps_only_the_newest_records(data):
    for index in range(5):
        action(data, session_id="s%d" % index, ts=1000.0 + index)
    path = ledger.ledger_path(data)
    size = os.path.getsize(path)
    line_size = size // 5
    records = ledger.load(data, max_bytes=line_size * 2 + 10)
    # the two newest actions survive, the partial older line is dropped
    assert [r["session_id"] for r in records] == ["s3", "s4"]


def test_windows_today_seven_days_and_all(data):
    # UTC: NOW is 07:00, so local midnight is NOW - 7 h.
    midnight = NOW - 7 * 3600.0
    a_today = action(data, session_id="s1", ts=midnight + 60)
    a_pending_today = action(data, session_id="s2", ts=midnight + 120)
    a_week = action(data, session_id="s3", ts=NOW - 3 * 86400)
    a_old = action(data, session_id="s4", ts=NOW - 40 * 86400)
    a_old_pending = action(data, session_id="s5", ts=NOW - 40 * 86400)
    outcome(data, a_today["id"], 2.0, ts=midnight + 200)
    outcome(data, a_week["id"], -0.5, ts=NOW - 3 * 86400, session_id="s3", kind="early")
    outcome(data, a_old["id"], 1.0, ts=NOW - 40 * 86400, session_id="s4")
    agg = ledger.aggregate(ledger.load(data), NOW)
    assert agg["today"] == {
        "realized_usd": 2.0,
        "realized_n": 1,
        "wasted_usd": 0.0,
        "wasted_n": 0,
        "net": 2.0,
        "pending": 1,
        "sessions": 2,
    }
    assert agg["last 7 days"]["realized_usd"] == 2.0
    assert agg["last 7 days"]["wasted_usd"] == -0.5  # wasted sums by value
    assert agg["last 7 days"]["wasted_n"] == 1
    assert agg["last 7 days"]["net"] == 1.5
    assert agg["last 7 days"]["pending"] == 1
    assert agg["last 7 days"]["sessions"] == 3
    assert agg["all time"]["realized_usd"] == 3.0
    assert agg["all time"]["realized_n"] == 2
    assert agg["all time"]["wasted_usd"] == -0.5
    assert agg["all time"]["net"] == 2.5
    assert agg["all time"]["pending"] == 2
    assert agg["all time"]["sessions"] == 5


def test_report_with_zero_data(data):
    report = ledger.format_report([], now_ts=NOW, session_id=None)
    assert "warmfold savings" in report
    for label in (
        "realized saved",
        "wasted",
        "net",
        "pending actions",
        "sessions",
    ):
        assert label in report, label
    assert report.count("$0.00") == 9
    assert "(0 actions)" in report
    assert "This session: no actions yet" in report
    assert "Estimates use list prices." in report


def test_report_rows_and_session_line(data):
    midnight = NOW - 7 * 3600.0
    a1 = action(data, session_id="s1", ts=midnight + 60)
    outcome(data, a1["id"], 2.0, ts=midnight + 200)
    a2 = action(data, session_id="s2", ts=NOW - 3 * 86400, action="compact")
    outcome(data, a2["id"], -0.5, ts=NOW - 3 * 86400, session_id="s2", kind="early")
    report = ledger.format_report(ledger.load(data), now_ts=NOW, session_id="s1")
    assert "$2.00" in report
    assert "$0.50" in report  # wasted is shown as a positive amount
    assert "(1 actions)" in report
    assert "This session: handoff at 00:01, realized, saved $2.00" in report
    other = ledger.format_report(ledger.load(data), now_ts=NOW, session_id="s3")
    assert "This session: no actions yet" in other
    wasted_line = ledger.format_report(
        ledger.load(data), now_ts=NOW, session_id="s2"
    )
    assert "early, saved -$0.50" in wasted_line


def test_missing_ledger_file_loads_empty(data):
    assert ledger.load(os.path.join(data, "does-not-exist")) == []


# ---------------------------------------------------------------------------
# record validation


def test_malformed_records_are_skipped(data):
    good = action(data, ts=1000.0)
    write_raw(data, json.dumps({
        "type": "outcome", "ref": [], "ts": 1.0, "session_id": "s",
        "outcome": "early", "saved_usd": -0.5}) + "\n")  # unhashable ref
    write_raw(data, json.dumps({
        "type": "action", "id": 42, "ts": 1.0, "session_id": "s",
        "action": "handoff"}) + "\n")  # non-string id
    write_raw(data, json.dumps({
        "type": "outcome", "ref": "x", "ts": "abc", "session_id": "s",
        "outcome": "early", "saved_usd": -0.5}) + "\n")  # non-numeric ts
    write_raw(data, json.dumps({
        "type": "outcome", "ref": "y", "ts": 1.0, "session_id": "s",
        "outcome": "weird", "saved_usd": -0.5}) + "\n")  # unknown outcome
    write_raw(data, json.dumps({
        "type": "outcome", "ref": "z", "ts": 1.0, "session_id": "s",
        "outcome": "early", "saved_usd": float("nan")}) + "\n")  # NaN
    write_raw(data, json.dumps({
        "type": "outcome", "ref": "w", "ts": 1.0, "session_id": True,
        "outcome": "early", "saved_usd": -0.5}) + "\n")  # non-string session
    records = ledger.load(data)
    assert [r["id"] for r in records] == [good["id"]]


def test_duplicate_outcomes_count_once(data):
    a1 = action(data, session_id="s1", ts=NOW - 100)
    outcome(data, a1["id"], 1.0)
    outcome(data, a1["id"], 5.0)  # a racing writer's copy: ignored
    agg = ledger.aggregate(ledger.load(data), NOW)
    assert agg["all time"]["realized_usd"] == 1.0
    assert agg["all time"]["realized_n"] == 1
    assert agg["all time"]["pending"] == 0


def test_wasted_counts_early_and_paid_cold_only(data):
    a_early = action(data, session_id="s1", ts=NOW - 30)
    a_cold = action(data, session_id="s2", ts=NOW - 20)
    a_neg = action(data, session_id="s3", ts=NOW - 10, action="compact")
    outcome(data, a_early["id"], 0.0, kind="early")
    outcome(data, a_cold["id"], -0.4, session_id="s2", kind="paid_cold")
    outcome(data, a_neg["id"], -3.0, session_id="s3")  # negative realized
    agg = ledger.aggregate(ledger.load(data), NOW)
    all_time = agg["all time"]
    assert all_time["realized_usd"] == Decimal("-3.0")  # realized may be negative
    assert all_time["realized_n"] == 1
    assert all_time["wasted_usd"] == Decimal("-0.4")  # zero-cost early adds none
    assert all_time["wasted_n"] == 2
    assert all_time["net"] == Decimal("-3.4")


# ---------------------------------------------------------------------------
# money formatting


def test_money_rounds_half_up_and_never_shows_minus_zero():
    assert ledger._usd(2.675) == "$2.68"
    assert ledger._usd(-2.675) == "-$2.68"
    assert ledger._usd(0.125) == "$0.13"
    assert ledger._usd(1.005) == "$1.01"
    assert ledger._usd(-0.001) == "$0.00"
    assert ledger._usd(-0.0) == "$0.00"
    assert ledger._usd(1.6) == "$1.60"
    assert ledger._usd(-0.11) == "-$0.11"


# ---------------------------------------------------------------------------
# DST-safe windows


@pytest.mark.parametrize("transition", ["spring", "autumn"])
def test_seven_day_window_survives_dst(data, monkeypatch, transition):
    # Europe/Warsaw: 2026-03-29 loses an hour, 2026-10-25 has 25 hours.
    # The boundary must stay on local midnight, not drift by the shift.
    monkeypatch.setenv("TZ", "Europe/Warsaw")
    time.tzset()
    if transition == "spring":
        now_ts = time.mktime((2026, 3, 29, 12, 0, 0, 0, 0, -1))
        inside = time.mktime((2026, 3, 23, 0, 30, 0, 0, 0, -1))
        outside = time.mktime((2026, 3, 22, 23, 30, 0, 0, 0, -1))
    else:
        now_ts = time.mktime((2026, 10, 25, 12, 0, 0, 0, 0, -1))
        inside = time.mktime((2026, 10, 19, 0, 30, 0, 0, 0, -1))
        outside = time.mktime((2026, 10, 18, 23, 30, 0, 0, 0, -1))
    action(data, session_id="in", ts=inside)
    action(data, session_id="out", ts=outside)
    agg = ledger.aggregate(ledger.load(data), now_ts)
    assert agg["last 7 days"]["sessions"] == 1
    assert agg["last 7 days"]["pending"] == 1
    assert agg["all time"]["sessions"] == 2


# ---------------------------------------------------------------------------
# torn tails, cap boundaries, concurrent writers


def test_append_repairs_a_torn_tail(data):
    os.makedirs(data, exist_ok=True)
    with open(ledger.ledger_path(data), "wb") as handle:
        handle.write(b'{"type": "action", "id": "torn')  # crashed writer
    act = action(data, ts=1000.0)
    records = ledger.load(data)
    # the repaired partial line parses as nothing; the new record survives
    assert [r["id"] for r in records] == [act["id"]]


def test_cap_boundary_keeps_complete_lines(data):
    for index in range(3):
        action(data, session_id="s%d" % index, ts=1000.0 + index)
    size = os.path.getsize(ledger.ledger_path(data))
    assert len(ledger.load(data, max_bytes=size)) == 3
    # the boundary one byte in starts inside the first line: drop only that
    records = ledger.load(data, max_bytes=size - 1)
    assert [r["session_id"] for r in records] == ["s1", "s2"]


def _writer(data_dir, writer_index, count):
    for i in range(count):
        ledger.append_action(
            data_dir,
            ts=1000.0 + i,
            action="keepalive",
            session_id="w%d" % writer_index,
            cwd="/w",
            model="m",
            context_tokens=1,
            ttl_seconds=300,
            cold_at=1300.0,
            paid_usd=0.01,
            avoid_usd=0.02,
        )


def test_concurrent_appends_do_not_lose_or_merge_records(data):
    workers = [
        multiprocessing.Process(target=_writer, args=(data, index, 50))
        for index in range(4)
    ]
    for proc in workers:
        proc.start()
    for proc in workers:
        proc.join(60)
        assert proc.exitcode == 0
    records = ledger.load(data)
    assert len(records) == 200
    ids = [r["id"] for r in records]
    assert len(set(ids)) == 200
    for record in records:
        assert record["type"] == "action"
        assert record["session_id"].startswith("w")
