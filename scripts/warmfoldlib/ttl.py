"""Prompt-cache TTL detection.

The TTL of the current request follows from the Claude Code configuration;
it is not a guess. ``detect_ttl`` resolves it in a fixed order, first match
wins, and returns the value with the name of the source that decided it:

1. transcript evidence (the newest cache write with a valid split)
2. ``FORCE_PROMPT_CACHING_5M`` (a true value)
3. ``CLAUDE_CODE_PROMPT_CACHE_TTL`` (exactly ``5m`` or ``1h``)
4. the ``promptCacheTtl`` setting (project scopes override the user scope)
5. ``ENABLE_PROMPT_CACHING_1H`` (a true value)
6. the auth mode: api key or cloud provider default to 5 minutes,
   a subscription defaults to 1 hour, and an unknown setup assumes
   5 minutes (the safe assumption).

Boolean variables accept the words Claude Code accepts: ``1``, ``true``,
``yes``, and ``on``, in any letter case, with surrounding spaces. The TTL
words accept only the exact spellings ``5m`` and ``1h``.

The ``project_dir`` argument is the session's project directory, from the
event payload or the saved state. The detector reads ``os.getcwd()`` only
when the caller has no directory at all.

The credentials file is opened in binary mode; at most 1 MB plus one byte
is read, and a file with a byte beyond the limit is rejected. Only the
presence of one key matters; no value from the file is returned, logged,
or printed. Any unexpected failure inside the detector returns the safe
300-second default.
"""

import json
import os

_TTL_WORDS = {"5m": 300, "1h": 3600}
_TRUTHY = frozenset(("1", "true", "yes", "on"))
_MAX_CREDENTIALS_BYTES = 1024 * 1024
_CLOUD_VARS = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def detect_ttl(info, env=os.environ, config_dir=None, project_dir=None):
    """Return ``(ttl_seconds, source)`` for the session described by info.

    ``config_dir`` overrides the user config directory, and ``project_dir``
    is the session's project directory; tests pass both explicitly. An
    unexpected error anywhere in the detection returns the safe default.
    """
    try:
        return _detect_ttl(info, env, config_dir, project_dir)
    except Exception:
        return 300, "detection failed; assume 5m"


def _detect_ttl(info, env, config_dir, project_dir):
    # 1. Transcript evidence reflects reality, including the drop to 5m
    #    when usage credits run out.
    if info is not None and info.ttl_seconds:
        return info.ttl_seconds, "transcript"

    # 2. Explicit 5m override.
    if _env_flag(env, "FORCE_PROMPT_CACHING_5M"):
        return 300, "env FORCE_PROMPT_CACHING_5M"

    # 3. Explicit TTL selection; only the exact words 5m and 1h count.
    value = _ttl_word(env.get("CLAUDE_CODE_PROMPT_CACHE_TTL"))
    if value:
        return value, "env CLAUDE_CODE_PROMPT_CACHE_TTL"

    user_dir = _user_config_dir(env, config_dir)
    settings_chain = _settings_chain(user_dir, project_dir)

    # 4. promptCacheTtl setting; project scopes override the user scope.
    for values in settings_chain:
        value = _ttl_word(values.get("promptCacheTtl"))
        if value:
            return value, "setting promptCacheTtl"

    # 5. Explicit 1h opt-in.
    if _env_flag(env, "ENABLE_PROMPT_CACHING_1H"):
        return 3600, "env ENABLE_PROMPT_CACHING_1H"

    # 6. Auth mode default.
    mode = _auth_mode(env, settings_chain, user_dir)
    if mode == "subscription":
        return 3600, "subscription"
    if mode == "api key":
        return 300, "api key"
    if mode == "cloud provider":
        return 300, "cloud provider"
    return 300, "unknown auth; assume 5m"


def _ttl_word(value):
    """Return 300 or 3600 for the exact strings "5m" and "1h"; else None.

    Claude Code reads no other spelling, so the detector folds nothing:
    no letter case change, no space removal.
    """
    if not isinstance(value, str):
        return None
    return _TTL_WORDS.get(value)


def _env_flag(env, name):
    """True when the variable holds a truthy word, as Claude Code reads it."""
    value = env.get(name)
    if not isinstance(value, str):
        return False
    return value.strip().lower() in _TRUTHY


def _user_config_dir(env, config_dir):
    """The user-level Claude config directory."""
    if config_dir:
        return config_dir
    from_env = env.get("CLAUDE_CONFIG_DIR")
    if from_env:
        return from_env
    return os.path.join(os.path.expanduser("~"), ".claude")


def _project_dir(project_dir):
    """The session's project directory, or "" when nothing is known."""
    if project_dir:
        return project_dir
    try:
        return os.getcwd()
    except OSError:
        return ""


def _settings_chain(user_dir, project_dir):
    """Settings dicts, highest precedence first: project local, project
    shared, user. Missing or corrupt files are skipped."""
    chain = []
    root = _project_dir(project_dir)
    if root:
        for path in (
            os.path.join(root, ".claude", "settings.local.json"),
            os.path.join(root, ".claude", "settings.json"),
        ):
            values = _read_json(path)
            if isinstance(values, dict):
                chain.append(values)
    values = _read_json(os.path.join(user_dir, "settings.json"))
    if isinstance(values, dict):
        chain.append(values)
    return chain


def _auth_mode(env, settings_chain, user_dir):
    """Classify the auth setup; "unknown" when nothing is recognizable."""
    if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return "api key"
    for values in settings_chain:
        if values.get("apiKeyHelper"):
            return "api key"
    for name in _CLOUD_VARS:
        if _env_flag(env, name):
            return "cloud provider"
    if _has_subscription(user_dir):
        return "subscription"
    return "unknown"


def _has_subscription(user_dir):
    """True only when the credentials file holds the claudeAiOauth key.

    The read is bounded: binary mode, 1 MB plus one byte, and a file with
    a byte beyond the limit is rejected without parsing. Only the presence
    of the key matters; no value from the file leaves this function.
    """
    path = os.path.join(user_dir, ".credentials.json")
    try:
        with open(path, "rb") as handle:
            blob = handle.read(_MAX_CREDENTIALS_BYTES + 1)
    except OSError:
        return False
    if len(blob) > _MAX_CREDENTIALS_BYTES:
        return False
    try:
        values = json.loads(blob.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    return isinstance(values, dict) and "claudeAiOauth" in values


def _read_json(path):
    """Parse a settings file; any failure returns None."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        return None
