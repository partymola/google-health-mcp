"""Daily heart-rate zone boundaries, as Google reports them.

A different quantity from `heart_rate.zones`, which holds an import's zones
with the minutes spent in each: these are the four boundaries alone. Read
from Google's merged stream: on the days Google holds two zone records, a
per-source list answers every date in the window twice, and which copy is
the right one varies by date.
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync

# Fictional throughout.
_ZONES = [
    {"heartRateZoneType": "LIGHT", "minBeatsPerMinute": "90", "maxBeatsPerMinute": "110"},
    {"heartRateZoneType": "MODERATE", "minBeatsPerMinute": "110", "maxBeatsPerMinute": "130"},
    {"heartRateZoneType": "VIGOROUS", "minBeatsPerMinute": "130", "maxBeatsPerMinute": "150"},
    {"heartRateZoneType": "PEAK", "minBeatsPerMinute": "150", "maxBeatsPerMinute": "200"},
]


def _point(day=15, zones=_ZONES):
    """The merged stream's shape: no `dataSource`."""
    return {
        "dailyHeartRateZones": {
            "date": {"year": 2026, "month": 3, "day": day},
            "heartRateZones": zones,
        }
    }


def _sync(tmp_db, points):
    seen = []

    def answer(data_type, start, end, view="list"):
        seen.append((data_type, view))
        return points

    with patch.object(google_sync.api, "list_google_data_points", side_effect=answer):
        count = google_sync.sync_heart_rate_zones(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
    tmp_db.commit()
    return count, seen


class TestTheBoundariesAreStoredAsSent:
    def test_a_day_is_its_zones_read_from_the_merged_stream(self, tmp_db):
        count, seen = _sync(tmp_db, [_point()])
        assert count == 1
        assert seen == [("daily-heart-rate-zones", "reconcile")]
        (row,) = db.query_heart_rate_zones(tmp_db, "2026-03-15", "2026-03-15")
        assert row["zone_boundaries"] == _ZONES
        assert row["provider"] == "google"

    def test_it_asks_for_exactly_the_range_given(self, tmp_db):
        """A day's zones are stored for whatever date they carry, so a wider
        request would write days outside a `since`/`until` repair."""
        asked = []

        def answer(data_type, start, end, view="list"):
            asked.append((start, end))
            return []

        with patch.object(google_sync.api, "list_google_data_points", side_effect=answer):
            google_sync.sync_heart_rate_zones(tmp_db, date(2026, 3, 10), date(2026, 3, 12))
        assert asked == [(date(2026, 3, 10), date(2026, 3, 13))]

    @pytest.mark.parametrize("zones", [[], None, "LIGHT", {"type": "LIGHT"}])
    def test_a_day_with_no_list_of_zones_writes_no_row(self, tmp_db, zones):
        count, _ = _sync(tmp_db, [_point(zones=zones)])
        assert count == 0
        assert db.query_heart_rate_zones(tmp_db, "2026-03-01", "2026-04-01") == []

    def test_a_date_answered_twice_keeps_the_later_point(self, tmp_db):
        """What resting heart rate does with the same shape, stated rather than left."""
        later = [
            dict(z, maxBeatsPerMinute="111") if z["heartRateZoneType"] == "LIGHT" else z
            for z in _ZONES
        ]
        _sync(tmp_db, [_point(), _point(zones=later)])
        (row,) = db.query_heart_rate_zones(tmp_db, "2026-03-15", "2026-03-15")
        assert row["zone_boundaries"] == later

    def test_a_point_that_is_not_an_object_or_has_no_date_is_skipped(self, tmp_db):
        broken = [{"dailyHeartRateZones": "x"}, {"dailyHeartRateZones": {"heartRateZones": _ZONES}}]
        count, _ = _sync(tmp_db, broken)
        assert count == 0

    def test_the_imported_zones_are_left_alone(self, tmp_db):
        """The import's zones carry minutes and calories these do not."""
        imported = [{"name": "Fat Burn", "minutes": 30, "caloriesOut": 200, "min": 100, "max": 140}]
        db.save_heart_rate(tmp_db, "2026-03-15", 55, imported)
        tmp_db.commit()
        _sync(tmp_db, [_point()])
        (row,) = db.query_heart_rate(tmp_db, "2026-03-15", "2026-03-15")
        assert row["zones"] == imported

    def test_an_unreadable_stored_list_reads_as_absent(self, tmp_db):
        db.save_heart_rate_zones(
            tmp_db, {"date": "2026-03-15", "zone_boundaries": "{not json", "provider": "google"}
        )
        tmp_db.commit()
        (row,) = db.query_heart_rate_zones(tmp_db, "2026-03-15", "2026-03-15")
        assert row["zone_boundaries"] is None


async def _call(db_path, **kwargs):
    from google_health_mcp.tools.heart_tools import health_get_heart_rate_zones

    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch("google_health_mcp.tools.heart_tools.refresh_before_query") as refresh,
        patch.object(db, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        body = json.loads(await health_get_heart_rate_zones(start_date="2026-03-15", **kwargs))
    return body, [c.args[0] for c in refresh.call_args_list]


class TestTheTool:
    async def test_it_returns_the_days_and_refreshes_its_own_type(self, tmp_db):
        _sync(tmp_db, [_point()])
        body, refreshed = await _call(Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]))
        assert body["heart_rate_zones"][0]["zone_boundaries"] == _ZONES
        assert body["count"] == 1
        assert refreshed == ["heart_rate_zones"]

    async def test_the_days_come_back_in_date_order(self, tmp_db):
        _sync(tmp_db, [_point(day=17), _point(day=15), _point(day=16)])
        body, _ = await _call(Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]))
        assert [e["date"] for e in body["heart_rate_zones"]] == [
            "2026-03-15",
            "2026-03-16",
            "2026-03-17",
        ]

    async def test_the_refresh_covers_the_default_window_and_carries_live(self, tmp_path):
        from google_health_mcp.tools.heart_tools import health_get_heart_rate_zones

        with (
            patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
            patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
            patch("google_health_mcp.tools.heart_tools.refresh_before_query") as refresh,
            patch.object(db, "DB_PATH", tmp_path / "empty.db"),
        ):
            client.exists.return_value = True
            tokens.exists.return_value = True
            await health_get_heart_rate_zones(end_date="2026-03-31", live=True)
        refresh.assert_called_once_with(
            "heart_rate_zones", date(2026, 3, 1), date(2026, 3, 31), True
        )

    async def test_an_empty_window_says_so(self, tmp_path):
        body, _ = await _call(tmp_path / "empty.db")
        assert body["message"] == "No heart rate zones found for this period."
