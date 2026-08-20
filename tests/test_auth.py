"""Tests for the OAuth authentication module."""

import json
import os
import socket
import sys
import time

import pytest

from google_health_mcp import auth
from google_health_mcp.auth import _generate_pkce, _load_json, _save_json, refresh_google_token

# Not `hasattr(os, "fchmod")`: CPython 3.13 added os.fchmod on Windows, so that
# test stopped selecting POSIX and the mode assertions below ran there anyway.
skip_non_posix = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX mode bits; Windows uses ACLs"
)


class TestPKCE:
    """Test PKCE code verifier/challenge generation."""

    def test_verifier_length(self):
        verifier, _ = _generate_pkce()
        assert 43 <= len(verifier) <= 128

    def test_challenge_is_base64url(self):
        _, challenge = _generate_pkce()
        # Base64url: only alphanumeric, hyphen, underscore (no padding)
        import re

        assert re.match(r"^[A-Za-z0-9_-]+$", challenge)

    def test_different_each_call(self):
        v1, c1 = _generate_pkce()
        v2, c2 = _generate_pkce()
        assert v1 != v2
        assert c1 != c2

    def test_challenge_matches_verifier(self):
        """Verify the challenge is the SHA256 of the verifier."""
        import base64
        import hashlib

        verifier, challenge = _generate_pkce()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == expected


class TestSaveLoadJson:
    """Test secure JSON file I/O."""

    def test_save_creates_file(self, tmp_path):
        path = tmp_path / "test.json"
        _save_json(path, {"key": "value"})
        assert path.exists()

    @skip_non_posix
    def test_save_permissions(self, tmp_path):
        path = tmp_path / "test.json"
        _save_json(path, {"key": "value"})
        mode = oct(os.stat(path).st_mode & 0o777)
        assert mode == "0o600"

    def test_save_creates_parents(self, tmp_path):
        path = tmp_path / "subdir" / "deep" / "test.json"
        _save_json(path, {"key": "value"})
        assert path.exists()

    def test_roundtrip(self, tmp_path):
        path = tmp_path / "test.json"
        data = {"client_id": "abc123", "nested": {"a": 1}}
        _save_json(path, data)
        loaded = _load_json(path)
        assert loaded == data

    def test_overwrite_truncates_rather_than_leaving_a_tail(self, tmp_path):
        """A shorter second payload must not leave bytes from the first.

        Equal-length payloads cannot catch a missing O_TRUNC: the second write
        covers the first exactly. A leftover tail makes the file unparseable,
        which now reads as unusable credentials and sends the user to
        re-authorise - rotating a token file the syncing host owns.
        """
        path = tmp_path / "tokens.json"
        _save_json(path, {"padding": "x" * 500, "v": 1})
        _save_json(path, {"v": 2})
        assert json.loads(path.read_text()) == {"v": 2}


class TestRefreshToken:
    """The cache short-circuit, which is what keeps a sync off the token endpoint."""

    @pytest.fixture(autouse=True)
    def _restore_the_cache(self, monkeypatch):
        import google_health_mcp.auth as auth

        monkeypatch.setattr(auth, "_cached_google_client", {"client_id": "fictional-client"})
        yield
        auth._cached_google_tokens = None
        auth._cached_google_client = None

    def test_returns_cached_if_not_expired(self, monkeypatch):
        """An unexpired token is returned without reaching the network at all."""
        import google_health_mcp.auth as auth

        monkeypatch.setattr(
            auth,
            "_cached_google_tokens",
            {
                "access_token": "valid_token",
                "refresh_token": "refresh_abc",
                "expires_at": time.time() + 3600,
            },
        )
        monkeypatch.setattr(
            auth.urllib.request, "urlopen", _refuse_the_network("refresh with a live token")
        )

        assert refresh_google_token() == "valid_token"

    def test_a_token_inside_the_buffer_is_refreshed_rather_than_served(self, monkeypatch):
        """Expiring in two minutes is treated as expired.

        Without the buffer a token can pass the check and be rejected by the
        time the request lands, which surfaces as an auth failure on a
        credential that was fine.
        """
        import google_health_mcp.auth as auth

        monkeypatch.setattr(
            auth,
            "_cached_google_tokens",
            {
                "access_token": "nearly_stale",
                "refresh_token": "",
                "expires_at": time.time() + 120,
            },
        )

        with pytest.raises(RuntimeError, match="refresh token"):
            refresh_google_token()

    def test_raises_if_no_refresh_token(self, monkeypatch):
        import google_health_mcp.auth as auth

        monkeypatch.setattr(
            auth,
            "_cached_google_tokens",
            {
                "access_token": "expired_token",
                "refresh_token": "",
                "expires_at": time.time() - 600,
            },
        )

        with pytest.raises(RuntimeError, match="refresh token"):
            refresh_google_token()


def _refuse_the_network(what):
    def refuse(*_args, **_kwargs):
        raise AssertionError(f"reached the network to {what}")

    return refuse


@skip_non_posix
def test_an_existing_loose_token_file_is_tightened(tmp_path):
    """O_CREAT's mode applies only at creation, so upgrades kept 0644.

    An install predating the owner-only write keeps a world-readable refresh
    token until something narrows it, and the user should not have to know to
    run chmod by hand.
    """
    path = tmp_path / "tokens.json"
    path.write_text("{}")
    os.chmod(path, 0o644)

    _save_json(path, {"refresh_token": "fictional"})

    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_the_mode_is_set_when_the_file_is_created(tmp_path, monkeypatch):
    """Pins the open mode, not just the final one.

    fchmod corrects the mode afterwards, so asserting the result alone cannot
    tell a file that was never readable from one that was briefly 0666.
    """
    seen = {}
    real_open = os.open

    def spy(path, flags, mode=0o777, **kwargs):
        seen["mode"] = mode
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    _save_json(tmp_path / "tokens.json", {"refresh_token": "fictional"})

    assert oct(seen["mode"]) == "0o600"


def test_a_chmod_failure_does_not_destroy_the_token(tmp_path, monkeypatch):
    """O_TRUNC has already emptied the file by the time the mode is set.

    Letting a chmod failure abort the write would trade a permissions problem
    for a lost refresh token - and an empty file reads as unusable
    credentials, which sends the user to re-authorise.
    """

    def refuse(fd, mode):
        raise PermissionError("not the owner")

    monkeypatch.setattr(os, "fchmod", refuse)
    path = tmp_path / "tokens.json"
    _save_json(path, {"refresh_token": "fictional"})

    assert json.loads(path.read_text()) == {"refresh_token": "fictional"}


def _setup_flows():
    """Every interactive authorisation flow, discovered rather than listed.

    A literal list is the shape that lets a second flow inherit none of these
    pins.
    """
    from google_health_mcp import auth

    flows = [n for n in dir(auth) if n.startswith("setup_") and callable(getattr(auth, n))]
    # Guards discovery returning nothing, not the count: adding a flow is a
    # correct change, and an assertion that fails on one invites deleting the
    # assertion instead.
    assert flows, "no setup flow discovered - these pins would cover nothing"
    return flows


@pytest.mark.parametrize("flow", _setup_flows())
class TestTheCallbackPage:
    """What the browser authorisation flow shows the user.

    The callback reflects a query-string parameter back to the browser, so
    the page escapes what it displays. The assertions below that go wider -
    reading the setup function's source rather than calling it - keep the
    exception itself out of that page and out of stderr, by the two routes
    it can travel: bound to a name, and reached without one.

    Parametrized over every flow, because each handler is a closure that
    cannot be imported: a source-reading test names one function, so a
    second flow added beside it inherits none of these pins by default.
    """

    def test_a_script_tag_is_escaped(self, flow):
        from google_health_mcp.auth import _callback_page

        page = _callback_page("Error: <script>alert(1)</script>")
        assert "<script>" not in page
        assert "&lt;script&gt;" in page

    def test_the_handler_builds_its_page_through_the_helper(self, flow):
        """The helper being correct is no use if the handler stops calling it.

        The handler is a closure inside the setup function and cannot be
        imported, so
        this reads the source: every write to wfile in that function must go
        through _callback_page, never an f-string of its own.
        """
        import ast
        import inspect

        from google_health_mcp import auth

        tree = ast.parse(inspect.getsource(getattr(auth, flow)))
        writes = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "wfile"
        ]
        assert writes, "no wfile.write found - has the callback handler moved?"
        for call in writes:
            source = ast.unparse(call)
            assert "_callback_page(" in source, f"unescaped page built at: {source}"

    def test_the_handler_silences_the_default_request_log(self, flow):
        """Stop overriding log_message and the authorisation code goes to stderr.

        BaseHTTPRequestHandler logs the full request line, and the callback's
        query string carries the code that is exchanged for the tokens. No
        other pin here can see it: one looks at wfile.write calls and two look
        at except handlers, while this is a base-class method the handler
        simply stops overriding.
        """
        import ast
        import inspect

        from google_health_mcp import auth

        tree = ast.parse(inspect.getsource(getattr(auth, flow)))
        logging_methods = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("log_")
        ]
        assert any(n.name == "log_message" for n in logging_methods), (
            "handler does not override log_message - the auth code reaches stderr"
        )
        # Existing is not enough, and neither is naming log_message alone.
        # super().log_message(...) is what gets written while debugging a
        # callback that will not fire; routing it through this package's logger
        # is worse, since server mode hands stderr to the MCP client; and
        # log_request is the method whose actual job is printing the request
        # line. A log_ method that calls nothing and is undecorated cannot log,
        # whatever it is named.
        for node in logging_methods:
            calls = [ast.unparse(n.func) for n in ast.walk(node) if isinstance(n, ast.Call)]
            assert not calls, f"{node.name} still calls {calls}"
            assert not node.decorator_list, f"{node.name} is decorated"

    def test_an_ordinary_message_still_reads_normally(self, flow):
        from google_health_mcp.auth import _callback_page

        assert "Authorised! You can close this tab." in _callback_page(
            "Authorised! You can close this tab."
        )

    def test_the_exception_reaches_the_user_only_as_its_type(self, flow):
        """A whitelist, not a list of banned shapes.

        The page and stderr both carry what setup_auth formats, and the
        exception's own text is a filesystem path for a TLS failure and
        response bytes for a decode failure. Any expression form can carry
        it - a call argument, a concatenation, e.args[0], a walrus - so
        listing the ones seen so far only ever closes the last instance.
        The bound name is permitted inside type(e).__name__ and nowhere else.

        Deliberately strict about `raise X from e`: nothing here is meant to
        escape to the user's terminal as a chained traceback.
        """
        import ast
        import inspect

        from google_health_mcp import auth

        tree = ast.parse(inspect.getsource(getattr(auth, flow)))
        offenders = []
        for handler in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
            if not handler.name:
                continue
            allowed = {
                id(node.value.args[0])
                for node in ast.walk(handler)
                if isinstance(node, ast.Attribute)
                and node.attr == "__name__"
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == "type"
                and len(node.value.args) == 1
            }
            offenders += [
                ast.unparse(node)
                for node in ast.walk(handler)
                if isinstance(node, ast.Name)
                and node.id == handler.name
                and id(node) not in allowed
            ]
        assert not offenders, f"exception used outside type(e).__name__: {offenders}"

    def test_the_live_exception_is_not_reached_without_binding_it(self, flow):
        """The rule above keys on the bound name, so these bypass it entirely.

        `traceback.format_exc()` is the realistic one - it is what gets added
        while debugging a failing authorisation, and it prints source lines
        and absolute paths. Unlike the ways of stringifying an object, the
        ways of reaching the live exception without naming it are a closed
        set, so naming them is not the trap the blocklist was.
        """
        import ast
        import inspect

        from google_health_mcp import auth

        tree = ast.parse(inspect.getsource(getattr(auth, flow)))

        unbound = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler) and not node.name
        ]
        assert not unbound, (
            f"except clause binds no name, so the rule above is blind to it: {unbound}"
        )

        # Exact callables for sys, whose exit() this function uses legitimately;
        # whole modules for traceback and inspect, which it has no other use for.
        banned_calls = {"sys.exc_info", "sys.exception", "exc_info", "locals", "vars"}
        banned_modules = {"traceback", "inspect"}
        reached = sorted(
            {
                name
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                for name in [ast.unparse(node.func)]
                if name in banned_calls or name.split(".")[0] in banned_modules
            }
        )
        assert not reached, f"reaches the live exception without binding it: {reached}"


class _RecordingSocket:
    """Stands in for the real socket so server_bind's Windows branch can run."""

    def __init__(self):
        self.calls = []

    def setsockopt(self, level, option, value):
        self.calls.append(("setsockopt", level, option, value))

    def bind(self, address):
        self.calls.append(("bind", address))

    def getsockname(self):
        return ("127.0.0.1", 8081)


class TestTheCallbackPortIsNotShared:
    """The listener carries the authorisation code, so it must not be displaceable.

    `server_bind` chooses on `sys.platform` at call time, so the Windows branch
    is reachable from a POSIX runner against a recording socket. That is worth
    more than reading the source for it: source assertions here let the option
    be set after the bind, at the wrong level, on the wrong socket, or with a
    value of 0, all of which pass a check that only looks for the name.
    """

    def _bind_as(self, platform, monkeypatch):
        monkeypatch.setattr(auth.sys, "platform", platform)
        # Absent on POSIX, so it has to be created rather than replaced.
        monkeypatch.setattr(auth.socket, "SO_EXCLUSIVEADDRUSE", 0xFFFFFFFF, raising=False)
        monkeypatch.setattr(auth.socket, "getfqdn", lambda host: host)

        server = auth._CallbackServer.__new__(auth._CallbackServer)
        server.socket = _RecordingSocket()
        server.server_address = ("localhost", 8081)
        # The class attribute is fixed at import against the real platform, so
        # the value the other test pins is supplied here rather than read.
        server.allow_reuse_address = platform != "win32"
        server.allow_reuse_port = False
        auth._CallbackServer.server_bind(server)
        return server.socket.calls

    def test_windows_asks_for_exclusive_use_before_binding(self, monkeypatch):
        calls = self._bind_as("win32", monkeypatch)
        assert calls[0] == (
            "setsockopt",
            socket.SOL_SOCKET,
            auth.socket.SO_EXCLUSIVEADDRUSE,
            1,
        ), calls
        assert ("bind", ("localhost", 8081)) in calls
        # Never both: asking to share after asking not to is the configuration
        # Microsoft documents as insecure.
        assert not any(
            call[:3] == ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR) for call in calls
        ), calls

    def test_posix_asks_for_nothing_exclusive(self, monkeypatch):
        calls = self._bind_as("linux", monkeypatch)
        assert not any(
            call[0] == "setsockopt" and call[2] == auth.socket.SO_EXCLUSIVEADDRUSE for call in calls
        ), calls

    def test_reuse_is_allowed_off_windows_and_refused_on_it(self):
        """Not asking to share is the fix; the exclusive option is the belt.

        The runtime value alone cannot see a revert to a bare `True`, because
        that is what this platform is supposed to have, so the source form is
        pinned with it.
        """
        import ast
        import inspect

        assert auth._CallbackServer.allow_reuse_address == (sys.platform != "win32")

        tree = ast.parse(inspect.getsource(auth._CallbackServer))
        assigned = [
            ast.unparse(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "allow_reuse_address" for t in node.targets)
        ]
        assert assigned, "allow_reuse_address is no longer set here"
        assert all("platform" in value for value in assigned), assigned

    def test_only_win32_is_named(self):
        """`sys.platform` is `win32` on 64-bit Windows too, so a `win64` test
        is a branch that never runs."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth._CallbackServer))
        compared = {
            const.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Compare) and ast.unparse(node.left) == "sys.platform"
            for const in node.comparators
            if isinstance(const, ast.Constant)
        }
        assert compared == {"win32"}, compared

    @pytest.mark.skipif(sys.platform == "win32", reason="binds a real POSIX socket")
    def test_it_still_binds(self):
        """server_bind is overridden, so a mistake there breaks `auth` outright."""
        server = auth._CallbackServer(("localhost", 0), auth.BaseHTTPRequestHandler)
        try:
            assert server.server_address[0] == "127.0.0.1"
            assert server.server_address[1] != 0
        finally:
            server.server_close()

    def test_no_bare_httpserver_is_constructed_anywhere(self):
        """Scoped to the module, not to the setup flow: a second listener added
        elsewhere would carry the default this class exists to refuse."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(auth))
        bare = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "HTTPServer"
        ]
        assert not bare, bare
