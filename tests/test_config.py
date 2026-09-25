import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "scripts")
)

from warmfoldlib import config  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith(("WARMFOLD_", "CLAUDE_PLUGIN_OPTION_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_DATA", raising=False)
    # Keep the user's installed config out of unit tests; the canonical-path
    # behavior has a focused test below.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


def test_defaults_when_no_overrides():
    cfg = config.load()
    assert cfg["idle_minutes"] == 30.0
    assert cfg["min_context_tokens"] == 100000.0
    assert cfg["safety_margin_minutes"] == 5.0
    assert cfg["mode"] == "auto"
    assert cfg["keepalive_hours"] == 0.0
    assert cfg["ttl_5m_policy"] == "warn"
    assert cfg["poll_seconds"] == 15.0
    assert cfg["force_ttl_seconds"] == 0.0
    assert cfg["force_channel"] == ""
    assert cfg["t3_token_path"] == ""
    assert cfg["t3_runtime_path"] == ""


def test_t3_path_overrides_are_loaded(monkeypatch):
    monkeypatch.setenv("WARMFOLD_T3_DB_PATH", "/tmp/t3/state.sqlite")
    monkeypatch.setenv("WARMFOLD_T3_ORIGIN", "http://127.0.0.1:3773")
    cfg = config.load()
    assert cfg["t3_db_path"] == "/tmp/t3/state.sqlite"
    assert cfg["t3_origin"] == "http://127.0.0.1:3773"


def test_data_dir_defaults_to_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cfg = config.load()
    assert cfg["data_dir"] == "/home/tester/.claude/plugins/data/warmfold-warmfold-local"


def test_data_dir_uses_claude_config_dir(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/claude")
    cfg = config.load()
    assert cfg["data_dir"] == "/custom/claude/plugins/data/warmfold-warmfold-local"


def test_canonical_config_is_loaded_without_plugin_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    data_dir = tmp_path / "claude" / "plugins" / "data" / "warmfold-warmfold-local"
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text(
        '{"t3_origin":"http://127.0.0.1:3773",'
        '"t3_token_path":"private/t3.json"}',
        encoding="utf-8",
    )
    cfg = config.load()
    assert cfg["t3_origin"] == "http://127.0.0.1:3773"
    assert cfg["t3_token_path"] == "private/t3.json"


def test_data_dir_from_plugin_data(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", "/data/plugins/x")
    cfg = config.load()
    assert cfg["data_dir"] == "/data/plugins/x"


def test_env_beats_plugin_option(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_IDLE_MINUTES", "10")
    monkeypatch.setenv("WARMFOLD_IDLE_MINUTES", "45")
    cfg = config.load()
    assert cfg["idle_minutes"] == 45.0


def test_plugin_option_beats_default(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_MODE", "keepalive")
    cfg = config.load()
    assert cfg["mode"] == "keepalive"


def test_config_json_beats_plugin_option(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_IDLE_MINUTES", "10")
    (tmp_path / "config.json").write_text('{"idle_minutes": 20}')
    cfg = config.load()
    assert cfg["idle_minutes"] == 20.0


def test_env_beats_config_json(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("WARMFOLD_POLL_SECONDS", "5")
    (tmp_path / "config.json").write_text('{"poll_seconds": 60}')
    cfg = config.load()
    assert cfg["poll_seconds"] == 5.0


def test_bad_values_keep_defaults(monkeypatch):
    monkeypatch.setenv("WARMFOLD_IDLE_MINUTES", "soon")
    assert config.load()["idle_minutes"] == 30.0


def test_bad_numbers_keep_defaults(monkeypatch):
    for raw in ("nan", "inf", "-inf", "1e999", "-5"):
        monkeypatch.setenv("WARMFOLD_IDLE_MINUTES", raw)
        assert config.load()["idle_minutes"] == 30.0, raw
    monkeypatch.setenv("WARMFOLD_POLL_SECONDS", "-0.5")
    assert config.load()["poll_seconds"] == 15.0
    monkeypatch.setenv("WARMFOLD_MIN_CONTEXT_TOKENS", "inf")
    assert config.load()["min_context_tokens"] == 100000.0


def test_bad_enum_values_keep_defaults(monkeypatch):
    monkeypatch.setenv("WARMFOLD_MODE", "turbo")
    assert config.load()["mode"] == "auto"
    monkeypatch.setenv("WARMFOLD_TTL_5M_POLICY", "always")
    assert config.load()["ttl_5m_policy"] == "warn"

def test_legacy_handoff_mode_migrates_to_auto(monkeypatch):
    monkeypatch.setenv("WARMFOLD_MODE", "handoff")
    assert config.load()["mode"] == "auto"


def test_invalid_override_keeps_lower_priority_value(monkeypatch, tmp_path):
    # config.json beats the plugin option, but an invalid config.json value
    # must not wipe the plugin option below it
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_IDLE_MINUTES", "10")
    (tmp_path / "config.json").write_text('{"idle_minutes": -3}')
    cfg = config.load()
    assert cfg["idle_minutes"] == 10.0


def test_malformed_config_json_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text("{not json")
    cfg = config.load()
    assert cfg["idle_minutes"] == 30.0


def test_unknown_keys_in_config_json_are_dropped(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PLUGIN_DATA", str(tmp_path))
    (tmp_path / "config.json").write_text('{"no_such_key": 1, "mode": "warn"}')
    cfg = config.load()
    assert "no_such_key" not in cfg
    assert cfg["mode"] == "warn"


def test_tilde_in_data_dir_env_expands(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("WARMFOLD_DATA_DIR", "~/.cache/cc")
    cfg = config.load()
    assert cfg["data_dir"] == "/home/tester/.cache/cc"
