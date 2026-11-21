"""Breathing rate by sleep stage: the summaries Google keeps for each night.

Every point carries a `name`, but its data-point id is empty, so all of them
share one name and a key on it would hold one row for the whole history. A
night can have several, so a day's summaries are kept together, as sent.
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync

# Fictional throughout. The shared, empty-id name is the shape measured.
_NAME = "users/me/dataTypes/respiratory-rate-sleep-summary/dataPoints/"
_STATS = {"breathsPerMinute": 15.5, "signalToNoise": 9.5, "standardDeviation": 0.5}


def _summary(physical, civil_day, hours=6, source="watch"):
    return {
        "name": _NAME,
        "dataSource": {"device": {"displayName": f"Fictional {source}"}, "platform": "FITBIT"},
        "respiratoryRateSleepSummary": {
            "fullSleepStats": _STATS,
            "deepSleepStats": _STATS,
            "sampleTime": {
                "physicalTime": physical,
                "utcOffset": "3600s",
                "civilTime": {
                    "date": {"year": 2026, "month": 3, "day": civil_day},
                    "time": {"hours": hours},
                },
            },
        },
    }


def _sync(tmp_db, points):
    seen = []

    def answer(data_type, start, end, view="list"):
        seen.append((data_type, view))
        return points

    with patch.object(google_sync.api, "list_google_data_points", side_effect=answer):
        count = google_sync.sync_respiratory_rate_sleep(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
    tmp_db.commit()
    return count, seen


def _days(tmp_db):
    return db.query_day_lists(tmp_db, "respiratory_rate_sleep", "2026-03-01", "2026-04-01")


class TestANightsSummariesAreKeptTogether:
    def test_summaries_sharing_one_name_are_all_kept(self, tmp_db):
        points = [
            _summary("2026-03-15T03:00:00Z", 15, hours=4),
            _summary("2026-03-15T05:30:00Z", 15, hours=6),
        ]
        count, seen = _sync(tmp_db, points)
        assert count == 1
        assert seen == [("respiratory-rate-sleep-summary", "list")]
        (row,) = _days(tmp_db)
        assert row["points"] == points

    def test_a_summary_is_dated_by_its_civil_time(self, tmp_db):
        """23:30 UTC on the 14th is 00:30 on the 15th an hour east."""
        _sync(tmp_db, [_summary("2026-03-14T23:30:00Z", 15, hours=0)])
        assert [r["date"] for r in _days(tmp_db)] == ["2026-03-15"]

    def test_a_resync_replaces_the_night(self, tmp_db):
        _sync(tmp_db, [_summary("2026-03-15T03:00:00Z", 15), _summary("2026-03-15T05:00:00Z", 15)])
        _sync(tmp_db, [_summary("2026-03-15T05:00:00Z", 15)])
        (row,) = _days(tmp_db)
        assert len(row["points"]) == 1

    def test_only_the_range_asked_for_is_stored(self, tmp_db):
        asked = []

        def answer(t, s, e, view="list"):
            asked.append((s, e))
            return [_summary("2026-03-09T05:00:00Z", 9), _summary("2026-03-10T05:00:00Z", 10)]

        with patch.object(google_sync.api, "list_google_data_points", side_effect=answer):
            google_sync.sync_respiratory_rate_sleep(tmp_db, date(2026, 3, 10), date(2026, 3, 12))
        tmp_db.commit()
        assert asked == [(date(2026, 3, 9), date(2026, 3, 14))]
        assert [r["date"] for r in _days(tmp_db)] == ["2026-03-10"]

    @pytest.mark.parametrize(
        "broken",
        [{"respiratoryRateSleepSummary": "x"}, {"respiratoryRateSleepSummary": {}}],
        ids=["not-an-object", "no-sample-time"],
    )
    def test_a_summary_that_cannot_be_dated_is_skipped(self, tmp_db, broken):
        count, _ = _sync(tmp_db, [broken])
        assert count == 0
        assert _days(tmp_db) == []


async def _call(db_path, **kwargs):
    from google_health_mcp.tools.breathing_rate_tools import health_get_breathing_rate

    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch("google_health_mcp.tools.breathing_rate_tools.refresh_before_query") as refresh,
        patch.object(db, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        body = json.loads(await health_get_breathing_rate(start_date="2026-03-15", **kwargs))
    return body, [c.args[0] for c in refresh.call_args_list]


def _path(tmp_db):
    return Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])


class TestTheTool:
    async def test_the_summaries_stay_behind_their_flag(self, tmp_db):
        db.save_breathing_rate(tmp_db, {"date": "2026-03-15", "breaths_per_min": 15.0})
        tmp_db.commit()
        _sync(tmp_db, [_summary("2026-03-15T05:00:00Z", 15)])
        body, refreshed = await _call(_path(tmp_db))
        assert "sleep_summaries" not in body
        assert refreshed == ["breathing_rate"]

    async def test_asked_for_they_come_back_with_their_sample_time(self, tmp_db):
        db.save_breathing_rate(tmp_db, {"date": "2026-03-15", "breaths_per_min": 15.0})
        tmp_db.commit()
        point = _summary("2026-03-15T05:00:00Z", 15)
        _sync(tmp_db, [point])
        body, refreshed = await _call(_path(tmp_db), include_sleep_summaries=True)
        assert refreshed == [("breathing_rate", "respiratory_rate_sleep")]
        (summary,) = body["sleep_summaries"]
        assert summary["date"] == "2026-03-15"
        assert summary["sample_time"] == "2026-03-15T05:00:00Z"
        assert summary["utc_offset"] == "3600s"
        assert summary["fullSleepStats"] == _STATS
        assert summary["data_source"] == point["dataSource"]
        assert "sampleTime" not in summary

    async def test_the_summaries_are_counted_apart_from_the_nights(self, tmp_db):
        db.save_breathing_rate(tmp_db, {"date": "2026-03-15", "breaths_per_min": 15.0})
        tmp_db.commit()
        _sync(tmp_db, [_summary("2026-03-15T03:00:00Z", 15), _summary("2026-03-15T05:00:00Z", 15)])
        body, _ = await _call(_path(tmp_db), include_sleep_summaries=True)
        assert (body["count"], body["summary_count"]) == (1, 2)

    async def test_summaries_alone_are_still_an_answer(self, tmp_db):
        _sync(tmp_db, [_summary("2026-03-15T05:00:00Z", 15)])
        body, _ = await _call(_path(tmp_db), include_sleep_summaries=True)
        assert "message" not in body
        assert body["breathing_rate"] == []
        assert len(body["sleep_summaries"]) == 1

    async def test_without_the_flag_summaries_alone_are_no_answer(self, tmp_db):
        _sync(tmp_db, [_summary("2026-03-15T05:00:00Z", 15)])
        body, _ = await _call(_path(tmp_db))
        assert body["message"] == "No breathing rate data found for this period."

    async def test_with_the_flag_and_no_summaries_the_list_is_empty_not_missing(self, tmp_db):
        db.save_breathing_rate(tmp_db, {"date": "2026-03-15", "breaths_per_min": 15.0})
        tmp_db.commit()
        body, _ = await _call(_path(tmp_db), include_sleep_summaries=True)
        assert (body["sleep_summaries"], body["summary_count"]) == ([], 0)

    async def test_a_field_google_adds_cannot_overwrite_the_summarys_own(self, tmp_db):
        point = _summary("2026-03-15T05:00:00Z", 15)
        point["respiratoryRateSleepSummary"].update(
            {"date": "not ours", "sample_time": "not ours", "data_source": "not ours"}
        )
        _sync(tmp_db, [point])
        body, _ = await _call(_path(tmp_db), include_sleep_summaries=True)
        (summary,) = body["sleep_summaries"]
        assert (summary["date"], summary["sample_time"], summary["data_source"]) == (
            "2026-03-15",
            "2026-03-15T05:00:00Z",
            point["dataSource"],
        )

    async def test_one_unreadable_day_costs_only_that_day(self, tmp_db):
        db.save_respiratory_rate_sleep(tmp_db, {"date": "2026-03-16", "points": b"not zlib"})
        _sync(tmp_db, [_summary("2026-03-15T05:00:00Z", 15)])
        body, _ = await _call(_path(tmp_db), include_sleep_summaries=True)
        assert [s["date"] for s in body["sleep_summaries"]] == ["2026-03-15"]

    async def test_the_end_date_is_included(self, tmp_db):
        _sync(tmp_db, [_summary("2026-03-15T05:00:00Z", 15), _summary("2026-03-16T05:00:00Z", 16)])
        body, _ = await _call(_path(tmp_db), end_date="2026-03-15", include_sleep_summaries=True)
        assert [s["date"] for s in body["sleep_summaries"]] == ["2026-03-15"]

    async def test_both_types_refresh_together_over_the_window_and_carry_live(self, tmp_db):
        """Together, so a night's rate and its summaries come from one sync."""
        from google_health_mcp.tools.breathing_rate_tools import health_get_breathing_rate

        with (
            patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
            patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
            patch("google_health_mcp.tools.breathing_rate_tools.refresh_before_query") as refresh,
            patch.object(db, "DB_PATH", _path(tmp_db)),
        ):
            client.exists.return_value = True
            tokens.exists.return_value = True
            await health_get_breathing_rate(
                start_date="2026-03-10",
                end_date="2026-03-15",
                live=True,
                include_sleep_summaries=True,
            )
        assert [c.args for c in refresh.call_args_list] == [
            (
                ("breathing_rate", "respiratory_rate_sleep"),
                date(2026, 3, 10),
                date(2026, 3, 15),
                True,
            ),
        ]
