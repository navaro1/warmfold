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
    "handoff_autoload": "clear",
    "handoff_max_age_hours": 24.0,
    "poll_seconds": 15.0,
    "pane_markers": "❯ ",
    "data_dir": "",
    "force_ttl_seconds": 0.0,
    "force_channel": "",
}

_NUMBER_KEYS = frozenset(
    k for k, v in DEFAULTS.items() if isinstance(v, float)
)
_BOOL_KEYS = frozenset(k for k, v in DEFAULTS.items() if isinstance(v, bool))
_ENUM_KEYS = {
    "mode": frozenset(("auto", "compact", "handoff", "keepalive", "warn")),
    "ttl_5m_policy": frozenset(("warn", "off", "compact_at_4m")),
    "handoff_autoload": frozenset(("off", "clear", "startup", "clear+startup")),
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
    return os.path.join(
        os.path.expanduser("~"), ".claude", "warmfold"
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
    if plugin_data:
        path = os.path.join(plugin_data, "config.json")
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

    if cfg["data_dir"]:
        cfg["data_dir"] = os.path.expanduser(str(cfg["data_dir"]))
    return cfg
