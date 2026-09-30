"""Recover the production refresh-only configuration without leaking credentials."""
import json
from datetime import datetime, timezone
from unittest import mock

import pytest

from ideagen import config, olive_web, scheduler, shelf_store
from ideagen.sources import olive


def response(payload, status=200):
    r = mock.Mock(status_code=status, headers={})
    r.content = json.dumps(payload).encode()
    r.json.return_value = payload
    if status >= 400:
        r.raise_for_status.side_effect = RuntimeError(f"HTTP {status}")
    return r


def test_refresh_only_startup_restores_access_and_persists_rotated_grant(tmp_path, monkeypatch):
    path = tmp_path / "oauth" / "tokens.json"
    monkeypatch.setenv("IDEAGEN_OLIVE_TOKEN_FILE", str(path))
    monkeypatch.setenv("OLIVE_OAUTH_ACCESS_TOKEN", "")
    monkeypatch.setenv("OLIVE_OAUTH_REFRESH_TOKEN", "initial-refresh")
    monkeypatch.setenv("OLIVE_OAUTH_CLIENT_ID", "existing-client")
    session = mock.Mock(headers={})
    session.post.return_value = response({"result": {"tools": []}})
    with mock.patch.object(olive.requests, "Session", return_value=session), \
         mock.patch.object(olive.requests, "post", return_value=response({
             "access_token": "restored-access", "refresh_token": "rotated-refresh",
             "expires_in": 3600,
         })) as refresh:
        client = olive.OliveMCP(url="https://catalog.example/mcp", token_url="https://sso.example/token")
        assert client.tools() == []
    refresh.assert_called_once()
    assert session.headers["Authorization"] == "Bearer restored-access"
    assert config.olive_credentials()["refresh_token"] == "rotated-refresh"
    assert config.olive_credentials()["access_token"] == "restored-access"
    # A new process must load the rotated grant, not reuse the original env grant.
    assert olive.OliveMCP(url="https://catalog.example/mcp").refresh_token == "rotated-refresh"


def test_revoked_refresh_grant_is_actionable_and_never_reaches_mcp():
    session = mock.Mock(headers={})
    with mock.patch.object(olive.requests, "Session", return_value=session), \
         mock.patch.object(olive.requests, "post", return_value=response({
             "error": "invalid_grant", "error_description": "refresh_token=secret-value",
         }, status=400)) as refresh:
        client = olive.OliveMCP(url="https://catalog.example/mcp", access_token="",
                                refresh_token="expired-refresh", client_id="client",
                                token_url="https://sso.example/token")
        with pytest.raises(olive.OliveMCPError, match="HTTP 400: invalid_grant") as error:
            client.tools()
    assert "secret-value" not in str(error.value)
    refresh.assert_called_once()
    session.post.assert_not_called()


def test_sync_errors_keep_cause_but_strip_credentials():
    with mock.patch.object(config, "olive_credentials", return_value={"refresh_token": "known-secret"}):
        text = olive.safe_error(RuntimeError(
            'HTTP 400 invalid_grant known-secret {"access_token": "new-secret"} '
            'Authorization: Bearer header-secret code=query-secret&x=1'))
    assert "invalid_grant" in text
    for secret in ("known-secret", "new-secret", "header-secret", "query-secret"):
        assert secret not in text


def test_manual_sync_uses_the_full_configured_detail_coverage():
    olive_web.reset_for_tests()
    with mock.patch.object(olive, "OliveMCP"), \
         mock.patch.object(olive, "pull_snapshot", return_value={}) as pull, \
         mock.patch.object(olive_web.platform_mod, "load"), \
         mock.patch.object(olive_web.shelf_store, "persist", return_value={}):
        olive_web._sync_worker()
    assert pull.call_args.kwargs["detail_limit"] == config.OLIVE_DETAIL_LIMIT
    olive_web.reset_for_tests()


@pytest.mark.parametrize("failed", [False, True])
def test_daily_sync_attempts_refresh_only_and_records_the_actual_error(failed):
    platform = mock.Mock()
    platform.state.q.return_value = []
    now = datetime(2026, 9, 30, 16, 0, tzinfo=scheduler.HKT)
    problems = []
    with mock.patch.object(config, "olive_credentials", return_value={
             "client_id": "client", "refresh_token": "refresh-secret"}), \
         mock.patch.object(shelf_store, "latest_snapshot", return_value=None), \
         mock.patch.object(shelf_store, "persist", return_value={"items": 1, "navs": 1}), \
         mock.patch.object(scheduler, "_insert_run_row") as record, \
         mock.patch.object(olive, "OliveMCP"), \
         mock.patch.object(olive, "pull_snapshot", return_value={}) as pull:
        if failed:
            pull.side_effect = olive.OliveMCPError('OAuth refresh HTTP 400: invalid_grant refresh-secret')
        result = scheduler._sync_olive_daily(platform, now, now.astimezone(timezone.utc),
                                             problems, dry_run=False, log=lambda _: None)
    pull.assert_called_once()
    if failed:
        assert "invalid_grant" in result["failed"]
        assert "refresh-secret" not in result["failed"]
        assert record.call_args.kwargs["error"] == result["failed"]
        assert problems
    else:
        assert result["items"] == 1
        assert record.call_args.kwargs["ok"] == 1
