"""What a query tool does before it reads the cache.

`live=True` means "sync this window first, then read", rather than fetching
and parsing a second time in a per-tool function of its own. One fetch layer
is the same data by a shorter route, and the cache keeps what it fetched
instead of discarding it.

The property that has to survive is what `live` is FOR. Auto-sync swallows
failures on purpose, because a stale answer beats no answer on an ordinary
query. A user who asked for live did so to escape the cache, so handing them
the cache anyway - silently - is the one outcome that makes the flag a lie.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp.tools import sync_tools


@pytest.fixture
def calls(monkeypatch):
    seen = {"auto": [], "sync": []}
    monkeypatch.setattr(sync_tools, "auto_sync_if_stale", lambda t: seen["auto"].append(t))
    return seen


def _run_sync_returning(status, seen):
    def _fake(data_types, days=30, since=None, until=None, handlers=None):
        seen["sync"].append((tuple(data_types), since, until))
        return {data_types[0]: {"status": status, "records": 3}}

    return _fake


class TestAnOrdinaryQuery:
    def test_it_defers_to_auto_sync(self, calls, monkeypatch):
        monkeypatch.setattr(sync_tools, "run_sync", _run_sync_returning("ok", calls))
        sync_tools.refresh_before_query("spo2", date(2026, 3, 1), date(2026, 3, 5), live=False)
        assert calls["auto"] == ["spo2"]
        assert calls["sync"] == [], "an ordinary query must not force a window sync"


class TestALiveQuery:
    def test_it_syncs_exactly_the_window_asked_for(self, calls, monkeypatch):
        monkeypatch.setattr(sync_tools, "run_sync", _run_sync_returning("ok", calls))
        sync_tools.refresh_before_query("spo2", date(2026, 3, 1), date(2026, 3, 5), live=True)
        assert calls["sync"] == [(("spo2",), "2026-03-01", "2026-03-05")]
        assert calls["auto"] == [], "live must not fall back to the once-a-day gate"

    @pytest.mark.parametrize("status", ["error", "auth_error", "rate_limited"])
    def test_a_failed_refresh_is_raised_rather_than_served_from_cache(
        self, calls, monkeypatch, status
    ):
        """run_sync reports failure in its return value, not by raising.

        So the natural implementation - call it and move on to the query -
        answers from the cache while telling the user it went live.
        """
        monkeypatch.setattr(sync_tools, "run_sync", _run_sync_returning(status, calls))
        with pytest.raises(RuntimeError) as caught:
            sync_tools.refresh_before_query("spo2", date(2026, 3, 1), date(2026, 3, 5), live=True)
        assert status in str(caught.value)
        assert "spo2" in str(caught.value)

    def test_a_type_missing_from_the_result_is_a_failure(self, calls, monkeypatch):
        """An empty result reads as success to anything checking for a bad status."""
        monkeypatch.setattr(sync_tools, "run_sync", lambda *a, **k: {})
        with pytest.raises(RuntimeError):
            sync_tools.refresh_before_query("spo2", date(2026, 3, 1), date(2026, 3, 5), live=True)

    def test_the_raised_message_carries_no_response_content(self, calls, monkeypatch):
        """These paths carry API responses; notes could hold a measurement."""

        def _fake(data_types, days=30, since=None, until=None, handlers=None):
            return {"spo2": {"status": "error", "notes": "avg SpO2 94 rejected"}}

        monkeypatch.setattr(sync_tools, "run_sync", _fake)
        with pytest.raises(RuntimeError) as caught:
            sync_tools.refresh_before_query("spo2", date(2026, 3, 1), date(2026, 3, 5), live=True)
        assert str(caught.value) == "live refresh of spo2 failed: error"


class TestOfflineMode:
    def test_live_is_refused_rather_than_quietly_downgraded(self, calls, monkeypatch):
        """And refused as HealthOfflineError specifically.

        require_auth catches that type and turns it into a tagged message; any
        other exception escapes to the client as a traceback, which is both a
        worse answer and a leak risk.
        """
        monkeypatch.setattr(sync_tools, "run_sync", _run_sync_returning("ok", calls))
        with patch.object(sync_tools.config, "OFFLINE_MODE", True):
            with pytest.raises(sync_tools.api.HealthOfflineError):
                sync_tools.refresh_before_query(
                    "spo2", date(2026, 3, 1), date(2026, 3, 5), live=True
                )
        assert calls["sync"] == []
