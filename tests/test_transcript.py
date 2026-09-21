import builtins
import json
import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts")
)

from warmfoldlib import transcript  # noqa: E402

import pytest  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fxt(name):
    return os.path.join(FIXTURES, name)


@pytest.fixture(autouse=True)
def clear_cache():
    transcript._CACHE.clear()
    yield
    transcript._CACHE.clear()


def test_parse_ts_fractional():
    assert transcript.parse_ts("2026-09-21T09:00:05.123Z") == 1789981205.123


def test_parse_ts_no_fraction():
    assert transcript.parse_ts("2026-09-21T09:00:05Z") == 1789981205.0


def test_parse_ts_bad():
    assert transcript.parse_ts("2026-13-45T99:00:00Z") is None
    assert transcript.parse_ts("") is None
    assert transcript.parse_ts(None) is None
    assert transcript.parse_ts("yesterday") is None


def test_read_normal_1h():
    info = transcript.read_transcript(fxt("normal_1h.jsonl"))
    assert info.context_tokens == 400000
    assert info.model == "claude-sonnet-5"
    assert info.ttl_seconds == 3600
    assert info.last_request_start == transcript.parse_ts("2026-09-21T09:00:20.000Z")
    assert info.last_assistant_at == transcript.parse_ts("2026-09-21T09:00:30.000Z")
    assert info.caching_observed is True


def test_read_5m_session():
    info = transcript.read_transcript(fxt("session_5m.jsonl"))
    assert info.ttl_seconds == 300
    assert info.model == "claude-opus-5"
    assert info.context_tokens == 100000


def test_compact_boundary_with_post_tokens():
    info = transcript.read_transcript(fxt("compact_post.jsonl"))
    assert info.context_tokens == 12000
    assert info.ttl_seconds == 3600
    assert info.model == "claude-sonnet-5"
    assert info.last_assistant_at == transcript.parse_ts("2026-09-21T11:00:10.000Z")


def test_compact_boundary_without_post_tokens_is_unknown():
    info = transcript.read_transcript(fxt("compact_nopost.jsonl"))
    assert info.context_tokens is None
    assert info.ttl_seconds == 3600


def test_malformed_lines_are_skipped():
    info = transcript.read_transcript(fxt("malformed.jsonl"))
    # input 10 + cache_creation 40000 + cache_read 60000
    assert info.context_tokens == 100010
    assert info.model == "claude-sonnet-5"
    assert info.ttl_seconds == 3600
    assert info.last_assistant_at == transcript.parse_ts("2026-09-21T12:00:10.000Z")
    # the boundary line sits before the usage line in scan order, so it is
    # in the past and ignored; with no valid user line the request start
    # falls back to the newest assistant timestamp.
    assert info.last_request_start == transcript.parse_ts("2026-09-21T12:00:10.000Z")


def test_empty_file():
    info = transcript.read_transcript(fxt("empty.jsonl"))
    assert info.context_tokens is None
    assert info.model is None
    assert info.ttl_seconds is None
    assert info.last_request_start == 0.0
    assert info.last_assistant_at == 0.0
    assert info.caching_observed is False


def test_missing_file():
    info = transcript.read_transcript("/no/such/file.jsonl")
    assert info.context_tokens is None
    assert info.ttl_seconds is None


def test_ttl_search_back_finds_5m_write():
    info = transcript.read_transcript(fxt("ttl_search.jsonl"))
    assert info.ttl_seconds == 300
    assert info.context_tokens == 20000
    assert info.last_request_start == transcript.parse_ts("2026-09-21T08:00:20.000Z")
    assert info.last_assistant_at == transcript.parse_ts("2026-09-21T08:00:25.000Z")


def test_ttl_unknown_when_newest_write_has_no_split():
    info = transcript.read_transcript(fxt("ttl_split_missing.jsonl"))
    # The newest cache write has no ephemeral split, so the TTL is unknown;
    # the older line's 1 h write must not leak through.
    assert info.ttl_seconds is None
    assert info.context_tokens == 63200
    assert info.last_request_start == transcript.parse_ts("2026-09-21T10:00:00.000Z")


def test_cache_hit_avoids_reread(monkeypatch, tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-21T09:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 1,
                        "cache_creation_input_tokens": 2,
                        "cache_read_input_tokens": 3,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 2,
                            "ephemeral_5m_input_tokens": 0,
                        },
                    },
                },
            }
        )
        + "\n"
    )
    first = transcript.read_transcript(str(path))

    def boom(*args, **kwargs):
        raise AssertionError("cache miss: file was read again")

    monkeypatch.setattr(builtins, "open", boom)
    second = transcript.read_transcript(str(path))
    assert second == first
    assert second.context_tokens == 6


def test_cache_invalidated_by_size_change(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-21T09:00:00.000Z",
                "message": {
                    "model": "claude-haiku-4-5",
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 10,
                        "cache_read_input_tokens": 0,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 0,
                            "ephemeral_5m_input_tokens": 10,
                        },
                    },
                },
            }
        )
        + "\n"
    )
    first = transcript.read_transcript(str(path))
    assert first.ttl_seconds == 300
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-21T09:01:00.000Z",
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {
                            "input_tokens": 700,
                            "cache_creation_input_tokens": 300,
                            "cache_read_input_tokens": 19000,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 300,
                                "ephemeral_5m_input_tokens": 0,
                            },
                        },
                    },
                }
            )
            + "\n"
        )
    second = transcript.read_transcript(str(path))
    assert second.context_tokens == 20000
    assert second.model == "claude-sonnet-5"
    assert second.ttl_seconds == 3600
    # no user line exists, so the request start falls back to the newest
    # assistant timestamp
    assert second.last_request_start == transcript.parse_ts("2026-09-21T09:01:00.000Z")


def test_backward_scan_over_large_file(tmp_path):
    path = tmp_path / "big.jsonl"
    filler = json.dumps({"type": "summary", "summary": "x" * 120}) + "\n"
    huge_payload = json.dumps(
        {
            "type": "summary",
            "summary": "y" * 200000,
        }
    )  # one line much larger than a read block
    with open(path, "w", encoding="utf-8") as handle:
        for _ in range(3000):
            handle.write(filler)
        handle.write(huge_payload + "\n")
        handle.write(
            json.dumps(
                {"type": "user", "timestamp": "2026-09-21T09:00:00.000Z"}
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-21T09:00:30.000Z",
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {
                            "input_tokens": 500,
                            "cache_creation_input_tokens": 1500,
                            "cache_read_input_tokens": 8000,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 1500,
                                "ephemeral_5m_input_tokens": 0,
                            },
                        },
                    },
                }
            )
            + "\n"
        )
    info = transcript.read_transcript(str(path))
    assert info.context_tokens == 10000
    assert info.ttl_seconds == 3600
    assert info.last_request_start == transcript.parse_ts("2026-09-21T09:00:00.000Z")
    assert info.last_assistant_at == transcript.parse_ts("2026-09-21T09:00:30.000Z")


def test_missing_fields_mean_unknown(tmp_path):
    path = tmp_path / "sparse.jsonl"
    path.write_text(
        json.dumps({"type": "assistant", "timestamp": "2026-09-21T09:00:00Z"}) + "\n"
    )
    info = transcript.read_transcript(str(path))
    assert info.context_tokens is None
    assert info.model is None
    assert info.ttl_seconds is None
    assert info.last_assistant_at == transcript.parse_ts("2026-09-21T09:00:00Z")


def test_partial_usage_gives_unknown_context(tmp_path):
    path = tmp_path / "partial.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-21T09:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 500,
                        "cache_read_input_tokens": 8000,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 0,
                            "ephemeral_5m_input_tokens": 0,
                        },
                    },
                },
            }
        )
        + "\n"
    )
    info = transcript.read_transcript(str(path))
    # cache_creation_input_tokens is missing: the context is unknown,
    # never an undercount that reads as zero.
    assert info.context_tokens is None
    assert info.ttl_seconds is None


def test_non_numeric_usage_field_gives_unknown_context(tmp_path):
    path = tmp_path / "textual.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-09-21T09:00:00.000Z",
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": "500",
                        "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": 2000,
                    },
                },
            }
        )
        + "\n"
    )
    info = transcript.read_transcript(str(path))
    assert info.context_tokens is None


def test_scan_continues_past_a_huge_user_line(tmp_path):
    # A user line larger than the old 4 MB cap must not stop the scan
    # before the request start is found.
    path = tmp_path / "huge.jsonl"
    big_text = "x" * (5 * 1024 * 1024)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-09-21T09:00:00.000Z",
                    "message": {"content": big_text},
                }
            )
            + "\n"
        )
        handle.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-09-21T09:00:30.000Z",
                    "message": {
                        "model": "claude-sonnet-5",
                        "usage": {
                            "input_tokens": 500,
                            "cache_creation_input_tokens": 1500,
                            "cache_read_input_tokens": 8000,
                            "cache_creation": {
                                "ephemeral_1h_input_tokens": 1500,
                                "ephemeral_5m_input_tokens": 0,
                            },
                        },
                    },
                }
            )
            + "\n"
        )
    info = transcript.read_transcript(str(path))
    assert info.context_tokens == 10000
    assert info.ttl_seconds == 3600
    # the user line, not the assistant fallback, anchors the request start
    assert info.last_request_start == transcript.parse_ts("2026-09-21T09:00:00.000Z")


def test_read_transcript_none_path():
    assert transcript.read_transcript(None) == transcript.TranscriptInfo()
    assert transcript.read_transcript("") == transcript.TranscriptInfo()
