"""A reply or credential file nested too deep for the parser.

`json.loads` raises `RecursionError` for one, which is not a `ValueError`, so a
handler written for an unreadable body never sees it.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from google_health_mcp import api, auth, config, db, doctor
from google_health_mcp.tools import sync_tools

#: Unclosed, so it fails at parse and no deep value ever exists to write out.
DEEP = b"[" * 200_000


def _answer(body):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = body
    return response


@pytest.fixture
def files(tmp_path, monkeypatch):
    tokens = tmp_path / "google_tokens.json"
    client = tmp_path / "google_client.json"
    monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", tokens)
    monkeypatch.setattr(config, "GOOGLE_CLIENT_PATH", client)
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "absent.db")
    client.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake.apps.googleusercontent.com",
                    "client_secret": "fake-secret",
                }
            }
        )
    )
    tokens.write_text(json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": 0}))
    return tokens, client


def test_the_fixture_is_too_deep_for_the_parser():
    """A parser accepting this depth leaves every test below proving nothing."""
    with pytest.raises(RecursionError):
        json.loads(DEEP)


def test_a_data_reply_is_reported_as_unreadable(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(api, "refresh_google_token", lambda: "fake-access-token")
    with patch("urllib.request.urlopen", return_value=_answer(DEEP)):
        with pytest.raises(api.HealthAPIError) as caught:
            api.google_get("users/me/profile", {})
    assert str(caught.value) == "Google returned an unreadable response."
    assert not isinstance(caught.value, api.HealthNetworkError)


def test_a_sync_records_it_as_an_api_error(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cache.db")
    monkeypatch.setattr(api, "refresh_google_token", lambda: "fake-access-token")
    with patch("urllib.request.urlopen", return_value=_answer(DEEP)):
        result = sync_tools.run_sync(
            ["sleep"], handlers={"sleep": lambda c, s, e: api.google_get("users/me/profile", {})}
        )
    assert result["sleep"] == {
        "status": "error",
        "message": "Google returned an unreadable response.",
    }


def test_a_refresh_reply_writes_nothing(files):
    tokens, _client = files
    before = tokens.read_bytes()
    with patch("urllib.request.urlopen", return_value=_answer(DEEP)):
        with pytest.raises(auth.RefreshNetworkError) as caught:
            auth.refresh_google_token()
    assert str(caught.value) == "Google returned an unreadable response."
    assert tokens.read_bytes() == before


def test_a_token_file_is_a_refusal(files):
    tokens, _client = files
    tokens.write_bytes(DEEP)
    with pytest.raises(auth.TokenRefused) as caught:
        auth.refresh_google_token()
    assert (
        str(caught.value)
        == "google_tokens.json is missing or unreadable. Run: google-health-mcp auth"
    )


def test_a_client_file_is_a_refusal(files):
    _tokens, client = files
    client.write_bytes(DEEP)
    with pytest.raises(auth.TokenRefused) as caught:
        auth.refresh_google_token()
    assert (
        str(caught.value)
        == "google_client.json is missing or unreadable. Run: google-health-mcp auth"
    )


def test_a_token_file_written_during_the_refresh_is_left_alone(files):
    tokens, _client = files

    def urlopen(request, timeout=None):
        tokens.write_bytes(DEEP)
        return _answer(b'{"access_token": "new"}')

    with patch("urllib.request.urlopen", urlopen):
        assert auth.refresh_google_token() == "new"
    assert tokens.read_bytes() == DEEP


def test_a_token_file_written_during_a_refusal_is_not_a_refusal(files):
    tokens, _client = files

    def urlopen(request, timeout=None):
        tokens.write_bytes(DEEP)
        raise urllib.error.HTTPError("https://example.invalid/", 400, "err", {}, None)

    with patch("urllib.request.urlopen", urlopen):
        with pytest.raises(auth.RefreshNetworkError) as caught:
            auth.refresh_google_token()
    assert str(caught.value) == "The token file changed during the refresh."


def test_a_consent_reply_saves_nothing(files, capsys):
    tokens, _client = files
    before = tokens.read_bytes()

    class Server:
        def __init__(self, address, handler):
            self.handler = handler

        def handle_request(self):
            callback = self.handler.__new__(self.handler)
            callback.path = "/?code=fake-code"
            callback._respond = lambda *a: None
            callback.do_GET()

        def server_close(self):
            pass

    with (
        patch.object(auth, "_CallbackServer", Server),
        patch("webbrowser.open"),
        patch("urllib.request.urlopen", return_value=_answer(DEEP)),
        pytest.raises(SystemExit) as stop,
    ):
        auth.setup_google_auth()
    assert stop.value.code == 1
    assert "RecursionError" in capsys.readouterr().err
    assert tokens.read_bytes() == before


def test_the_granted_scopes_read_as_unknown(files):
    tokens, _client = files
    tokens.write_bytes(DEEP)
    assert auth.granted_scopes() is None


@pytest.mark.parametrize(
    "which, name",
    [(0, "credentials"), (1, "app config")],
    ids=["token-file", "client-file"],
)
def test_doctor_reports_a_file_as_unreadable(files, which, name):
    """Past its handler it lands in the catch-all, which calls it a bug in doctor."""
    files[which].write_bytes(DEEP)
    findings = doctor.run_checks()
    assert not [f for f in findings if "could not run" in f.detail]
    (finding,) = [f for f in findings if f.name == name]
    assert finding.severity == doctor.FAIL
    assert "unreadable or malformed" in finding.detail
    assert any(f.name == "database" and "absent.db" in f.detail for f in findings)


def test_an_error_body_on_a_refused_refresh_is_still_a_refusal(files):
    tokens, _client = files
    before = tokens.read_bytes()
    error = urllib.error.HTTPError("https://example.invalid/", 400, "err", {}, None)
    error.read = lambda: DEEP
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(auth.TokenRefused) as caught:
            auth.refresh_google_token()
    assert str(caught.value) == "Google refused the credentials. Run: google-health-mcp auth"
    assert tokens.read_bytes() == before


def test_an_error_body_on_a_403_is_still_a_refused_request(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(api, "refresh_google_token", lambda: "fake-access-token")
    error = urllib.error.HTTPError("https://example.invalid/", 403, "err", {}, None)
    error.read = lambda: DEEP
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(api.HealthAuthError) as caught:
            api.google_get("users/me/profile", {})
    assert "has not been published" in str(caught.value)


def test_an_error_body_on_a_400_is_an_api_error(monkeypatch):
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(api, "refresh_google_token", lambda: "fake-access-token")
    error = urllib.error.HTTPError("https://example.invalid/", 400, "err", {}, None)
    error.read = lambda: DEEP
    with patch("urllib.request.urlopen", side_effect=error):
        with pytest.raises(api.HealthAPIError) as caught:
            api.google_get("users/me/profile", {})
    assert str(caught.value) == "API error 400 for profile"
    assert not isinstance(caught.value, api.AccountNotLinked)
