"""Google OAuth: the classification boundary and the Testing-status diagnostic.

Mirrors TestTheRefreshBoundary in test_api.py. The two-type boundary is not
optional here: api.get and doctor's grading are built on it, so a Google-only
exception type would reach code that has no branch for it.
"""

import json
import time
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from google_health_mcp import auth, config


@pytest.fixture(autouse=True)
def _isolate_google_state(monkeypatch, tmp_path):
    """No test may read the developer's real client file or token cache."""
    monkeypatch.setattr(config, "GOOGLE_CLIENT_PATH", tmp_path / "google_client.json")
    monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", tmp_path / "google_tokens.json")
    monkeypatch.setattr(auth, "_cached_google_tokens", None)
    monkeypatch.setattr(auth, "_cached_google_client", None)


def _write_client(tmp_path, **overrides):
    body = {
        "installed": {
            "client_id": "fake-client.apps.googleusercontent.com",
            "client_secret": "fake-secret",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        }
    }
    body["installed"].update(overrides)
    (tmp_path / "google_client.json").write_text(json.dumps(body))


def _write_tokens(tmp_path, **overrides):
    body = {
        "access_token": "stored-access",
        "refresh_token": "stored-refresh",
        "expires_at": 0,
    }
    body.update(overrides)
    (tmp_path / "google_tokens.json").write_text(json.dumps(body))


def _http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://oauth2.googleapis.com/token", code, "err", {}, None)


class TestTheGoogleClientFile:
    def test_googles_downloaded_desktop_format_is_read_as_is(self, tmp_path):
        _write_client(tmp_path)
        client = auth._load_google_client()
        assert client["client_id"] == "fake-client.apps.googleusercontent.com"
        assert client["client_secret"] == "fake-secret"

    def test_a_file_without_the_installed_wrapper_is_a_refusal(self, tmp_path):
        (tmp_path / "google_client.json").write_text(json.dumps({"client_id": "bare"}))
        with pytest.raises(auth.TokenRefused):
            auth._load_google_client()

    def test_an_absent_file_is_a_refusal(self, tmp_path):
        with pytest.raises(auth.TokenRefused):
            auth._load_google_client()


class TestTheGoogleRefreshBoundary:
    """Exactly two types out, including for a failure nobody anticipated."""

    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("bare timeout"),
            ConnectionResetError("reset"),
            KeyError("access_token"),
            ValueError("unparseable"),
            RuntimeError("something nobody classified"),
        ],
        ids=["timeout", "reset", "keyerror", "valueerror", "runtime"],
    )
    def test_an_unclassified_failure_becomes_a_network_error(self, exc, monkeypatch):
        monkeypatch.setattr(auth, "_refresh_google_token", MagicMock(side_effect=exc))
        with pytest.raises(auth.RefreshNetworkError):
            auth.refresh_google_token()

    def test_a_refusal_is_passed_through_unchanged(self, monkeypatch):
        monkeypatch.setattr(
            auth, "_refresh_google_token", MagicMock(side_effect=auth.TokenRefused("no"))
        )
        with pytest.raises(auth.TokenRefused):
            auth.refresh_google_token()

    def test_a_rate_limit_is_not_a_refusal(self, tmp_path):
        """429 clears on its own; answering it by re-authorising rotates a shared file."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        with patch("urllib.request.urlopen", side_effect=_http_error(429)):
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_google_token()


class TestTheTestingStatusDiagnostic:
    """The failure every install hits a week in, and that nobody diagnoses unaided."""

    def test_invalid_grant_names_the_publish_step(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        body = json.dumps({"error": "invalid_grant", "error_description": "Token expired"}).encode()
        err = _http_error(400)
        err.read = lambda: body
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(auth.TokenRefused) as excinfo:
                auth.refresh_google_token()
        assert "Publish" in str(excinfo.value)

    def test_the_message_carries_no_part_of_the_response(self, tmp_path):
        """A token endpoint's error_description is response content like any other."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        body = json.dumps(
            {"error": "invalid_grant", "error_description": "SECRETMARKER anything"}
        ).encode()
        err = _http_error(400)
        err.read = lambda: body
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(auth.TokenRefused) as excinfo:
                auth.refresh_google_token()
        assert "SECRETMARKER" not in str(excinfo.value)

    def test_another_400_is_still_a_refusal_without_the_publish_advice(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        body = json.dumps({"error": "invalid_client"}).encode()
        err = _http_error(400)
        err.read = lambda: body
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(auth.TokenRefused) as excinfo:
                auth.refresh_google_token()
        assert "Publish" not in str(excinfo.value)

    def test_an_unreadable_error_body_still_refuses(self, tmp_path):
        """Parsing the body must not turn a refusal into an unhandled failure."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        err = _http_error(400)
        err.read = lambda: b"<html>not json</html>"
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(auth.TokenRefused):
                auth.refresh_google_token()


class TestWhatARefreshStores:
    def _respond(self, payload):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        return resp

    def test_a_response_without_a_refresh_token_keeps_the_stored_one(self, tmp_path):
        """Google's refresh tokens do not rotate, so a response omits it."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        with patch("urllib.request.urlopen", return_value=self._respond({"access_token": "new"})):
            assert auth.refresh_google_token() == "new"
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["refresh_token"] == "stored-refresh"

    def test_an_access_token_defaults_to_an_hour(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        with patch("urllib.request.urlopen", return_value=self._respond({"access_token": "new"})):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert abs(stored["expires_at"] - (time.time() + config.GOOGLE_TOKEN_LIFETIME)) < 5

    def test_a_granted_expiry_is_stored_as_the_instant_it_falls_due(self, tmp_path):
        """A duration cannot be read twice: nobody knows if a repeat counts down."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        payload = {"access_token": "new", "refresh_token_expires_in": 604799}
        with patch("urllib.request.urlopen", return_value=self._respond(payload)):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert abs(stored["refresh_token_expires_at"] - (time.time() + 604799)) < 5

    def test_a_refresh_does_not_move_the_expiry_it_was_told_about(self, tmp_path):
        """Recomputing hourly would push a restated constant past every deadline.

        The stored instant is carried, not refreshed: a response that repeats
        the field has only been measured against a published app, so whether
        a repeat counts down or restates the grant is unknown, and carrying
        it means the same thing either way.
        """
        _write_client(tmp_path)
        granted = time.time() + 600
        _write_tokens(tmp_path, refresh_token_expires_at=granted)
        payload = {"access_token": "new", "refresh_token_expires_in": 604799}
        with patch("urllib.request.urlopen", return_value=self._respond(payload)):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["refresh_token_expires_at"] == granted

    def test_a_fresh_grant_starts_without_one(self):
        """Publishing the app and re-authorising is what clears it: consent has no previous."""
        assert "refresh_token_expires_at" not in auth._google_token_store({"access_token": "a"}, {})

    @pytest.mark.parametrize("value", ["604799", True, {}], ids=["str", "bool", "dict"])
    def test_an_unusable_granted_expiry_records_nothing(self, value):
        """Adding it to a timestamp would store a date nobody can read back."""
        store = auth._google_token_store(
            {"access_token": "a", "refresh_token_expires_in": value}, {}
        )
        assert "refresh_token_expires_at" not in store

    @pytest.mark.parametrize("value", ["3600", None, {}, True], ids=["str", "null", "dict", "bool"])
    def test_an_unusable_expiry_falls_back_rather_than_raising(self, tmp_path, value):
        """The arithmetic runs in setup's main thread, where nothing catches a TypeError."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        payload = {"access_token": "new", "expires_in": value}
        with patch("urllib.request.urlopen", return_value=self._respond(payload)):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert abs(stored["expires_at"] - (time.time() + config.GOOGLE_TOKEN_LIFETIME)) < 5

    def test_a_response_with_no_token_is_a_refusal(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        with patch("urllib.request.urlopen", return_value=self._respond({"scope": "..."})):
            with pytest.raises(auth.TokenRefused):
                auth.refresh_google_token()


class TestTheAuthorisationUrl:
    def test_it_asks_for_a_refresh_token_every_time(self):
        """access_type=offline yields one at all; prompt=consent yields one again.

        Without the second, a user who has consented before gets an
        authorisation that returns no refresh token and unattended sync
        cannot work - and the flow looks like it succeeded.
        """
        url = auth._google_auth_url("challenge-value", "fake-client-id")
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "code_challenge_method=S256" in url
        assert "challenge-value" in url
