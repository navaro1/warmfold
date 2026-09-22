"""Tests for explicit native T3 authentication enrollment."""

import json
import os
import stat
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from warmfoldlib import t3_auth  # noqa: E402


class _Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def close(self):
        pass


def _runner_factory(calls):
    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"id": "pairing-id", "credential": "one-time-secret"}),
            stderr="",
        )

    return runner


def test_issue_uses_bundled_native_cli_without_logging_credential(tmp_path):
    app = tmp_path / "T3 Code"
    server = tmp_path / "bin.mjs"
    app.write_text("app", encoding="utf-8")
    server.write_text("server", encoding="utf-8")
    calls = []
    logs = []
    result = t3_auth.issue_pairing_credential(
        {"t3_app_path": str(app), "t3_server_bin": str(server)},
        db_path=str(tmp_path / "userdata" / "state.sqlite"),
        runner=_runner_factory(calls),
        log=logs.append,
    )
    assert result.accepted
    assert result.pairing_id == "pairing-id"
    assert result.token == "one-time-secret"
    command, kwargs = calls[0]
    assert command[0:2] == [str(app), str(server)]
    assert command[-5:] == ["--json", "--ttl", "5m", "--label", "warmfold"]
    assert kwargs["env"]["ELECTRON_RUN_AS_NODE"] == "1"
    assert not logs


def test_exchange_requests_only_orchestration_scopes():
    seen = {}

    def opener(request, timeout):
        seen["body"] = request.data.decode("ascii")
        seen["url"] = request.full_url
        return _Response(
            b'{"access_token":"bearer-secret","token_type":"Bearer",'
            b'"expires_in":3600,"scope":"orchestration:read orchestration:operate"}'
        )

    result = t3_auth.exchange_pairing_credential(
        "one-time-secret", "http://127.0.0.1:3773", opener=opener
    )
    assert result.accepted
    assert result.token == "bearer-secret"
    assert seen["url"] == "http://127.0.0.1:3773/oauth/token"
    assert "scope=orchestration%3Aread+orchestration%3Aoperate" in seen["body"]
    assert "subject_token=one-time-secret" in seen["body"]


def test_exchange_rejects_a_token_without_required_scope():
    result = t3_auth.exchange_pairing_credential(
        "one-time-secret",
        "http://127.0.0.1:3773",
        opener=lambda request, timeout: _Response(
            b'{"access_token":"token","token_type":"Bearer",'
            b'"expires_in":3600,"scope":"orchestration:read"}'
        ),
    )
    assert result.status == t3_auth.UNCERTAIN
    assert "malformed" in result.reason


def test_exchange_rejects_excess_scope():
    result = t3_auth.exchange_pairing_credential(
        "one-time-secret",
        "http://127.0.0.1:3773",
        opener=lambda request, timeout: _Response(
            b'{"access_token":"token","token_type":"Bearer",'
            b'"expires_in":3600,"scope":"orchestration:read orchestration:operate terminal:operate"}'
        ),
    )
    assert result.status == t3_auth.UNCERTAIN


def test_exchange_rejects_remote_origin_before_network():
    called = []
    result = t3_auth.exchange_pairing_credential(
        "credential",
        "https://example.invalid",
        opener=lambda *args, **kwargs: called.append(True),
    )
    assert result.status == t3_auth.REJECTED
    assert not called


def test_exchange_does_not_follow_redirects():
    # A redirect response is handled as a rejected exchange. The injected
    # opener is deliberately the response boundary, so no second URL can be
    # visited by this code path.
    seen = []

    def opener(request, timeout):
        seen.append(request.full_url)
        return _Response(b'{"location":"https://example.invalid/steal"}')

    result = t3_auth.exchange_pairing_credential(
        "credential", "http://127.0.0.1:3773", opener=opener
    )
    assert result.status == t3_auth.UNCERTAIN
    assert seen == ["http://127.0.0.1:3773/oauth/token"]


def test_enrollment_saves_private_expiring_token(tmp_path):
    app = tmp_path / "T3 Code"
    server = tmp_path / "bin.mjs"
    app.write_text("app", encoding="utf-8")
    server.write_text("server", encoding="utf-8")
    token_path = tmp_path / "private" / "t3.json"
    result = t3_auth.enroll_native_bearer(
        {
            "t3_app_path": str(app),
            "t3_server_bin": str(server),
            "t3_token_path": str(token_path),
        },
        db_path=str(tmp_path / "userdata" / "state.sqlite"),
        origin="http://127.0.0.1:3773",
        runner=_runner_factory([]),
        opener=lambda request, timeout: _Response(
            b'{"access_token":"bearer-secret","token_type":"Bearer",'
            b'"expires_in":3600,"scope":"orchestration:read orchestration:operate"}'
        ),
    )
    assert result.accepted
    assert token_path.exists()
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    saved = json.loads(token_path.read_text(encoding="utf-8"))
    assert saved["access_token"] == "bearer-secret"
    assert t3_auth.read_saved_token(str(token_path), now=0).accepted


def test_saved_token_is_reused_without_native_pairing(tmp_path):
    token_path = tmp_path / "t3.json"
    t3_auth.save_token(str(token_path), "saved-token", 4_000_000_000)
    result = t3_auth.enroll_native_bearer(
        {"t3_token_path": str(token_path)},
        origin="http://127.0.0.1:3773",
        runner=lambda *args, **kwargs: pytest.fail("pairing should not run"),
    )
    assert result.accepted
    assert result.token == "saved-token"


def test_status_report_never_includes_token(tmp_path):
    token_path = tmp_path / "t3.json"
    t3_auth.save_token(str(token_path), "private-token", 4_000_000_000)
    report = t3_auth.status_report(token_path=str(token_path))
    assert report["status"] == "ready"
    assert "private-token" not in json.dumps(report)
    assert "expiresAt" in report


def test_data_dir_selects_private_token_file(tmp_path):
    cfg = {"data_dir": str(tmp_path / "plugin-data")}
    expected = tmp_path / "plugin-data" / "t3-bearer.json"
    assert t3_auth.token_path(cfg) == str(expected)
    t3_auth.save_token(str(expected), "data-dir-token", 4_000_000_000)
    saved = t3_auth.read_saved_token_for_config(cfg, now=0)
    assert saved is not None
    assert saved.token == "data-dir-token"


def test_renew_requires_existing_well_formed_record(tmp_path):
    token_path = tmp_path / "t3.json"
    token_path.write_text("{}", encoding="utf-8")
    result = t3_auth.renew_native_bearer(
        {"t3_token_path": str(token_path)},
        runner=lambda *args, **kwargs: pytest.fail("malformed record must not pair"),
    )
    assert result.status == t3_auth.REJECTED
    assert "well-formed" in result.reason


def test_renew_reenrolls_only_an_expired_existing_record(tmp_path):
    app = tmp_path / "T3 Code"
    server = tmp_path / "bin.mjs"
    app.write_text("app", encoding="utf-8")
    server.write_text("server", encoding="utf-8")
    token_path = tmp_path / "t3.json"
    t3_auth.save_token(str(token_path), "near-expiry-bearer", time.time() + 30)
    calls = []
    result = t3_auth.renew_native_bearer(
        {
            "t3_app_path": str(app),
            "t3_server_bin": str(server),
            "t3_token_path": str(token_path),
        },
        origin="http://127.0.0.1:3773",
        runner=_runner_factory(calls),
        opener=lambda request, timeout: _Response(
            b'{"authenticated":true}' if request.full_url.endswith("/api/auth/session") else
            b'{"access_token":"renewed-bearer","token_type":"Bearer",'
            b'"expires_in":3600,"scope":"orchestration:read orchestration:operate"}'
        ),
    )
    assert result.accepted
    assert result.token == "renewed-bearer"
    assert calls
    assert t3_auth.read_saved_token(str(token_path), now=0).token == "renewed-bearer"


def test_renew_does_not_replace_revoked_or_unauthorized_record(tmp_path):
    token_path = tmp_path / "t3.json"
    t3_auth.save_token(str(token_path), "revoked-bearer", time.time() + 30)

    class _Unauthorized:
        status = 401

        def close(self):
            pass

    result = t3_auth.renew_native_bearer(
        {"t3_token_path": str(token_path)},
        origin="http://127.0.0.1:3773",
        opener=lambda request, timeout: _Unauthorized(),
        runner=lambda *args, **kwargs: pytest.fail("inactive record must not pair"),
    )
    assert result.status == t3_auth.REJECTED
    assert "not active" in result.reason
