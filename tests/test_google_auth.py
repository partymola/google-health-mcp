"""Google OAuth: the classification boundary and the Testing-status diagnostic.

Mirrors TestTheRefreshBoundary in test_api.py. The two-type boundary is not
optional here: api.get and doctor's grading are built on it, so a Google-only
exception type would reach code that has no branch for it.
"""

import json
import math
import os
import threading
import time
import urllib.error
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

import pytest

from google_health_mcp import auth, config


@pytest.fixture(autouse=True)
def _isolate_google_state(monkeypatch, tmp_path):
    """No test may read the developer's real client file or token file."""
    monkeypatch.setattr(config, "GOOGLE_CLIENT_PATH", tmp_path / "google_client.json")
    monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", tmp_path / "google_tokens.json")


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


def _respond(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


_NOT_A_TOKEN = pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "a\nb",
        "a\tb",
        "a\x00b",
        "a\x7fb",
        "中",
        "é",
        123,
        True,
        {"value": "x"},
        ["x"],
    ],
    ids=[
        "null",
        "empty",
        "blank",
        "newline",
        "tab",
        "nul",
        "delete",
        "non-ascii",
        "latin-1",
        "number",
        "bool",
        "object",
        "array",
    ],
)


class TestTheTokenSyntax:
    """RFC 6749's VSCHAR rule, which is wider than what a token usually looks like."""

    @pytest.mark.parametrize(
        "value",
        ["ya29.a0AfB_c-d~+/=", "1//0gabc-_DEF", "!", "~", "a b"],
        ids=["access-shape", "refresh-shape", "lowest", "highest", "interior-space"],
    )
    def test_a_token_in_the_syntax_is_accepted(self, value):
        assert auth._is_token(value)

    @_NOT_A_TOKEN
    def test_anything_else_is_refused(self, value):
        assert not auth._is_token(value)


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

    def test_the_message_carries_nothing_of_the_failure_it_absorbed(self, monkeypatch):
        """The catch-all is handed an arbitrary exception, and this is where its
        text would leak.

        `run_sync` writes this message into `sync_log` and returns it to the
        client, and an unanticipated exception's own text is an absolute path
        often enough. Asserted here rather than at the client, where
        `google_get` substitutes a fixed message either way and would mask it.
        """
        path = "/home/someone/certs/ca.pem"
        monkeypatch.setattr(
            auth, "_refresh_google_token", MagicMock(side_effect=RuntimeError(path))
        )
        with pytest.raises(auth.RefreshNetworkError) as caught:
            auth.refresh_google_token()
        assert str(caught.value) == "Could not obtain a token from Google."
        assert "/home/someone" not in str(caught.value)

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

    @pytest.mark.parametrize(
        "body",
        [
            {"scope": "granted"},
            {"refresh_token": "reissued"},
            {"access_token": None},
            {"access_token": ""},
            {"access_token": "   "},
            {"access_token": "a\nb"},
            {"access_token": "中"},
            {"access_token": 123},
            {"access_token": True},
            {"access_token": {"value": "x"}},
            {"access_token": ["x"]},
            ["access_token"],
            "access_token",
            7,
            None,
        ],
        ids=[
            "absent",
            "refresh-token-only",
            "null",
            "empty",
            "blank",
            "newline",
            "non-ascii",
            "number",
            "bool",
            "object",
            "array",
            "body-array",
            "body-string",
            "body-number",
            "body-null",
        ],
    )
    def test_a_200_with_no_usable_token_is_not_a_refusal(self, tmp_path, body):
        """RFC 6749 refuses with a 400 or 401, so this judged nothing: "run auth"
        would rewrite a token file another host may own, for a grant still good."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        before = (tmp_path / "google_tokens.json").read_bytes()
        answer = _respond(body)
        with patch("urllib.request.urlopen", return_value=answer):
            with pytest.raises(auth.RefreshNetworkError) as caught:
                auth.refresh_google_token()
        assert str(caught.value) == "Google returned no access token."
        assert (tmp_path / "google_tokens.json").read_bytes() == before

    def test_a_200_with_no_token_logs_fixed_text_once(self, tmp_path, caplog):
        """The client reports it as a network error, so the log is what tells them apart."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        answer = _respond({"scope": "SECRETMARKER"})
        with caplog.at_level("WARNING", logger=auth.__name__):
            with patch("urllib.request.urlopen", return_value=answer):
                with pytest.raises(auth.RefreshNetworkError):
                    auth.refresh_google_token()
        assert [r.getMessage() for r in caplog.records] == [
            "Google answered a refresh with no access token"
        ]

    def test_a_forced_refresh_with_no_token_raises_and_keeps_the_valid_one(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path, expires_at=time.time() + 3600)
        before = (tmp_path / "google_tokens.json").read_bytes()
        answer = _respond({"scope": "granted"})
        with patch("urllib.request.urlopen", return_value=answer):
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_google_token(force=True)
        assert (tmp_path / "google_tokens.json").read_bytes() == before
        assert auth.refresh_google_token() == "stored-access"


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
    def test_a_response_without_a_refresh_token_keeps_the_stored_one(self, tmp_path):
        """Google's refresh tokens do not rotate, so a response omits it."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        with patch("urllib.request.urlopen", return_value=_respond({"access_token": "new"})):
            assert auth.refresh_google_token() == "new"
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["refresh_token"] == "stored-refresh"

    @_NOT_A_TOKEN
    def test_an_unusable_refresh_token_keeps_the_stored_one(self, tmp_path, value):
        """The file holds the only copy, so saving this loses a grant that still works."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        payload = {"access_token": "new", "refresh_token": value}
        with patch("urllib.request.urlopen", return_value=_respond(payload)):
            assert auth.refresh_google_token() == "new"
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["refresh_token"] == "stored-refresh"

    def test_a_usable_refresh_token_replaces_the_stored_one(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        payload = {"access_token": "new", "refresh_token": "reissued"}
        with patch("urllib.request.urlopen", return_value=_respond(payload)):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["refresh_token"] == "reissued"

    def test_a_whole_answer_in_googles_shape_is_stored(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        payload = {
            "access_token": "ya29.a0AfB_c-d~+/=",
            "expires_in": 3599,
            "scope": "fictional-scope",
            "token_type": "Bearer",
        }
        with patch("urllib.request.urlopen", return_value=_respond(payload)):
            assert auth.refresh_google_token() == "ya29.a0AfB_c-d~+/="
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["access_token"] == "ya29.a0AfB_c-d~+/="
        assert stored["refresh_token"] == "stored-refresh"
        assert stored["scope"] == "fictional-scope"
        assert abs(stored["expires_at"] - (time.time() + 3599)) < 5

    def test_an_access_token_defaults_to_an_hour(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        with patch("urllib.request.urlopen", return_value=_respond({"access_token": "new"})):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert abs(stored["expires_at"] - (time.time() + config.GOOGLE_TOKEN_LIFETIME)) < 5

    def test_a_granted_expiry_is_stored_as_the_instant_it_falls_due(self, tmp_path):
        """A duration cannot be read twice: nobody knows if a repeat counts down."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        payload = {"access_token": "new", "refresh_token_expires_in": 604799}
        with patch("urllib.request.urlopen", return_value=_respond(payload)):
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
        with patch("urllib.request.urlopen", return_value=_respond(payload)):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["refresh_token_expires_at"] == granted

    def test_a_fresh_grant_starts_without_one(self):
        """Publishing the app and re-authorising is what clears it: consent has no previous."""
        assert "refresh_token_expires_at" not in auth._google_token_store({"access_token": "a"}, {})

    @pytest.mark.parametrize(
        "value",
        ["604799", True, {}, float("inf"), float("-inf"), float("nan"), 10**400],
        ids=["str", "bool", "dict", "infinity", "negative-infinity", "nan", "oversized"],
    )
    def test_an_unusable_granted_expiry_records_nothing(self, value):
        """Adding it to a timestamp would store a date nobody can read back."""
        store = auth._google_token_store(
            {"access_token": "a", "refresh_token_expires_in": value}, {}
        )
        assert "refresh_token_expires_at" not in store

    @pytest.mark.parametrize(
        "value",
        [float("inf"), "soon", {"x": 1}, True],
        ids=["infinity", "str", "dict", "bool"],
    )
    def test_a_stored_granted_expiry_that_is_not_a_number_is_replaced(self, value):
        """Kept, it reads as no expiry at all and hides doctor's short-lived warning."""
        previous = {"refresh_token_expires_at": value}
        assert "refresh_token_expires_at" not in auth._google_token_store(
            {"access_token": "a"}, previous
        )
        store = auth._google_token_store(
            {"access_token": "a", "refresh_token_expires_in": 604799}, previous
        )
        assert abs(store["refresh_token_expires_at"] - (time.time() + 604799)) < 5

    @pytest.mark.parametrize(
        "value",
        ["3600", None, {}, True, float("inf"), float("-inf"), float("nan"), 10**400],
        ids=["str", "null", "dict", "bool", "infinity", "negative-infinity", "nan", "oversized"],
    )
    def test_an_unusable_expiry_falls_back_rather_than_raising(self, tmp_path, value):
        """The arithmetic runs in setup's main thread, where nothing catches a TypeError.
        JSON here parses `Infinity`, and a token stored with it is never refreshed."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        payload = {"access_token": "new", "expires_in": value}
        with patch("urllib.request.urlopen", return_value=_respond(payload)):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert abs(stored["expires_at"] - (time.time() + config.GOOGLE_TOKEN_LIFETIME)) < 5

    def test_a_forced_refresh_asks_even_while_the_token_is_valid(self, tmp_path):
        """How a sync learns the scopes of a token from before they were recorded."""
        _write_client(tmp_path)
        _write_tokens(tmp_path, expires_at=time.time() + 3600)
        payload = {"access_token": "new", "scope": "granted"}
        with patch("urllib.request.urlopen", return_value=_respond(payload)) as request:
            assert auth.refresh_google_token() == "stored-access"
            assert not request.called
            assert auth.refresh_google_token(force=True) == "new"
        assert auth.granted_scopes() == "granted"


class TestTheTokenFileIsTheOnlyCopy:
    """`google-health-mcp auth` runs in another process while the server runs.

    Each test writes real files at the paths the fixture above points `config`
    at, because the property is about what is on disk, not about anything the
    module holds.
    """

    @staticmethod
    def _rewrite_unnoticeably(path, text):
        """Same length and mtime as before, so only the content says it changed.

        A cache keyed on size or modification time would serve the old copy;
        mtime resolution is coarse on some filesystems, so it can miss a real
        rewrite.
        """
        before = path.stat()
        assert len(path.read_text()) == len(text)
        path.write_text(text)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))

    def _urlopen_writing(self, tmp_path, then, raw=None, **tokens):
        """An urlopen that rewrites the token file mid-request, then answers.

        `raw` is written as the file's text; otherwise `tokens` override the
        default token fields.
        """

        def urlopen(request, timeout=None):
            if raw is not None:
                (tmp_path / "google_tokens.json").write_text(raw)
            else:
                _write_tokens(tmp_path, **tokens)
            if isinstance(then, Exception):
                raise then
            return _respond(then)

        return urlopen

    def test_a_token_written_by_auth_is_used_without_a_restart(self, tmp_path):
        """Held in memory, the old grant stays in use after `auth` until a
        restart, which after a Testing-mode expiry means a dead refresh token."""
        _write_client(tmp_path)
        _write_tokens(tmp_path, access_token="token_aaaa", expires_at=time.time() + 3600)
        assert auth.refresh_google_token() == "token_aaaa"
        path = tmp_path / "google_tokens.json"
        self._rewrite_unnoticeably(path, path.read_text().replace("token_aaaa", "token_bbbb"))
        assert auth.refresh_google_token() == "token_bbbb"

    def test_a_new_client_file_is_used_without_a_restart(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        sent = []

        def urlopen(request, timeout=None):
            sent.append(parse_qs(request.data.decode())["client_id"][0])
            return _respond({"access_token": "new"})

        with patch("urllib.request.urlopen", urlopen):
            auth.refresh_google_token(force=True)
            path = tmp_path / "google_client.json"
            self._rewrite_unnoticeably(path, path.read_text().replace("fake-client", "fake-clienz"))
            auth.refresh_google_token(force=True)
        assert sent == [
            "fake-client.apps.googleusercontent.com",
            "fake-clienz.apps.googleusercontent.com",
        ]

    def test_a_refresh_does_not_overwrite_a_consent_saved_while_it_ran(self, tmp_path):
        """Saving over it puts back the old refresh token and the old scopes.

        The competing grant expires before the refreshed one would, as one
        issued moments earlier does, so a comparison against the refreshed
        expiry instead of the starting one fails here.
        """
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        urlopen = self._urlopen_writing(
            tmp_path,
            {"access_token": "from_refresh"},
            access_token="from_consent",
            refresh_token="consent-refresh",
            expires_at=time.time() + 1800,
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth.refresh_google_token() == "from_consent"
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert (stored["access_token"], stored["refresh_token"]) == (
            "from_consent",
            "consent-refresh",
        )

    def test_a_file_changed_only_in_its_scopes_is_not_saved_over(self, tmp_path):
        """A consent can return the same refresh token with wider scopes, so a
        change is the whole file differing, not one field of it."""
        _write_client(tmp_path)
        _write_tokens(tmp_path, scope="narrow")
        urlopen = self._urlopen_writing(
            tmp_path, {"access_token": "from_refresh"}, scope="narrow wider"
        )
        with patch("urllib.request.urlopen", urlopen):
            auth.refresh_google_token()
        assert json.loads((tmp_path / "google_tokens.json").read_text())["scope"] == "narrow wider"

    def test_a_forced_refresh_does_not_overwrite_a_consent_with_an_earlier_expiry(self, tmp_path):
        """A consent written on a host whose clock runs behind can carry an
        earlier expiry than a token that is still valid here, which is the
        case a forced refresh starts from. Comparing the two expiries saves
        the old grant over it."""
        _write_client(tmp_path)
        _write_tokens(tmp_path, expires_at=time.time() + 3000)
        urlopen = self._urlopen_writing(
            tmp_path,
            {"access_token": "from_refresh"},
            access_token="from_consent",
            refresh_token="consent-refresh",
            expires_at=time.time() + 2400,
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth.refresh_google_token(force=True) == "from_consent"
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["refresh_token"] == "consent-refresh"

    def test_a_changed_file_holding_an_expired_token_is_neither_used_nor_saved_over(self, tmp_path):
        """Its expiry is later than the starting one, so an expiry comparison
        returns its token, which has expired. Its refresh token may be a newer
        grant's, so the refreshed token is returned unsaved and the next call
        refreshes from the file."""
        _write_client(tmp_path)
        _write_tokens(tmp_path, expires_at=50)
        urlopen = self._urlopen_writing(
            tmp_path,
            {"access_token": "from_refresh"},
            access_token="restored",
            refresh_token="other-refresh",
            expires_at=100,
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth.refresh_google_token() == "from_refresh"
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert (stored["access_token"], stored["refresh_token"]) == ("restored", "other-refresh")

    def test_a_refresh_does_not_overwrite_a_file_caught_mid_write(self, tmp_path):
        """Unreadable after the refresh most likely means another writer is
        part-way through, so the token just obtained is returned unsaved."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        urlopen = self._urlopen_writing(tmp_path, {"access_token": "from_refresh"}, raw="{")
        with patch("urllib.request.urlopen", urlopen):
            assert auth.refresh_google_token() == "from_refresh"
        assert (tmp_path / "google_tokens.json").read_text() == "{"

    @pytest.mark.parametrize("code", sorted(auth._REFUSAL_CODES))
    def test_a_refusal_after_the_file_was_replaced_uses_the_new_file(self, tmp_path, code):
        """The refusal judged a refresh token `auth` had already replaced.
        Reporting it as a refusal tells the user to re-authorise over a working
        grant."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        urlopen = self._urlopen_writing(
            tmp_path,
            _http_error(code),
            access_token="from_consent",
            refresh_token="consent-refresh",
            expires_at=time.time() + 3600,
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth.refresh_google_token() == "from_consent"

    @pytest.mark.parametrize("code", sorted(auth._REFUSAL_CODES))
    def test_a_refusal_after_the_file_changed_to_an_expired_token_is_not_a_refusal(
        self, tmp_path, code
    ):
        """The refusal says nothing about the refresh token now in the file, so
        the next call refreshes with it rather than this one sending the user
        to re-authorise."""
        _write_client(tmp_path)
        _write_tokens(tmp_path, expires_at=100)
        urlopen = self._urlopen_writing(
            tmp_path,
            _http_error(code),
            access_token="restored",
            refresh_token="other-refresh",
            expires_at=50,
        )
        with patch("urllib.request.urlopen", urlopen):
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_google_token()

    def test_a_200_with_no_token_leaves_a_file_written_during_the_request(self, tmp_path):
        """The next call reads the new file, so this one has nothing to save."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        urlopen = self._urlopen_writing(
            tmp_path,
            {"scope": "granted"},
            access_token="from_consent",
            refresh_token="consent-refresh",
            expires_at=time.time() + 3600,
        )
        with patch("urllib.request.urlopen", urlopen):
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert (stored["access_token"], stored["refresh_token"]) == (
            "from_consent",
            "consent-refresh",
        )
        assert auth.refresh_google_token() == "from_consent"

    def test_a_stored_token_with_no_access_token_is_refreshed_rather_than_sent(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path, access_token="", expires_at=time.time() + 3600)
        with patch("urllib.request.urlopen", return_value=_respond({"access_token": "new"})):
            assert auth.refresh_google_token() == "new"

    @_NOT_A_TOKEN
    def test_a_stored_access_token_that_is_not_a_token_is_refreshed_rather_than_sent(
        self, tmp_path, value
    ):
        _write_client(tmp_path)
        _write_tokens(tmp_path, access_token=value, expires_at=time.time() + 3600)
        with patch("urllib.request.urlopen", return_value=_respond({"access_token": "new"})):
            assert auth.refresh_google_token() == "new"

    @_NOT_A_TOKEN
    def test_a_stored_refresh_token_that_is_not_a_token_is_not_sent(self, tmp_path, value):
        """The credential file itself is what judged it, so this one is a refusal."""
        _write_client(tmp_path)
        _write_tokens(tmp_path, refresh_token=value)
        with patch("urllib.request.urlopen") as request:
            with pytest.raises(auth.TokenRefused) as caught:
                auth.refresh_google_token()
        assert not request.called
        assert str(caught.value) == "No Google refresh token. Run: google-health-mcp auth"

    def test_a_changed_file_holding_a_token_that_is_not_one_is_not_used(self, tmp_path):
        """It would be sent in place of the token this refresh just obtained."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        urlopen = self._urlopen_writing(
            tmp_path, {"access_token": "from_refresh"}, access_token=123, expires_at=9e12
        )
        with patch("urllib.request.urlopen", urlopen):
            assert auth.refresh_google_token() == "from_refresh"

    @pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN", "1e400"])
    def test_a_stored_expiry_that_is_not_finite_is_refreshed(self, tmp_path, literal):
        """An earlier release could write these. `Infinity` never refreshes, and
        the refresh has to replace the value or the next call asks again."""
        _write_client(tmp_path)
        (tmp_path / "google_tokens.json").write_text(
            '{"access_token": "stored-access", "refresh_token": "stored-refresh", '
            f'"expires_at": {literal}}}'
        )
        answer = _respond({"access_token": "new"})
        with patch("urllib.request.urlopen", return_value=answer) as request:
            assert auth.refresh_google_token() == "new"
            assert auth.refresh_google_token() == "new"
        assert request.call_count == 1
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert math.isfinite(stored["expires_at"])

    def test_a_valid_token_does_not_need_the_client_file(self, tmp_path):
        """Only a refresh uses it, so a moment when it cannot be read must not
        refuse a token that needs no refresh."""
        _write_tokens(tmp_path, expires_at=time.time() + 3600)
        assert auth.refresh_google_token() == "stored-access"

    def test_concurrent_calls_refresh_once(self, tmp_path):
        """Tool calls run in worker threads. Unserialised, a second call reads
        the file while the first is saving it, or refreshes a second time."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        posts = []
        second = {}

        def call_from_another_thread():
            second["token"] = auth.refresh_google_token()

        other = threading.Thread(target=call_from_another_thread)

        def urlopen(request, timeout=None):
            posts.append(request)
            if len(posts) == 1:
                other.start()
                # Long enough for an unserialised second call to reach the
                # network; with the lock it is still waiting when this returns.
                other.join(timeout=0.5)
            return _respond({"access_token": f"from_refresh_{len(posts)}"})

        with patch("urllib.request.urlopen", urlopen):
            first = auth.refresh_google_token()
            other.join(timeout=5)

        assert len(posts) == 1
        assert second["token"] == first

    @pytest.mark.parametrize("code", sorted(auth._REFUSAL_CODES))
    def test_a_refusal_while_the_file_is_unreadable_is_not_a_refusal(self, tmp_path, code):
        """The file is most likely part-way through being replaced, which clears
        on its own; a refusal would tell the user to re-authorise over it."""
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        urlopen = self._urlopen_writing(tmp_path, _http_error(code), raw="{")
        with patch("urllib.request.urlopen", urlopen):
            with pytest.raises(auth.RefreshNetworkError):
                auth.refresh_google_token()


class TestTheGrantedScopesAreRecorded:
    """What Google says it granted is kept, so a missing permission can be named.

    A grant does not gain scopes on refresh, so a release that asks for a new
    one leaves every existing install without it until the user re-consents,
    and nothing else on disk can say so.
    """

    _GRANTED = (
        "https://www.googleapis.com/auth/googlehealth.sleep.readonly "
        "https://www.googleapis.com/auth/googlehealth.ecg.readonly"
    )

    def test_a_refresh_records_what_it_reports(self, tmp_path):
        _write_client(tmp_path)
        _write_tokens(tmp_path)
        payload = {"access_token": "new", "scope": self._GRANTED}
        with patch("urllib.request.urlopen", return_value=_respond(payload)):
            auth.refresh_google_token()
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["scope"] == self._GRANTED

    def test_a_response_that_omits_it_keeps_the_stored_one(self):
        store = auth._google_token_store({"access_token": "a"}, {"scope": self._GRANTED})
        assert store["scope"] == self._GRANTED

    def test_a_consent_that_reports_none_records_none(self):
        """Unknown is not empty: an empty string would read as no permission at all."""
        assert "scope" not in auth._google_token_store({"access_token": "a"}, {})

    def test_a_refresh_of_a_token_from_before_scopes_were_recorded_records_none(self):
        store = auth._google_token_store({"access_token": "a"}, {"refresh_token": "r"})
        assert "scope" not in store

    @pytest.mark.parametrize(
        "value",
        [["a"], 7, None, True, "", "   "],
        ids=["list", "int", "null", "bool", "empty", "blank"],
    )
    def test_an_unusable_one_keeps_the_stored_one(self, value):
        store = auth._google_token_store(
            {"access_token": "a", "scope": value}, {"scope": self._GRANTED}
        )
        assert store["scope"] == self._GRANTED

    @pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
    def test_a_consent_reporting_a_blank_one_records_none(self, value):
        """Blank reads as a grant holding nothing, so every later sync skips every type."""
        assert "scope" not in auth._google_token_store({"access_token": "a", "scope": value}, {})

    @pytest.mark.parametrize("value", ["", "   "], ids=["empty", "blank"])
    def test_a_blank_one_on_disk_is_not_carried_forward(self, value):
        """Carried, it goes on reading as a grant holding nothing after the refresh."""
        assert "scope" not in auth._google_token_store({"access_token": "a"}, {"scope": value})


class TestFinishingAConsent:
    """What happens once Google has handed back tokens.

    The account check exists because picking the wrong account on the consent
    screen produces a grant that works and reads nothing: every request then
    answers ACCOUNT_NOT_LINKED, and until this check that was discovered at
    the first sync, if at all.
    """

    _RAW = {"access_token": "a", "refresh_token": "r", "scope": "x"}

    def _finish(self, tmp_path, probe, **overrides):
        from google_health_mcp import api

        _write_client(tmp_path)
        with patch.object(api, "google_get", probe):
            try:
                auth._finish_consent({**self._RAW, **overrides})
            except SystemExit as stop:
                return stop.code
        return 0

    def test_a_whole_answer_in_googles_shape_is_saved(self, tmp_path, capsys):
        answer = {
            "access_token": "ya29.a0AfB_c-d~+/=",
            "refresh_token": "1//0gabc-_DEF",
            "expires_in": 3599,
            "scope": "fictional-scope",
            "token_type": "Bearer",
        }
        assert self._finish(tmp_path, MagicMock(return_value={}), **answer) == 0
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert stored["access_token"] == "ya29.a0AfB_c-d~+/="
        assert stored["refresh_token"] == "1//0gabc-_DEF"
        assert stored["scope"] == "fictional-scope"
        assert abs(stored["expires_at"] - (time.time() + 3599)) < 5

    @_NOT_A_TOKEN
    def test_a_consent_with_no_usable_access_token_saves_nothing(self, tmp_path, capsys, value):
        _write_tokens(tmp_path)
        before = (tmp_path / "google_tokens.json").read_bytes()
        assert self._finish(tmp_path, MagicMock(return_value={}), access_token=value) == 1
        assert "Google returned no access token." in capsys.readouterr().err
        assert (tmp_path / "google_tokens.json").read_bytes() == before

    @_NOT_A_TOKEN
    def test_a_consent_with_no_usable_refresh_token_saves_nothing(self, tmp_path, capsys, value):
        """Saved, unattended sync stops an hour later with "Setup complete" on screen."""
        _write_tokens(tmp_path)
        before = (tmp_path / "google_tokens.json").read_bytes()
        assert self._finish(tmp_path, MagicMock(return_value={}), refresh_token=value) == 1
        assert "Google returned no refresh token" in capsys.readouterr().err
        assert (tmp_path / "google_tokens.json").read_bytes() == before

    def test_a_lifetime_too_large_for_a_float_does_not_end_in_a_traceback(self, tmp_path):
        """It runs in the main thread, where nothing else would catch the OverflowError."""
        code = self._finish(
            tmp_path,
            MagicMock(return_value={}),
            expires_in=10**400,
            refresh_token_expires_in=10**400,
        )
        assert code == 0
        stored = json.loads((tmp_path / "google_tokens.json").read_text())
        assert abs(stored["expires_at"] - (time.time() + config.GOOGLE_TOKEN_LIFETIME)) < 5
        assert "refresh_token_expires_at" not in stored

    def test_a_short_lived_grant_is_warned_about(self, tmp_path, capsys):
        assert (
            self._finish(tmp_path, MagicMock(return_value={}), refresh_token_expires_in=604799) == 0
        )
        assert "expires in about a week" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "value",
        ["soon", True, {}, float("inf"), float("nan"), 10**400],
        ids=["str", "bool", "dict", "infinity", "nan", "oversized"],
    )
    def test_a_lifetime_that_records_no_expiry_is_not_warned_about(self, tmp_path, capsys, value):
        """The warning says a week, and nothing recorded says so."""
        assert (
            self._finish(tmp_path, MagicMock(return_value={}), refresh_token_expires_in=value) == 0
        )
        assert "about a week" not in capsys.readouterr().out

    @pytest.mark.parametrize("body", [["access_token"], "access_token", 7])
    def test_a_consent_answer_that_is_not_an_object_saves_nothing(self, tmp_path, capsys, body):
        """It runs in the main thread, where nothing else would catch the traceback."""
        _write_tokens(tmp_path)
        before = (tmp_path / "google_tokens.json").read_bytes()
        with pytest.raises(SystemExit) as stop:
            auth._finish_consent(body)
        assert stop.value.code == 1
        assert "Google returned no access token." in capsys.readouterr().err
        assert (tmp_path / "google_tokens.json").read_bytes() == before

    def test_the_tokens_are_saved_before_the_account_is_checked(self, tmp_path, capsys):
        """Saving first means a wrong-account grant is replaced by re-running
        auth, never left half-written."""
        from google_health_mcp import api

        code = self._finish(tmp_path, MagicMock(side_effect=api.AccountNotLinked("x")))
        assert (tmp_path / "google_tokens.json").exists()
        assert code == 1

    def test_the_wrong_account_is_named_and_fails_the_command(self, tmp_path, capsys):
        from google_health_mcp import api

        code = self._finish(tmp_path, MagicMock(side_effect=api.AccountNotLinked("x")))
        assert code == 1
        assert api.ACCOUNT_NOT_LINKED_MESSAGE in capsys.readouterr().err

    def test_a_linked_account_finishes_cleanly(self, tmp_path, capsys):
        assert self._finish(tmp_path, MagicMock(return_value={})) == 0
        assert "Tokens saved" in capsys.readouterr().out

    def test_a_check_that_cannot_run_does_not_fail_the_consent(self, tmp_path):
        """A transient failure says nothing about the account."""
        from google_health_mcp import api

        assert self._finish(tmp_path, MagicMock(side_effect=api.HealthAPIError("net"))) == 0

    def test_a_refused_check_does_not_blame_the_account(self, tmp_path, capsys):
        """A grant without the settings scope is refused the device list; that
        is a permission, not the wrong account."""
        from google_health_mcp import api

        code = self._finish(tmp_path, MagicMock(side_effect=api.HealthAuthError("403")))
        assert code == 0
        assert api.ACCOUNT_NOT_LINKED_MESSAGE not in capsys.readouterr().err

    def test_the_check_uses_the_token_just_saved(self, tmp_path):
        """A token held from before the consent would check the old account."""
        _write_client(tmp_path)
        _write_tokens(tmp_path, access_token="stale", expires_at=9e12)
        assert auth.refresh_google_token() == "stale"
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request.get_header("Authorization"))
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b"{}"
            return response

        with patch("urllib.request.urlopen", urlopen):
            auth._finish_consent(dict(self._RAW))
        assert sent == ["Bearer a"]

    def test_setup_hands_the_tokens_to_it(self, tmp_path):
        """The one line joining the interactive flow to everything tested above,
        driven through a callback carrying a code rather than read off the source."""
        _write_client(tmp_path)
        tokens = {"access_token": "a", "refresh_token": "r"}

        class Server:
            def __init__(self, address, handler):
                self.handler = handler

            def handle_request(self):
                h = self.handler.__new__(self.handler)
                h.path = "/?code=fake-code"
                h._respond = lambda *a: None
                h.do_GET()

            def server_close(self):
                pass

        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(tokens).encode()
        with (
            patch.object(auth, "_CallbackServer", Server),
            patch("webbrowser.open"),
            patch("urllib.request.urlopen", return_value=response),
            patch.object(auth, "_finish_consent") as finish,
        ):
            auth.setup_google_auth()
        finish.assert_called_once_with(tokens)


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
