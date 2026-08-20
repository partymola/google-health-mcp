"""The client's exception vocabulary, and the one header it parses.

The request paths themselves are covered in `test_google_api.py`. What is here
is what the rest of the package depends on rather than what the client does:
which exception each failure becomes, and that a value read out of a response
header can neither be unbounded nor unparseable.
"""

import json
from datetime import date
from unittest.mock import MagicMock

import pytest

from google_health_mcp import api, config, db
from google_health_mcp.api import (
    HealthAPIError,
    HealthAuthError,
    HealthOfflineError,
    HealthRateLimitError,
)


class TestAPIExceptions:
    """Test exception hierarchy."""

    def test_auth_error(self):
        e = HealthAuthError("test")
        assert str(e) == "test"

    def test_rate_limit_error_default(self):
        """The default is the cap, not a value the cap would reject."""
        from google_health_mcp.api import MAX_RATE_LIMIT_WAIT

        e = HealthRateLimitError()
        assert e.reset_seconds == MAX_RATE_LIMIT_WAIT

    def test_rate_limit_error_custom(self):
        e = HealthRateLimitError(120)
        assert e.reset_seconds == 120
        assert "120" in str(e)

    def test_api_error(self):
        e = HealthAPIError("bad request")
        assert "bad request" in str(e)

    def test_offline_error_not_a_caught_api_error(self):
        # run_sync() catches HealthAPIError/Auth/RateLimit per data type. If
        # HealthOfflineError subclassed any of them it would be swallowed into
        # per-type error rows instead of propagating to require_auth/CLI.
        assert not issubclass(
            HealthOfflineError,
            (HealthAPIError, HealthAuthError, HealthRateLimitError),
        )


class TestTheRateLimitWait:
    """The value a 429 hands us is bounded before anything can act on it.

    Nothing sleeps on it today, which is exactly why the bound belongs where
    the header is read rather than where a wait would happen: the next caller
    to add one inherits it instead of having to know.
    """

    def _reset_for(self, header):
        import urllib.error

        from google_health_mcp.api import _reset_seconds

        return _reset_seconds(
            urllib.error.HTTPError("url", 429, "TMR", {"Retry-After": header}, None)
        )

    def test_a_fractional_header_keeps_its_value(self):
        assert self._reset_for("60.5") == 60

    def test_a_whole_number_is_unchanged(self):
        assert self._reset_for("600") == 600

    def test_an_enormous_header_is_capped(self):
        from google_health_mcp.api import MAX_RATE_LIMIT_WAIT

        assert self._reset_for("86400") == MAX_RATE_LIMIT_WAIT

    def test_an_unparseable_header_falls_back_within_the_cap(self):
        from google_health_mcp.api import MAX_RATE_LIMIT_WAIT

        assert self._reset_for("soon") == MAX_RATE_LIMIT_WAIT

    def test_an_http_date_falls_back_rather_than_raising(self):
        """The header's other documented form, which this does not read."""
        from google_health_mcp.api import MAX_RATE_LIMIT_WAIT

        assert self._reset_for("Wed, 21 Oct 2026 07:28:00 GMT") == MAX_RATE_LIMIT_WAIT

    @pytest.mark.parametrize("header", ["inf", "Infinity", "-inf", "1e400", "nan"])
    def test_a_non_finite_header_falls_back_rather_than_raising(self, header):
        """int(float("inf")) raises OverflowError, where int("inf") raised ValueError.

        Accepting floats to keep a fractional header widened the input past
        what the handler caught.
        """
        from google_health_mcp.api import MAX_RATE_LIMIT_WAIT

        assert self._reset_for(header) == MAX_RATE_LIMIT_WAIT

    def test_a_negative_header_does_not_become_a_negative_sleep(self):
        assert self._reset_for("-5") == 0

    def test_absent_headers_do_not_raise(self):
        import urllib.error

        from google_health_mcp.api import MAX_RATE_LIMIT_WAIT, _reset_seconds

        assert _reset_seconds(urllib.error.HTTPError("url", 429, "TMR", None, None)) == (
            MAX_RATE_LIMIT_WAIT
        )


class TestTheArrayEndpointsSurviveTheRealClient:
    """Exercised through the real client, not a patched one.

    Everywhere else the client's own functions are patched, so the shape it
    hands back is agreed between two mocks rather than measured. These two
    drive a reader from the response body up.
    """

    def _urlopen_returning(self, payload, monkeypatch):
        monkeypatch.setattr(config, "OFFLINE_MODE", False)
        monkeypatch.setattr(api, "refresh_google_token", MagicMock(return_value="token"))
        response = MagicMock()
        response.read.return_value = payload
        response.__enter__ = lambda s: s
        response.__exit__ = lambda *a: None
        monkeypatch.setattr(api.urllib.request, "urlopen", MagicMock(return_value=response))

    def test_devices_are_read_from_what_the_endpoint_returns(self, monkeypatch):
        from google_health_mcp.tools import devices_tools

        self._urlopen_returning(
            json.dumps(
                {
                    "pairedDevices": [
                        {
                            "name": "users/me/pairedDevices/1",
                            "deviceVersion": "Pixel Watch 3",
                            "batteryLevel": 80,
                        }
                    ]
                }
            ).encode(),
            monkeypatch,
        )
        devices = devices_tools._fetch_devices()
        assert len(devices) == 1
        assert devices[0]["battery_level"] == 80

    def test_a_normaliser_reads_what_the_real_client_returns(self, monkeypatch):
        from google_health_mcp.tools import google_sync

        self._urlopen_returning(
            json.dumps(
                {
                    "dataPoints": [
                        {
                            "dailyOxygenSaturation": {
                                "date": {"year": 2026, "month": 3, "day": 10},
                                "averagePercentage": 96.5,
                            }
                        }
                    ]
                }
            ).encode(),
            monkeypatch,
        )

        conn = db.get_db(":memory:")
        try:
            assert google_sync.sync_spo2(conn, date(2026, 3, 10), date(2026, 3, 10)) == 1
            assert db.query_spo2(conn, "2026-03-10", "2026-03-10")[0]["avg"] == 96.5
        finally:
            conn.close()
