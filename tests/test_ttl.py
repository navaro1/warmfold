import json
import os
import sys
import time

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts")
)

from warmfoldlib import transcript, ttl  # noqa: E402

import pytest  # noqa: E402

T0 = 1789974000.0  # 2026-09-21T07:00:00Z


def iso(t):
    parts = time.gmtime(t)
    return "%04d-%02d-%02dT%02d:%02d:%02d.000Z" % (
        parts.tm_year, parts.tm_mon, parts.tm_mday,
        parts.tm_hour, parts.tm_min, parts.tm_sec,
    )


def info(ttl_seconds=None):
    return transcript.TranscriptInfo(ttl_seconds=ttl_seconds)


EMPTY = {}

TRUTHY = ("1", "true", "True", "TRUE", " yes ", "on", "On")
FALSY = ("0", "false", "no", "off", "", "   ", "maybe")

# exactly at the 1 MB limit: a JSON object padded to the byte
_LIMIT = 1024 * 1024
_CRED_PREFIX = '{"claudeAiOauth": 1, "pad": "'
_CRED_SUFFIX = '"}'
_PAD_AT_LIMIT = _LIMIT - (len(_CRED_PREFIX) + len(_CRED_SUFFIX))


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def write_json(path, obj):
    write_text(path, json.dumps(obj))


def user_settings(tmp_path, obj):
    write_json(os.path.join(str(tmp_path), "settings.json"), obj)


def project_setting(tmp_path, obj, name="settings.json"):
    cwd = tmp_path / "proj"
    write_json(str(cwd / ".claude" / name), obj)
    return str(cwd)


def credentials(tmp_path, obj):
    write_json(os.path.join(str(tmp_path), ".credentials.json"), obj)


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch):
    for name in (
        "FORCE_PROMPT_CACHING_5M",
        "CLAUDE_CODE_PROMPT_CACHE_TTL",
        "ENABLE_PROMPT_CACHING_1H",
        "CLAUDE_CONFIG_DIR",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        monkeypatch.delenv(name, raising=False)


# rule 1: transcript evidence


def test_transcript_evidence_wins(tmp_path):
    env = {"FORCE_PROMPT_CACHING_5M": "1", "CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}
    value, source = ttl.detect_ttl(info(300), env, config_dir=str(tmp_path))
    assert (value, source) == (300, "transcript")
    value, source = ttl.detect_ttl(info(3600), env, config_dir=str(tmp_path))
    assert (value, source) == (3600, "transcript")


# rule 2: FORCE_PROMPT_CACHING_5M


@pytest.mark.parametrize("value", TRUTHY)
def test_force_prompt_caching_5m_truthy_words(tmp_path, value):
    env = {"FORCE_PROMPT_CACHING_5M": value, "ENABLE_PROMPT_CACHING_1H": "1"}
    result = ttl.detect_ttl(info(None), env, config_dir=str(tmp_path))
    assert result == (300, "env FORCE_PROMPT_CACHING_5M")


@pytest.mark.parametrize("value", FALSY)
def test_force_prompt_caching_5m_falsy_words(tmp_path, value):
    env = {"FORCE_PROMPT_CACHING_5M": value}
    result = ttl.detect_ttl(info(None), env, config_dir=str(tmp_path))
    assert result == (300, "unknown auth; assume 5m")


# rule 3: CLAUDE_CODE_PROMPT_CACHE_TTL


@pytest.mark.parametrize(
    "word,expected", (("5m", 300), ("1h", 3600))
)
def test_claude_code_ttl_env_exact_words(tmp_path, word, expected):
    env = {"CLAUDE_CODE_PROMPT_CACHE_TTL": word}
    value, source = ttl.detect_ttl(info(None), env, config_dir=str(tmp_path))
    assert (value, source) == (expected, "env CLAUDE_CODE_PROMPT_CACHE_TTL")


@pytest.mark.parametrize("word", (" 1H ", "1H", "5M", "1 h", "2h", "3600"))
def test_claude_code_ttl_env_rejects_other_spellings(tmp_path, word):
    # Claude Code ignores every other spelling, so the detector must fall
    # through; here it lands on the explicit 1h opt-in below it.
    env = {"CLAUDE_CODE_PROMPT_CACHE_TTL": word, "ENABLE_PROMPT_CACHING_1H": "1"}
    value, source = ttl.detect_ttl(info(None), env, config_dir=str(tmp_path))
    assert (value, source) == (3600, "env ENABLE_PROMPT_CACHING_1H")


# rule 4: promptCacheTtl setting


def test_setting_user_scope(tmp_path):
    user_settings(tmp_path, {"promptCacheTtl": "1h"})
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (3600, "setting promptCacheTtl")


def test_project_dir_argument_selects_project_scope(tmp_path):
    # The caller's project_dir is used directly; os.getcwd() stays out of
    # the decision (the repo cwd holds no settings in this test).
    cwd = project_setting(tmp_path, {"promptCacheTtl": "5m"})
    user_settings(tmp_path, {"promptCacheTtl": "1h"})
    value, source = ttl.detect_ttl(
        info(None), EMPTY, config_dir=str(tmp_path), project_dir=cwd
    )
    assert (value, source) == (300, "setting promptCacheTtl")


def test_project_setting_overrides_user(tmp_path):
    user_settings(tmp_path, {"promptCacheTtl": "1h"})
    cwd = project_setting(tmp_path, {"promptCacheTtl": "5m"})
    value, source = ttl.detect_ttl(
        info(None), EMPTY, config_dir=str(tmp_path), project_dir=cwd
    )
    assert (value, source) == (300, "setting promptCacheTtl")


def test_local_setting_overrides_project(tmp_path):
    cwd = project_setting(tmp_path, {"promptCacheTtl": "5m"})
    write_json(
        os.path.join(cwd, ".claude", "settings.local.json"),
        {"promptCacheTtl": "1h"},
    )
    value, source = ttl.detect_ttl(
        info(None), EMPTY, config_dir=str(tmp_path), project_dir=cwd
    )
    assert (value, source) == (3600, "setting promptCacheTtl")


def test_invalid_project_setting_falls_to_user(tmp_path):
    user_settings(tmp_path, {"promptCacheTtl": "1h"})
    cwd = project_setting(tmp_path, {"promptCacheTtl": "someday"})
    value, source = ttl.detect_ttl(
        info(None), EMPTY, config_dir=str(tmp_path), project_dir=cwd
    )
    assert (value, source) == (3600, "setting promptCacheTtl")


def test_corrupt_settings_file_is_skipped(tmp_path):
    user_settings(tmp_path, {"promptCacheTtl": "5m"})
    cwd = tmp_path / "proj"
    write_text(
        str(cwd / ".claude" / "settings.local.json"),
        "{broken",
    )
    value, source = ttl.detect_ttl(
        info(None), EMPTY, config_dir=str(tmp_path), project_dir=str(cwd)
    )
    assert (value, source) == (300, "setting promptCacheTtl")


def test_deeply_nested_settings_file_is_skipped(tmp_path):
    user_settings(tmp_path, {"promptCacheTtl": "5m"})
    cwd = tmp_path / "proj"
    deep = "[" * 60000 + "]" * 60000
    write_text(str(cwd / ".claude" / "settings.local.json"), deep)
    value, source = ttl.detect_ttl(
        info(None), EMPTY, config_dir=str(tmp_path), project_dir=str(cwd)
    )
    assert (value, source) == (300, "setting promptCacheTtl")


def test_getcwd_is_the_last_resort_project_source(tmp_path, monkeypatch):
    user_settings(tmp_path, {"promptCacheTtl": "5m"})
    cwd = tmp_path / "proj"
    write_json(str(cwd / ".claude" / "settings.json"), {"promptCacheTtl": "1h"})
    monkeypatch.setattr(os, "getcwd", lambda: str(cwd))
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (3600, "setting promptCacheTtl")


def test_failed_getcwd_skips_project_scopes(tmp_path, monkeypatch):
    user_settings(tmp_path, {"promptCacheTtl": "1h"})

    def gone():
        raise OSError("current directory was deleted")

    monkeypatch.setattr(os, "getcwd", gone)
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (3600, "setting promptCacheTtl")


def test_subagent_controls_do_not_affect_detection(tmp_path):
    user_settings(tmp_path, {"subagentPromptCacheTtl": "1h"})
    env = {"CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": "1h"}
    value, source = ttl.detect_ttl(info(None), env, config_dir=str(tmp_path))
    assert (value, source) == (300, "unknown auth; assume 5m")


def test_setting_beats_1h_opt_in(tmp_path):
    user_settings(tmp_path, {"promptCacheTtl": "5m"})
    value, source = ttl.detect_ttl(
        info(None), {"ENABLE_PROMPT_CACHING_1H": "1"}, config_dir=str(tmp_path)
    )
    assert (value, source) == (300, "setting promptCacheTtl")


# rule 5: ENABLE_PROMPT_CACHING_1H


@pytest.mark.parametrize("value", TRUTHY)
def test_enable_prompt_caching_1h_truthy_words(tmp_path, value):
    value_found, source = ttl.detect_ttl(
        info(None), {"ENABLE_PROMPT_CACHING_1H": value}, config_dir=str(tmp_path)
    )
    assert (value_found, source) == (3600, "env ENABLE_PROMPT_CACHING_1H")


# rule 6: auth mode


def test_api_key_env_beats_subscription_credentials(tmp_path):
    credentials(tmp_path, {"claudeAiOauth": {"accessToken": "x"}})
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        value, source = ttl.detect_ttl(
            info(None), {name: "secret"}, config_dir=str(tmp_path)
        )
        assert (value, source) == (300, "api key"), name


def test_api_key_helper_setting(tmp_path):
    user_settings(tmp_path, {"apiKeyHelper": "/bin/helper"})
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (300, "api key")


@pytest.mark.parametrize("value", TRUTHY)
def test_cloud_provider_env_truthy_words(tmp_path, value):
    credentials(tmp_path, {"claudeAiOauth": {}})
    for name in (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        value_found, source = ttl.detect_ttl(
            info(None), {name: value}, config_dir=str(tmp_path)
        )
        assert (value_found, source) == (300, "cloud provider"), name


@pytest.mark.parametrize("value", FALSY)
def test_cloud_provider_env_falsy_words(tmp_path, value):
    # A falsy cloud value must not win over the subscription credentials.
    credentials(tmp_path, {"claudeAiOauth": {}})
    env = {"CLAUDE_CODE_USE_BEDROCK": value}
    result = ttl.detect_ttl(info(None), env, config_dir=str(tmp_path))
    assert result == (3600, "subscription")


def test_subscription_credentials(tmp_path):
    credentials(tmp_path, {"claudeAiOauth": {}})
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (3600, "subscription")


def test_config_dir_env_discovers_credentials(tmp_path):
    credentials(tmp_path, {"claudeAiOauth": {}})
    value, source = ttl.detect_ttl(
        info(None), {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    )
    assert (value, source) == (3600, "subscription")


def test_config_dir_env_discovers_user_settings(tmp_path):
    user_settings(tmp_path, {"promptCacheTtl": "5m"})
    value, source = ttl.detect_ttl(
        info(None), {"CLAUDE_CONFIG_DIR": str(tmp_path)}
    )
    assert (value, source) == (300, "setting promptCacheTtl")


def test_credentials_without_oauth_key(tmp_path):
    credentials(tmp_path, {"someService": {"token": "x"}})
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (300, "unknown auth; assume 5m")


def test_corrupt_credentials_are_ignored(tmp_path):
    write_text(
        os.path.join(str(tmp_path), ".credentials.json"), "{nope"
    )
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (300, "unknown auth; assume 5m")


def test_deeply_nested_credentials_are_ignored(tmp_path):
    deep = "[" * 60000 + "]" * 60000
    write_text(os.path.join(str(tmp_path), ".credentials.json"), deep)
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (300, "unknown auth; assume 5m")


def test_credentials_at_the_size_limit_are_read(tmp_path):
    blob = _CRED_PREFIX + "x" * _PAD_AT_LIMIT + _CRED_SUFFIX
    path = os.path.join(str(tmp_path), ".credentials.json")
    write_text(path, blob)
    assert os.path.getsize(path) == _LIMIT
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (3600, "subscription")


def test_oversized_credentials_are_not_read(tmp_path):
    blob = _CRED_PREFIX + "x" * (_PAD_AT_LIMIT + 1) + _CRED_SUFFIX
    path = os.path.join(str(tmp_path), ".credentials.json")
    write_text(path, blob)
    assert os.path.getsize(path) == _LIMIT + 1
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (300, "unknown auth; assume 5m")


def test_missing_credentials_and_empty_env(tmp_path):
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (300, "unknown auth; assume 5m")


# robustness


def test_unexpected_detection_failure_returns_safe_default(
    tmp_path, monkeypatch
):
    user_settings(tmp_path, {"promptCacheTtl": "1h"})

    def broken(user_dir, project_dir):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(ttl, "_settings_chain", broken)
    value, source = ttl.detect_ttl(info(None), EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (300, "detection failed; assume 5m")


def test_transcript_frontmatter_cache_ttl_is_not_evidence(tmp_path):
    # A cacheTtl in the transcript frontmatter is not usage evidence; the
    # detector must fall through to the environment and settings.
    path = str(tmp_path / "t.jsonl")
    lines = [
        '---\ncacheTtl: "1h"\n---\n',
        json.dumps({"type": "user", "timestamp": iso(T0 - 100)}) + "\n",
        json.dumps(
            {
                "type": "assistant",
                "timestamp": iso(T0 - 90),
                "message": {
                    "model": "claude-sonnet-5",
                    "usage": {
                        "input_tokens": 0,
                        "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": 399000,
                        "cache_creation": {
                            "ephemeral_1h_input_tokens": 0,
                            "ephemeral_5m_input_tokens": 0,
                        },
                    },
                },
            }
        )
        + "\n",
    ]
    with open(path, "w", encoding="utf-8") as handle:
        handle.writelines(lines)
    parsed = transcript.read_transcript(path)
    assert parsed.ttl_seconds is None
    value, source = ttl.detect_ttl(parsed, EMPTY, config_dir=str(tmp_path))
    assert (value, source) == (300, "unknown auth; assume 5m")
