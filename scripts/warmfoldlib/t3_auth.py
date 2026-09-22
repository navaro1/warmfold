"""Native T3 Code authentication enrollment.

The T3 desktop bundle ships its supported CLI inside the app.  Running that
CLI through Electron's own binary issues a short lived pairing credential;
the credential is exchanged at T3's documented OAuth endpoint for a scoped
bearer token.  Enrollment is explicit: this module never creates a pairing
credential merely because a dispatch is missing a token.

Only the two orchestration scopes needed by warmfold are requested.  Tokens
are written atomically with mode 0600 and are never included in log messages.
"""

from __future__ import annotations

import datetime as _datetime
import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Optional
from urllib import error as _urlerror
from urllib import request as _urlrequest
from urllib.parse import urlencode, urlsplit


ACCEPTED = "accepted"
UNCERTAIN = "uncertain"
REJECTED = "rejected"

REQUIRED_SCOPES = frozenset(("orchestration:read", "orchestration:operate"))
PAIRING_TTL = "5m"
TOKEN_REFRESH_MARGIN_SECONDS = 60
DEFAULT_TOKEN_PATH = os.path.expanduser(
    "~/.claude/plugins/data/warmfold-warmfold-local/t3-bearer.json"
)
DEFAULT_APP_PATH = "/Applications/T3 Code (Nightly).app/Contents/MacOS/T3 Code (Nightly)"
DEFAULT_SERVER_BIN = "/Applications/T3 Code (Nightly).app/Contents/Resources/app.asar/apps/server/dist/bin.mjs"
STABLE_APP_PATH = "/Applications/T3 Code.app/Contents/MacOS/T3 Code"
STABLE_SERVER_BIN = "/Applications/T3 Code.app/Contents/Resources/app.asar/apps/server/dist/bin.mjs"
TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
BOOTSTRAP_TOKEN_TYPE = "urn:t3:params:oauth:token-type:environment-bootstrap"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"
_LOOPBACK_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))


def _safe_log(log: Optional[Callable[[str], Any]], message: str) -> None:
    if log is None:
        return
    try:
        log(message)
    except Exception:
        pass


def _cfg_value(cfg: Any, key: str, default: Any = None) -> Any:
    if cfg is None:
        return default
    try:
        if hasattr(cfg, key):
            return getattr(cfg, key)
    except Exception:
        return default
    getter = getattr(cfg, "get", None)
    if callable(getter):
        try:
            return getter(key, default)
        except Exception:
            return default
    return default


def _string(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _loopback_origin(value: Any) -> Optional[str]:
    raw = _string(value).rstrip("/")
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme != "http" or parsed.username or parsed.password:
            return None
        if parsed.hostname not in _LOOPBACK_HOSTS or parsed.path not in ("", "/"):
            return None
        if parsed.query or parsed.fragment:
            return None
        return raw
    except Exception:
        return None


@dataclass(frozen=True)
class AuthResult:
    status: str
    reason: str
    token: Optional[str] = field(default=None, repr=False)
    expires_at: Optional[float] = None
    pairing_id: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED


def token_path(cfg: Any = None, path: Optional[str] = None) -> str:
    """Resolve the private token path from explicit config or plugin data.

    The normal plugin config always has ``data_dir``.  Keeping this resolver
    here means callers that only have the normal config do not accidentally
    fall back to the developer machine's default token file.
    """
    selected = path or _string(_cfg_value(cfg, "t3_token_path"))
    selected = selected or _string(os.environ.get("WARMFOLD_T3_TOKEN_PATH"))
    if not selected:
        data_dir = _string(_cfg_value(cfg, "data_dir"))
        if data_dir:
            selected = os.path.join(os.path.expanduser(data_dir), "t3-bearer.json")
    return os.path.expanduser(selected or DEFAULT_TOKEN_PATH)


def resolve_token_path(cfg: Any = None, path: Optional[str] = None) -> str:
    """Internal spelling that avoids collisions with token_path kwargs."""
    return token_path(cfg, path)


def _token_path(cfg: Any, path: Optional[str]) -> str:
    # Private compatibility name for code written before token_path was public.
    return resolve_token_path(cfg, path)


def read_saved_token_for_config(cfg: Any = None, path: Optional[str] = None, *, now: Optional[float] = None) -> Optional[AuthResult]:
    """Read the configured private token without exposing its value."""
    return read_saved_token(resolve_token_path(cfg, path), now=now)


def read_saved_token(
    path: Optional[str] = None,
    *,
    now: Optional[float] = None,
) -> Optional[AuthResult]:
    """Read a still-valid token without exposing its value in a result repr."""
    try:
        with open(os.path.expanduser(path or DEFAULT_TOKEN_PATH), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    token = _string(value.get("access_token"))
    try:
        expires_at = float(value.get("expires_at"))
    except (TypeError, ValueError):
        return None
    current = float(now if now is not None else _datetime.datetime.now().timestamp())
    if not token or expires_at - current <= TOKEN_REFRESH_MARGIN_SECONDS:
        return None
    return AuthResult(ACCEPTED, "saved T3 bearer token", token=token, expires_at=expires_at)


def _read_token_record(path: str) -> Optional[AuthResult]:
    """Read a well-formed token record, including an expired one."""
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    token = _string(value.get("access_token"))
    try:
        expires_at = float(value.get("expires_at"))
    except (TypeError, ValueError):
        return None
    if not token or not math.isfinite(expires_at) or expires_at <= 0:
        return None
    return AuthResult(ACCEPTED, "saved T3 bearer token", token=token, expires_at=expires_at)


def save_token(path: str, token: str, expires_at: float) -> None:
    """Atomically save a token with private parent and file permissions."""
    target = os.path.abspath(os.path.expanduser(path))
    parent = os.path.dirname(target)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    try:
        os.chmod(parent, 0o700)
    except OSError:
        pass
    fd, temporary = tempfile.mkstemp(prefix=".t3-bearer-", dir=parent, text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"access_token": token, "expires_at": float(expires_at)}, handle)
            handle.write("\n")
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _cli_paths(cfg: Any) -> tuple:
    configured_app = _string(_cfg_value(cfg, "t3_app_path"))
    configured_server = _string(_cfg_value(cfg, "t3_server_bin"))
    if configured_app:
        app = configured_app
        server_bin = configured_server or (
            STABLE_SERVER_BIN if os.path.normpath(configured_app) == os.path.normpath(STABLE_APP_PATH)
            else DEFAULT_SERVER_BIN
        )
    elif os.path.exists(DEFAULT_APP_PATH):
        app = DEFAULT_APP_PATH
        server_bin = configured_server or DEFAULT_SERVER_BIN
    elif os.path.exists(STABLE_APP_PATH):
        app = STABLE_APP_PATH
        server_bin = configured_server or STABLE_SERVER_BIN
    else:
        app = DEFAULT_APP_PATH
        server_bin = configured_server or DEFAULT_SERVER_BIN
    return os.path.expanduser(app), os.path.expanduser(server_bin)


def _base_dir(cfg: Any, db_path: Optional[str]) -> str:
    selected = _string(_cfg_value(cfg, "t3_base_dir"))
    if selected:
        return os.path.expanduser(selected)
    if db_path:
        # state.sqlite is normally <base>/userdata/state.sqlite.
        return os.path.dirname(os.path.dirname(os.path.abspath(os.path.expanduser(db_path))))
    return os.path.expanduser("~/.t3")


def issue_pairing_credential(
    cfg: Any = None,
    *,
    db_path: Optional[str] = None,
    runner: Optional[Callable[..., Any]] = None,
    timeout: float = 15.0,
    log: Optional[Callable[[str], Any]] = None,
) -> AuthResult:
    """Issue a native T3 pairing credential through the installed app CLI."""
    app, server_bin = _cli_paths(cfg)
    # ``server_bin`` lives inside app.asar on the installed desktop build;
    # that path is virtual to the host filesystem but is resolved by Electron.
    server_path_is_virtual = ".asar/" in server_bin or server_bin.endswith(".asar")
    if not os.path.exists(app) or (not os.path.exists(server_bin) and not server_path_is_virtual):
        return AuthResult(REJECTED, "T3 native CLI unavailable")
    command = [
        app,
        server_bin,
        "auth",
        "pairing",
        "create",
        "--base-dir",
        _base_dir(cfg, db_path),
        "--json",
        "--ttl",
        PAIRING_TTL,
        "--label",
        "warmfold",
    ]
    environment = dict(os.environ)
    environment["ELECTRON_RUN_AS_NODE"] = "1"
    run = runner or subprocess.run
    try:
        completed = run(command, capture_output=True, text=True, timeout=timeout, env=environment)
    except Exception as exc:
        _safe_log(log, "T3 native pairing command failed: %s" % type(exc).__name__)
        return AuthResult(REJECTED, "T3 native CLI failed")
    if getattr(completed, "returncode", 1) != 0:
        _safe_log(log, "T3 native pairing command rejected")
        return AuthResult(REJECTED, "T3 native CLI rejected enrollment")
    try:
        payload = json.loads(getattr(completed, "stdout", ""))
    except (TypeError, ValueError):
        return AuthResult(REJECTED, "T3 native pairing response malformed")
    credential = _string(payload.get("credential")) if isinstance(payload, dict) else ""
    pairing_id = _string(payload.get("id")) if isinstance(payload, dict) else ""
    if not credential or not pairing_id:
        return AuthResult(REJECTED, "T3 native pairing response incomplete")
    return AuthResult(ACCEPTED, "T3 pairing credential issued", token=credential, pairing_id=pairing_id)


class _NoRedirect(_urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise _urlerror.HTTPError(req.full_url, code, "redirect refused", headers, fp)


def _safe_open(request: _urlrequest.Request, timeout: float, opener: Optional[Callable[..., Any]]) -> Any:
    if opener is not None:
        return opener(request, timeout=timeout)
    return _urlrequest.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _probe_active_token(
    token: str,
    origin: str,
    *,
    opener: Optional[Callable[..., Any]] = None,
    timeout: float = 5.0,
) -> Optional[bool]:
    """Check the saved session before native renewal.

    A 401/403 means the prior session is no longer active, so renewal must
    stop instead of silently minting a replacement. Transport failures are
    also treated as unknown and do not mint a credential.
    """
    request = _urlrequest.Request(
        origin.rstrip("/") + "/api/auth/session",
        headers={"Accept": "application/json", "Authorization": "Bearer " + token},
        method="GET",
    )
    try:
        response = _safe_open(request, timeout, opener)
        try:
            status = int(getattr(response, "status", None) or response.getcode())
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        if not 200 <= status < 300:
            return False
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return False
        return isinstance(payload, dict) and payload.get("authenticated") is True
    except _urlerror.HTTPError:
        return False
    except Exception:
        return None


def exchange_pairing_credential(
    credential: str,
    origin: str,
    *,
    opener: Optional[Callable[..., Any]] = None,
    timeout: float = 5.0,
    log: Optional[Callable[[str], Any]] = None,
) -> AuthResult:
    """Exchange a one time credential for a narrowly scoped bearer token."""
    selected_origin = _loopback_origin(origin)
    if not selected_origin:
        return AuthResult(REJECTED, "T3 server origin is not loopback")
    credential = _string(credential)
    if not credential:
        return AuthResult(REJECTED, "T3 pairing credential missing")
    form = {
        "grant_type": TOKEN_EXCHANGE_GRANT,
        "subject_token": credential,
        "subject_token_type": BOOTSTRAP_TOKEN_TYPE,
        "requested_token_type": ACCESS_TOKEN_TYPE,
        "scope": "orchestration:read orchestration:operate",
        "client_label": "warmfold",
        "client_device_type": "bot",
        "client_os": "darwin",
    }
    request = _urlrequest.Request(
        selected_origin + "/oauth/token",
        data=urlencode(form).encode("ascii"),
        headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        response = _safe_open(request, timeout, opener)
        try:
            response_status = getattr(response, "status", None)
            status = int(response_status if response_status is not None else response.getcode())
        except Exception:
            status = 200
        try:
            raw = response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except _urlerror.HTTPError as exc:
        _safe_log(log, "T3 token exchange rejected (HTTP %d)" % int(getattr(exc, "code", 0) or 0))
        return AuthResult(REJECTED, "T3 token exchange rejected")
    except Exception as exc:
        _safe_log(log, "T3 token exchange outcome uncertain: %s" % type(exc).__name__)
        return AuthResult(UNCERTAIN, "T3 token exchange outcome uncertain")
    if status < 200 or status >= 300:
        return AuthResult(REJECTED, "T3 token exchange rejected")
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        token = _string(payload["access_token"])
        token_type = _string(payload["token_type"])
        expires_in = int(payload["expires_in"])
        scope = set(_string(payload["scope"]).split())
        if not token or token_type.lower() != "bearer" or expires_in <= 0 or scope != REQUIRED_SCOPES:
            raise ValueError("token response scope or shape invalid")
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return AuthResult(UNCERTAIN, "T3 token response malformed")
    return AuthResult(
        ACCEPTED,
        "T3 bearer token issued",
        token=token,
        expires_at=_datetime.datetime.now().timestamp() + expires_in,
    )


def enroll_native_bearer(
    cfg: Any = None,
    *,
    db_path: Optional[str] = None,
    origin: Optional[str] = None,
    token_path: Optional[str] = None,
    runner: Optional[Callable[..., Any]] = None,
    opener: Optional[Callable[..., Any]] = None,
    log: Optional[Callable[[str], Any]] = None,
) -> AuthResult:
    """Use a saved token or explicitly enroll one through native T3 auth."""
    saved = read_saved_token_for_config(cfg, token_path)
    if saved is not None:
        return saved
    selected_origin = origin or _string(_cfg_value(cfg, "t3_origin"))
    if not selected_origin:
        from .t3 import runtime_origin

        selected_origin = runtime_origin(_cfg_value(cfg, "t3_runtime_path"))
    if not selected_origin:
        return AuthResult(REJECTED, "T3 server origin unavailable")
    selected_db = db_path or _string(_cfg_value(cfg, "t3_db_path")) or None
    issued = issue_pairing_credential(cfg, db_path=selected_db, runner=runner, log=log)
    if not issued.accepted or not issued.token:
        return issued
    exchanged = exchange_pairing_credential(issued.token, selected_origin, opener=opener, log=log)
    if not exchanged.accepted or not exchanged.token or exchanged.expires_at is None:
        return exchanged
    try:
        save_token(resolve_token_path(cfg, token_path), exchanged.token, exchanged.expires_at)
    except OSError:
        return AuthResult(REJECTED, "T3 bearer token could not be stored")
    return exchanged


def renew_native_bearer(
    cfg: Any = None,
    *,
    db_path: Optional[str] = None,
    origin: Optional[str] = None,
    token_path: Optional[str] = None,
    runner: Optional[Callable[..., Any]] = None,
    opener: Optional[Callable[..., Any]] = None,
    log: Optional[Callable[[str], Any]] = None,
) -> AuthResult:
    """Refresh an active, previously enrolled token through native T3 auth.

    Renewal is deliberately separate from enrollment. An absent, malformed,
    expired, or revoked token cannot trigger a replacement automatically.
    Callers may use this before dispatch while the existing session is still
    accepted by T3 and within the refresh margin; a dispatch 401 must never
    call it. An explicitly invoked ``t3-setup`` remains available after expiry.
    """
    path = resolve_token_path(cfg, token_path)
    record = _read_token_record(path)
    if record is None:
        return AuthResult(REJECTED, "no well-formed prior T3 bearer token")
    current = _datetime.datetime.now().timestamp()
    remaining = (record.expires_at or 0) - current
    if remaining > TOKEN_REFRESH_MARGIN_SECONDS:
        return record
    if remaining <= 0:
        return AuthResult(REJECTED, "previous T3 bearer token expired; run t3-setup")
    selected_origin = origin or _string(_cfg_value(cfg, "t3_origin"))
    if not selected_origin:
        from .t3 import runtime_origin

        selected_origin = runtime_origin(_cfg_value(cfg, "t3_runtime_path")) or ""
    selected_origin = _loopback_origin(selected_origin)
    if not selected_origin:
        return AuthResult(REJECTED, "T3 server origin unavailable for renewal")
    active = _probe_active_token(record.token or "", selected_origin, opener=opener)
    if active is not True:
        return AuthResult(REJECTED, "previous T3 bearer token is not active")
    return enroll_native_bearer(
        cfg,
        db_path=db_path,
        origin=origin,
        token_path=path,
        runner=runner,
        opener=opener,
        log=log,
    )


def status_report(cfg: Any = None, *, token_path: Optional[str] = None) -> dict:
    """Return non-secret readiness information for a manual status command."""
    path = resolve_token_path(cfg, token_path)
    saved = read_saved_token(path)
    if saved is not None:
        expires = _datetime.datetime.fromtimestamp(saved.expires_at, _datetime.timezone.utc)
        return {
            "status": "ready",
            "reason": saved.reason,
            "expiresAt": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
        }
    if os.path.exists(path):
        return {"status": "expired", "reason": "saved T3 bearer token is missing or expired"}
    return {"status": "unconfigured", "reason": "no saved T3 bearer token"}


def setup_report(cfg: Any = None, **kwargs: Any) -> dict:
    """Enroll once through native T3 auth and return a secret-free report."""
    result = enroll_native_bearer(cfg, **kwargs)
    report = {"status": result.status, "reason": result.reason}
    if result.expires_at is not None:
        report["expiresAt"] = _datetime.datetime.fromtimestamp(
            result.expires_at, _datetime.timezone.utc
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
    return report


__all__ = [
    "ACCEPTED",
    "UNCERTAIN",
    "REJECTED",
    "REQUIRED_SCOPES",
    "AuthResult",
    "token_path",
    "read_saved_token",
    "read_saved_token_for_config",
    "save_token",
    "issue_pairing_credential",
    "exchange_pairing_credential",
    "enroll_native_bearer",
    "renew_native_bearer",
    "status_report",
    "setup_report",
]
