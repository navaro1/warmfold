"""Configuration loading.

Precedence, first match wins:
1. Environment ``WARMFOLD_<KEY>``.
2. ``$CLAUDE_PLUGIN_DATA/config.json``.
3. Plugin option environment ``CLAUDE_PLUGIN_OPTION_<KEY>``.
4. Default.
"""

import json
import math
import os

DEFAULTS = {
    "idle_minutes": 30.0,
    "min_context_tokens": 100000.0,
    "safety_margin_minutes": 5.0,
    "mode": "auto",
    "keepalive_hours": 0.0,
    "ttl_5m_policy": "warn",
    "guard": True,
    "guard_min_usd": 1.0,
    "guard_ack_seconds": 120.0,
    "poll_seconds": 15.0,
    "pane_markers": "❯ ",
    "data_dir": "",
    "force_ttl_seconds": 0.0,
    "force_channel": "",
    # Native T3 integration. Empty values select the installed local defaults
    # in t3.py/t3_auth.py; these are override points for tests and custom T3
    # homes, not secrets.
    "t3_app_path": "",
    "t3_server_bin": "",
    "t3_base_dir": "",
    "t3_db_path": "",
    "t3_runtime_path": "",
    "t3_origin": "",
    "t3_token_path": "",
}

_NUMBER_KEYS = frozenset(
    k for k, v in DEFAULTS.items() if isinstance(v, float)
)
_BOOL_KEYS = frozenset(k for k, v in DEFAULTS.items() if isinstance(v, bool))
_ENUM_KEYS = {
    "mode": frozenset(("auto", "compact", "handoff", "keepalive", "warn")),
    "ttl_5m_policy": frozenset(("warn", "off", "compact_at_4m")),
}
_TRUE_WORDS = frozenset(("1", "true", "yes"))
_FALSE_WORDS = frozenset(("0", "false", "no"))


class _Invalid(object):
    """Sentinel: the override value is rejected; keep the lower priority."""


_INVALID = _Invalid()


def default_data_dir():
    """Return the data directory when no explicit override exists."""
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA") or ""
    if plugin_data:
        return plugin_data
    config_home = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude"
    )
    return os.path.join(
        os.path.expanduser(config_home), "plugins", "data", "warmfold-warmfold-local"
    )


def _coerce(key, value):
    """Convert a raw string (or JSON value) to the key's type.

    Returns _INVALID when the value does not fit; the caller keeps the
    lower-priority value.
    """
    if key in _NUMBER_KEYS:
        if isinstance(value, bool):
            return _INVALID
        try:
            number = float(value)
        except (TypeError, ValueError):
            return _INVALID
        if not math.isfinite(number) or number < 0:
            return _INVALID
        return number
    if key in _BOOL_KEYS:
        if isinstance(value, bool):
            return value
        if value is None:
            return _INVALID
        text = str(value).strip().lower()
        if text in _TRUE_WORDS:
            return True
        if text in _FALSE_WORDS:
            return False
        return _INVALID
    if key in _ENUM_KEYS:
        if value is None:
            return _INVALID
        text = str(value).strip().lower()
        return text if text in _ENUM_KEYS[key] else _INVALID
    if value is None:
        return _INVALID
    return str(value)


def _apply(cfg, values):
    for key in DEFAULTS:
        if key in values:
            coerced = _coerce(key, values[key])
            if coerced is not _INVALID:
                cfg[key] = coerced


def load():
    """Load the configuration with the precedence rules from DESIGN.md."""
    cfg = dict(DEFAULTS)
    cfg["data_dir"] = default_data_dir()

    # 3. plugin option environment
    for key in DEFAULTS:
        raw = os.environ.get("CLAUDE_PLUGIN_OPTION_" + key.upper())
        if raw:
            coerced = _coerce(key, raw)
            if coerced is not _INVALID:
                cfg[key] = coerced

    # 2. $CLAUDE_PLUGIN_DATA/config.json
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA") or ""
    # Manual commands often run outside Claude's hook environment, where
    # CLAUDE_PLUGIN_DATA is absent. Read the canonical installed data dir in
    # that case, while an explicit WARMFOLD_DATA_DIR remains an isolated test
    # override and must not inherit the installed config.
    config_data = plugin_data or (
        "" if os.environ.get("WARMFOLD_DATA_DIR") else cfg["data_dir"]
    )
    if config_data:
        path = os.path.join(config_data, "config.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                values = json.load(handle)
            if isinstance(values, dict):
                _apply(cfg, values)
        except (OSError, ValueError):
            pass

    # 1. WARMFOLD_* environment
    for key in DEFAULTS:
        raw = os.environ.get("WARMFOLD_" + key.upper())
        if raw:
            coerced = _coerce(key, raw)
            if coerced is not _INVALID:
                cfg[key] = coerced

    # ``handoff`` was supported by 0.1.2. Keep old config files readable, but
    # map the retired mode to automatic native compaction.
    if cfg.get("mode") == "handoff":
        cfg["mode"] = "auto"
    if cfg["data_dir"]:
        cfg["data_dir"] = os.path.expanduser(str(cfg["data_dir"]))
    return cfg
