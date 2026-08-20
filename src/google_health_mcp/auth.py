"""Google OAuth2 setup and token management.

The client is a Google Desktop app, so its file carries a client secret that
is not confidential in the OAuth sense, and the flow uses PKCE regardless.
Access tokens last an hour; refresh tokens do not rotate, which is why a token
minted on a machine with a browser can be copied to a headless one.
"""

import base64
import hashlib
import html
import json
import logging
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from . import config

logger = logging.getLogger(__name__)


# RFC 6749 defines the token endpoint's refusals as 400, with 401 for a bad
# client. 403 is deliberately absent: a WAF or bot-protection block returns it
# with no opinion about the grant, and this client already reads 403 on a data
# request as something else entirely.
_REFUSAL_CODES = frozenset({400, 401})


class _CallbackServer(HTTPServer):
    """Refuse to share the port the authorisation code arrives on.

    Deliberate, and not what `HTTPServer` does by default: it asks for address
    reuse, which on Windows is what lets another process bind over this
    listener and take the code. Not asking is the fix; the exclusive-use
    option is asked for as well. Why each half is there, and what it costs, is
    in AGENTS.md. Pinned by TestTheCallbackPortIsNotShared, which drives this
    method's Windows branch on a POSIX runner.
    """

    allow_reuse_address = sys.platform != "win32"
    # SO_REUSEPORT is the POSIX-side version of the same hazard; nothing sets
    # this, and nothing should.
    allow_reuse_port = False

    def server_bind(self):
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class TokenRefused(RuntimeError):
    """The server judged the credentials and rejected them.

    The only failure that warrants telling the user to re-authorise, which
    rewrites the token file the syncing host owns.
    """


class RefreshNetworkError(RuntimeError):
    """The refresh request never got an answer.

    Subclasses RuntimeError so existing callers are unaffected, but is
    distinguishable: an unreachable server says nothing about whether the
    credentials are still good, and telling the user to re-authorise would
    rotate a token file another host may own.
    """


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        # Best-effort: O_TRUNC has already emptied the file, so a
        # permissions failure must not take the token with it.
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                logger.warning("Could not tighten permissions on the token file")
        os.write(fd, json.dumps(data, indent=2).encode())
    finally:
        os.close(fd)


def _load_json(path):
    """Read a credential file as a dict, or say why the credentials are unusable.

    Classified here rather than left to the caller: a file that is absent,
    unreadable or not a JSON object means there are no usable credentials,
    which is a refusal - unlike a transport failure, it will not clear on its
    own and the user does have to re-authorise.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise TokenRefused(
            f"{path.name} is missing or unreadable. Run: google-health-mcp auth"
        ) from e
    if not isinstance(data, dict):
        raise TokenRefused(f"{path.name} is malformed. Run: google-health-mcp auth")
    return data


def _generate_pkce():
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


#: Its own cache and lock, kept per-provider rather than global so a second
#: one can be added without the two evicting each other mid-sync.
_cached_google_tokens = None
_cached_google_client = None
_google_token_lock = threading.Lock()


def _load_google_client() -> dict:
    """Return the client credentials from Google's downloaded Desktop-client file.

    Read in Google's own shape - the `installed` wrapper - so the file can be
    dropped in unedited. Anything else means there are no usable credentials.

    Absence gets its own message rather than `_load_json`'s, which tells the
    reader to run `auth` - true of a missing token and circular here, since
    this is what `auth` reads first.
    """
    if not config.GOOGLE_CLIENT_PATH.exists():
        raise TokenRefused(
            f"No client file at {config.GOOGLE_CLIENT_PATH}. Create an OAuth client of "
            "type Desktop app in the Google Cloud console, download its JSON, and save "
            "it there unedited."
        )
    data = _load_json(config.GOOGLE_CLIENT_PATH)
    installed = data.get("installed")
    if not isinstance(installed, dict) or not installed.get("client_id"):
        # A Web client file is refused rather than accepted: its secret is
        # genuinely confidential where a Desktop client's is not, and its
        # redirect URI has to be registered, so it fails at consent with
        # redirect_uri_mismatch instead of here where the cause is legible.
        raise TokenRefused(
            f"{config.GOOGLE_CLIENT_PATH.name} is not a Google OAuth Desktop client file. "
            "Create an OAuth client of type Desktop app in the Google Cloud console "
            "and save its downloaded JSON there unedited."
        )
    return installed


def _google_auth_url(challenge: str, client_id: str) -> str:
    """Build the consent URL.

    `access_type=offline` is what makes Google issue a refresh token at all,
    and `prompt=consent` is what makes it issue one again to a user who has
    consented before. Without the second, re-authorising returns an access
    token and no refresh token, unattended sync cannot work, and the flow
    looks like it succeeded.
    """
    return (
        config.GOOGLE_AUTH_URL
        + "?"
        + urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": config.GOOGLE_REDIRECT_URI,
                "scope": config.GOOGLE_SCOPES,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "access_type": "offline",
                "prompt": "consent",
            }
        )
    )


def _google_expires_in(payload: dict) -> float:
    """Seconds until the access token expires, defaulting when unusable.

    A string or null here would otherwise raise a TypeError out of the
    arithmetic, in the setup flow's main thread where nothing catches it.
    """
    value = payload.get("expires_in", config.GOOGLE_TOKEN_LIFETIME)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return config.GOOGLE_TOKEN_LIFETIME
    return value


def _google_token_store(payload: dict, previous: dict) -> dict:
    """Shape a token response for storage.

    A refresh-token expiry is kept because it is the reading that has not
    lied here about an app still being in Testing - the console's Audience
    page reports "In production" while the token server disagrees.

    Stored as an instant rather than as the duration Google sends, and
    computed once at consent. Whether a refresh response repeats the field
    has only been measured against a published app, so nobody knows whether
    a repeat counts down or restates the grant - and recomputing on each
    refresh would push a restated constant an hour further out every hour,
    so the expiry would never arrive. Carried forward, it means what it says
    whichever the truth is, and a consent (`previous` empty) replaces it.
    """
    store = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or previous.get("refresh_token", ""),
        "expires_at": time.time() + _google_expires_in(payload),
    }
    granted = payload.get("refresh_token_expires_in")
    if previous.get("refresh_token_expires_at") is not None:
        store["refresh_token_expires_at"] = previous["refresh_token_expires_at"]
    elif isinstance(granted, (int, float)) and not isinstance(granted, bool):
        store["refresh_token_expires_at"] = time.time() + granted
    return store


def _classify_google_refusal(error: urllib.error.HTTPError) -> RuntimeError:
    """Turn a refused token request into the failure the user can act on.

    `invalid_grant` a week after authorising means the app is still in
    Testing whatever the console shows, and that is the one failure worth
    naming a fix for. The body is read to tell that apart from a bad client,
    but nothing from it reaches the message.
    """
    if error.code not in _REFUSAL_CODES:
        return RefreshNetworkError("Google could not answer the refresh request.")
    reason = ""
    try:
        body = json.loads(error.read().decode())
        if isinstance(body, dict):
            reason = body.get("error", "")
    except Exception:
        # An unreadable body still leaves a refusal - only the advice is lost.
        pass
    if reason == "invalid_grant":
        return TokenRefused(
            "Google rejected the refresh token. A refresh token issued while the "
            "OAuth app is in Testing expires after 7 days: open the Google Cloud "
            "console, use the Publish app button on the Audience page, then run: "
            "google-health-mcp auth"
        )
    return TokenRefused("Google refused the credentials. Run: google-health-mcp auth")


def _refresh_google_token() -> str:
    global _cached_google_tokens, _cached_google_client

    with _google_token_lock:
        if _cached_google_tokens is None:
            _cached_google_tokens = _load_json(config.GOOGLE_TOKENS_PATH)
        if _cached_google_client is None:
            _cached_google_client = _load_google_client()

        expires_at = _cached_google_tokens.get("expires_at", 0)
        if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
            expires_at = 0
        if time.time() < expires_at - 300:
            return _cached_google_tokens["access_token"]

        if not _cached_google_tokens.get("refresh_token"):
            raise TokenRefused("No Google refresh token. Run: google-health-mcp auth")

        data = urlencode(
            {
                "grant_type": "refresh_token",
                "client_id": _cached_google_client["client_id"],
                "client_secret": _cached_google_client.get("client_secret", ""),
                "refresh_token": _cached_google_tokens["refresh_token"],
            }
        ).encode()
        req = urllib.request.Request(
            config.GOOGLE_TOKEN_URL,
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as e:
            raise _classify_google_refusal(e) from e
        except OSError as e:
            raise RefreshNetworkError("Could not reach Google to refresh the token.") from e

        try:
            payload = json.loads(raw)
        except ValueError as e:
            raise RefreshNetworkError("Google returned an unreadable response.") from e

        if not isinstance(payload, dict) or not payload.get("access_token"):
            raise TokenRefused("Google returned no access token. Run: google-health-mcp auth")

        _cached_google_tokens = _google_token_store(payload, _cached_google_tokens)
        _save_json(config.GOOGLE_TOKENS_PATH, _cached_google_tokens)
        logger.info("Google token refreshed successfully")
        return _cached_google_tokens["access_token"]


def refresh_google_token() -> str:
    """Return a valid Google access token, refreshing if expired.

    Raises exactly two types, because `google_get` and doctor's grading both
    branch on which one it is: a credential the server refused, or anything
    else. Never replace the catch-all with a list of exception types - an
    unanticipated failure must land in the second by construction.
    """
    try:
        return _refresh_google_token()
    except (TokenRefused, RefreshNetworkError):
        raise
    except Exception as e:
        logger.error("Google token refresh failed: %s", type(e).__name__)
        raise RefreshNetworkError("Could not obtain a token from Google.") from e


def invalidate_google_token_cache():
    """Clear the in-memory Google token cache."""
    global _cached_google_tokens
    with _google_token_lock:
        _cached_google_tokens = None


def setup_google_auth():
    """Interactive Google consent. Opens a browser, exchanges the code, stores tokens.

    No client id is prompted for: Google issues a JSON file, and asking the
    user to retype fields out of it only invites transcription errors.
    """
    client = _load_google_client()
    verifier, challenge = _generate_pkce()
    auth_result = {"tokens": None, "error": None}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = parse_qs(urlparse(self.path).query)

            code = qs.get("code", [None])[0]
            if not code:
                error = qs.get("error", ["unknown"])[0]
                self._respond(400, f"Error: {error}")
                auth_result["error"] = error
                return

            data = urlencode(
                {
                    "client_id": client["client_id"],
                    "client_secret": client.get("client_secret", ""),
                    "grant_type": "authorization_code",
                    "code": code,
                    "code_verifier": verifier,
                    "redirect_uri": config.GOOGLE_REDIRECT_URI,
                }
            ).encode()
            req = urllib.request.Request(
                config.GOOGLE_TOKEN_URL,
                data=data,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            try:
                with urllib.request.urlopen(req, timeout=15) as r:
                    tokens = json.loads(r.read().decode())
                self._respond(200, "Authorised! You can close this tab.")
                auth_result["tokens"] = tokens
            except Exception as e:
                logger.error("Google token exchange failed: %s", type(e).__name__)
                self._respond(500, f"Token exchange failed ({type(e).__name__}).")
                auth_result["error"] = type(e).__name__

        def _respond(self, status_code, message):
            self.send_response(status_code)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(_callback_page(message).encode())

        def log_message(self, format, *args):
            # The default logs the full request line, and the callback's query
            # string carries the authorisation code.
            pass

    auth_url = _google_auth_url(challenge, client["client_id"])

    # Bind before opening the browser: the listener no longer asks to share the
    # port, so a busy one is now a real failure, and sending the user to an
    # authorisation page whose redirect has nowhere to land wastes the attempt.
    try:
        server = _CallbackServer(("localhost", config.GOOGLE_CALLBACK_PORT), CallbackHandler)
    except OSError as e:
        print(
            f"Port {config.GOOGLE_CALLBACK_PORT} could not be bound ({type(e).__name__}), so "
            "the OAuth callback cannot be received. A Desktop client registers no redirect "
            "URI, so nothing at Google holds this number - it is fixed by this package and "
            "the flow cannot use another. On Windows a socket from a recent "
            "`google-health-mcp auth` may still be closing; retry once it has.",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nOpening browser for Google authorisation...")
    print(f"If it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    thread.join(timeout=120)
    server.server_close()

    if auth_result["error"]:
        print(f"Authorisation failed: {auth_result['error']}", file=sys.stderr)
        sys.exit(1)
    if not auth_result["tokens"]:
        print("No response received. Timed out or denied.", file=sys.stderr)
        sys.exit(1)

    raw = auth_result["tokens"]
    if not isinstance(raw, dict) or not raw.get("access_token"):
        # Same guard the refresh path carries: a proxy answering 200 with
        # HTML, or a bare scalar body, otherwise escapes to the CLI as a
        # traceback from the main thread rather than being reported.
        print("Google returned no access token.", file=sys.stderr)
        sys.exit(1)
    if not raw.get("refresh_token"):
        print(
            "Google returned no refresh token, so unattended sync will not work. "
            "Revoke this app at myaccount.google.com/permissions and run auth again.",
            file=sys.stderr,
        )
        sys.exit(1)

    _save_json(config.GOOGLE_TOKENS_PATH, _google_token_store(raw, {}))
    print("Tokens saved.")
    print("\nSetup complete. Register with Claude Code:")
    exe = shutil.which("google-health-mcp") or "google-health-mcp"
    print(f"  claude mcp add -s user google-health -- {exe}")
    if "refresh_token_expires_in" in raw:
        # Worth stopping the user here: it works today and breaks in a week,
        # by which time nothing connects the failure to this moment.
        print(
            "\nWARNING: this refresh token expires in about a week, which means the "
            "OAuth app is still in Testing whatever the console's Audience page says. "
            "Use the Publish app button, then run auth again."
        )


def _callback_page(message):
    """Build the page the OAuth callback returns.

    Escaped: message can carry a query-string parameter.
    """
    return f"<html><body><h2>{html.escape(message)}</h2></body></html>"
