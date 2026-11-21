"""Each food and drink logged, kept a day at a time.

Entries are logged by hand, and deleting one is ordinary. A sync never
removes a row, so a table keyed by each entry's name would keep a deleted
entry for good. Storing the day's entries as one row, replaced on a re-sync,
keeps it in step with what Google sends for each day it answers.
"""

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from google_health_mcp import db, doctor
from google_health_mcp.tools import google_sync

# Fictional throughout.
_SOURCE = {"platform": "FITBIT", "recordingMethod": "MANUAL"}

_FOOD = {
    "foodDisplayName": "Plum",
    "mealType": "ANYTIME",
    "serving": {"amount": 1, "foodMeasurementUnitDisplayName": "medium"},
    "energy": {"kcal": 30},
    "energyFromFat": {"kcal": 1},
    "totalCarbohydrate": {"grams": 7.5},
    "totalFat": {"grams": 0.2},
    "nutrients": [{"nutrient": "SUGAR", "quantity": {"grams": 6.6}}],
    "food": "users/1/dataTypes/food/dataPoints/9",
}
_WATER = {"amountConsumed": {"milliliters": 250}}


def _civil(day, hours=12):
    return {"date": {"year": 2026, "month": 3, "day": day}, "time": {"hours": hours}}


def _entry(field, values, start, civil_day, name="1", hours=12, offset="0s"):
    """The measured shape: a name, and an interval ending seconds after it starts."""
    return {
        "name": f"users/1/dataTypes/x/dataPoints/{name}",
        "dataSource": _SOURCE,
        field: {
            "interval": {
                "startTime": start,
                "endTime": start.replace(":00Z", ":30Z"),
                "startUtcOffset": offset,
                "endUtcOffset": offset,
                "civilStartTime": _civil(civil_day, hours),
                "civilEndTime": _civil(civil_day, hours),
            },
            **values,
        },
    }


_APP = {"platform": "HEALTH_CONNECT", "recordingMethod": "MANUAL"}


def _civ(day, hours, minutes=0, seconds=0):
    return {
        "date": {"year": 2026, "month": 3, "day": day},
        "time": {"hours": hours, "minutes": minutes, "seconds": seconds},
    }


def _real(field, values, start, end, civil_start, civil_end, name, offset="3600s", source=_SOURCE):
    """An entry whose end, offsets and civil times are each its own."""
    return {
        "name": f"users/1/dataTypes/x/dataPoints/{name}",
        "dataSource": source,
        field: {
            "interval": {
                "startTime": start,
                "endTime": end,
                "startUtcOffset": offset,
                "endUtcOffset": offset,
                "civilStartTime": civil_start,
                "civilEndTime": civil_end,
            },
            **values,
        },
    }


_HANDLERS = [
    (google_sync.sync_food_entries, "nutrition-log", "nutritionLog", _FOOD, "food_entries"),
    (google_sync.sync_water_entries, "hydration-log", "hydrationLog", _WATER, "water_entries"),
]
_IDS = [table for *_, table in _HANDLERS]


def _sync(tmp_db, handler, data_type, points, start=date(2026, 3, 1), end=date(2026, 4, 1)):
    """Answer only a list of the type the handler should ask for."""
    asked = []

    def answer(t, s, e, view="list"):
        if (t, view) != (data_type, "list"):
            pytest.fail(f"asked for {t} {view}")
        asked.append((s, e))
        return points

    with patch.object(google_sync.api, "list_google_data_points", side_effect=answer):
        count = handler(tmp_db, start, end)
    tmp_db.commit()
    return count, asked


def _days(table, tmp_db):
    return db.query_day_lists(tmp_db, table, "2026-03-01", "2026-04-01")


@pytest.mark.parametrize("handler,data_type,field,values,table", _HANDLERS, ids=_IDS)
class TestADayIsItsEntries:
    def test_the_day_holds_every_entry_as_sent(
        self, tmp_db, handler, data_type, field, values, table
    ):
        """Two entries at one minute are two entries, not one."""
        points = [
            _entry(field, values, "2026-03-15T12:00:00Z", 15, name="1"),
            _entry(field, values, "2026-03-15T12:00:00Z", 15, name="2"),
        ]
        count, _ = _sync(tmp_db, handler, data_type, points)
        assert count == 1
        (row,) = _days(table, tmp_db)
        assert (row["date"], row["points"], row["provider"]) == ("2026-03-15", points, "google")

    def test_an_entry_missing_from_the_next_answer_leaves_the_day(
        self, tmp_db, handler, data_type, field, values, table
    ):
        kept = _entry(field, values, "2026-03-15T08:00:00Z", 15, name="1")
        deleted = _entry(field, values, "2026-03-15T12:00:00Z", 15, name="2")
        _sync(tmp_db, handler, data_type, [kept, deleted])
        _sync(tmp_db, handler, data_type, [kept])
        (row,) = _days(table, tmp_db)
        assert row["points"] == [kept]

    def test_a_day_emptied_in_the_app_keeps_its_last_entries(
        self, tmp_db, handler, data_type, field, values, table
    ):
        """Intended: an answer holding nothing for a day writes nothing, as for
        every day-list type, so no row is ever removed."""
        point = _entry(field, values, "2026-03-15T12:00:00Z", 15)
        _sync(tmp_db, handler, data_type, [point])
        _sync(tmp_db, handler, data_type, [])
        (row,) = _days(table, tmp_db)
        assert row["points"] == [point]

    def test_an_entry_is_dated_by_the_civil_start_the_filter_reads(
        self, tmp_db, handler, data_type, field, values, table
    ):
        """23:30 UTC on the 14th, logged an hour east, is 00:30 on the 15th."""
        point = _entry(field, values, "2026-03-14T23:30:00Z", 15, hours=0, offset="3600s")
        _sync(tmp_db, handler, data_type, [point])
        assert [r["date"] for r in _days(table, tmp_db)] == ["2026-03-15"]

    def test_the_request_has_a_day_of_margin_and_only_the_range_is_stored(
        self, tmp_db, handler, data_type, field, values, table
    ):
        points = [
            _entry(field, values, f"2026-03-{d:02d}T12:00:00Z", d, name=str(d))
            for d in (9, 10, 12, 13)
        ]
        count, asked = _sync(
            tmp_db, handler, data_type, points, start=date(2026, 3, 10), end=date(2026, 3, 12)
        )
        assert count == 2
        assert asked == [(date(2026, 3, 9), date(2026, 3, 14))]
        assert [r["date"] for r in _days(table, tmp_db)] == ["2026-03-10", "2026-03-12"]

    def test_an_entry_ending_on_the_next_day_is_stored_under_the_day_it_starts(
        self, tmp_db, handler, data_type, field, values, table
    ):
        """Not a shape measured: a guard that the day is the civil start, never the end."""
        point = _real(
            field,
            values,
            "2026-03-14T19:00:00Z",
            "2026-03-15T08:00:00Z",
            _civ(14, 19),
            _civ(15, 8),
            "1",
            offset="0s",
        )
        _sync(tmp_db, handler, data_type, [point])
        assert [r["date"] for r in _days(table, tmp_db)] == ["2026-03-14"]

    def test_an_undatable_entry_costs_only_itself(
        self, tmp_db, handler, data_type, field, values, table
    ):
        good = _entry(field, values, "2026-03-15T12:00:00Z", 15)
        count, _ = _sync(tmp_db, handler, data_type, [{field: {}}, good])
        assert count == 1
        (row,) = _days(table, tmp_db)
        assert row["points"] == [good]

    @pytest.mark.parametrize(
        "broken",
        ["not an object", {}, {"interval": {"startTime": "2026-03-15T12:00:00Z"}}],
        ids=["not-an-object", "no-interval", "no-civil-start"],
    )
    def test_an_entry_that_cannot_be_dated_is_skipped(
        self, tmp_db, handler, data_type, field, values, table, broken
    ):
        count, _ = _sync(tmp_db, handler, data_type, [{field: broken}])
        assert count == 0
        assert _days(table, tmp_db) == []


@pytest.mark.parametrize("table", ["food_entries", "water_entries"])
def test_entries_are_not_judged_as_a_stopped_series(tmp_db, table):
    """Logging is a habit, and a habit ending is not a fault."""
    start = date(2026, 3, 1)
    for i in range(30):
        getattr(db, f"save_{table}")(
            tmp_db, {"date": (start + timedelta(days=i)).isoformat(), "points": "[]"}
        )
    for i in range(50):
        db.save_sleep(tmp_db, {"date": (start + timedelta(days=i)).isoformat(), "total_minutes": 1})
    tmp_db.commit()
    findings = doctor._check_stopped_series(tmp_db, doctor._newest_per_type(tmp_db))
    assert f"{table} series" not in {f.name for f in findings}


def _path(tmp_db):
    return Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])


async def _call(db_path, **kwargs):
    from google_health_mcp.tools.food_tools import health_get_food_log

    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch("google_health_mcp.tools.food_tools.refresh_before_query") as refresh,
        patch.object(db, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        body = json.loads(await health_get_food_log(**kwargs))
    return body, refresh.call_args_list


def _store_both(tmp_db):
    food = _entry("nutritionLog", _FOOD, "2026-03-15T12:00:00Z", 15)
    water = _entry("hydrationLog", _WATER, "2026-03-15T13:00:00Z", 15)
    _sync(tmp_db, google_sync.sync_food_entries, "nutrition-log", [food])
    _sync(tmp_db, google_sync.sync_water_entries, "hydration-log", [water])
    return food, water


class TestTheTool:
    async def test_without_the_flag_it_refreshes_and_reads_the_totals_alone(self, tmp_db):
        _store_both(tmp_db)
        db.save_food_log(tmp_db, {"date": "2026-03-15", "water_ml": 250})
        tmp_db.commit()
        with patch.object(db, "query_day_lists") as day_lists:
            body, calls = await _call(_path(tmp_db), start_date="2026-03-15")
        assert [c.args[0] for c in calls] == ["food_log"]
        assert not day_lists.called
        assert set(body) == {"food_log", "count"}

    async def test_without_the_flag_entries_alone_are_no_answer(self, tmp_db):
        _store_both(tmp_db)
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15")
        assert body["message"] == "No food log data found for this period."

    async def test_with_the_flag_and_no_entries_the_lists_are_empty_not_missing(self, tmp_db):
        db.save_food_log(tmp_db, {"date": "2026-03-15", "water_ml": 250})
        tmp_db.commit()
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        assert (body["food_entries"], body["water_entries"], body["entry_count"]) == ([], [], 0)

    @pytest.mark.parametrize("flag,days", [(False, 30), (True, 7)])
    async def test_the_default_window_is_a_week_with_entries(self, tmp_db, flag, days):
        """A heavy logger's month of entries, nutrients included, is a large reply."""
        _, calls = await _call(_path(tmp_db), end_date="2026-03-31", include_entries=flag)
        (call,) = calls
        assert call.args[1] == date(2026, 3, 31) - timedelta(days=days)

    async def test_each_entry_comes_back_as_sent_with_its_date_times_and_source(self, tmp_db):
        _store_both(tmp_db)
        body, _ = await _call(
            _path(tmp_db), start_date="2026-03-15", end_date="2026-03-15", include_entries=True
        )
        assert body["food_entries"] == [
            {
                **_FOOD,
                "date": "2026-03-15",
                "start_time": "2026-03-15T12:00:00Z",
                "end_time": "2026-03-15T12:00:30Z",
                "start_utc_offset": "0s",
                "data_source": _SOURCE,
            }
        ]
        assert body["water_entries"] == [
            {
                **_WATER,
                "date": "2026-03-15",
                "start_time": "2026-03-15T13:00:00Z",
                "end_time": "2026-03-15T13:00:30Z",
                "start_utc_offset": "0s",
                "data_source": _SOURCE,
            }
        ]
        assert body["entry_count"] == 2

    async def test_the_local_time_travels_as_the_offset(self, tmp_db):
        """08:00 an hour east is 07:00Z; without the offset a model reads breakfast as 07:00."""
        point = _entry("nutritionLog", _FOOD, "2026-03-15T07:00:00Z", 15, hours=8, offset="3600s")
        # Saved after a clock change: the start's offset is the one that dates it.
        point["nutritionLog"]["interval"]["endUtcOffset"] = "0s"
        _sync(tmp_db, google_sync.sync_food_entries, "nutrition-log", [point])
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        (entry,) = body["food_entries"]
        assert (entry["start_time"], entry["start_utc_offset"]) == ("2026-03-15T07:00:00Z", "3600s")

    async def test_a_live_call_without_the_flag_refreshes_the_totals_alone(self, tmp_db):
        _, calls = await _call(_path(tmp_db), start_date="2026-03-15", live=True)
        assert [(c.args[0], c.args[3]) for c in calls] == [("food_log", True)]

    async def test_with_the_flag_a_cached_call_refreshes_nothing_live(self, tmp_db):
        _, calls = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        assert [(c.args[0], c.args[3]) for c in calls] == [
            (("food_log", "food_entries", "water_entries"), False)
        ]

    async def test_an_empty_window_without_the_flag_says_so(self, tmp_path):
        body, _ = await _call(tmp_path / "empty.db", start_date="2026-03-15")
        assert body["message"] == "No food log data found for this period."

    async def test_totals_still_come_back_with_the_flag(self, tmp_db):
        water = _entry("hydrationLog", _WATER, "2026-03-15T13:00:00Z", 15)
        _sync(tmp_db, google_sync.sync_water_entries, "hydration-log", [water])
        db.save_food_log(tmp_db, {"date": "2026-03-15", "water_ml": 250})
        tmp_db.commit()
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        assert [(d["date"], d["water_ml"]) for d in body["food_log"]] == [("2026-03-15", 250)]
        assert body["count"] == 1
        assert body["food_entries"] == []

    async def test_the_reply_dates_an_entry_by_its_local_day(self, tmp_db):
        point = _real(
            "nutritionLog",
            _FOOD,
            "2026-03-14T23:30:00Z",
            "2026-03-14T23:30:05Z",
            _civ(15, 0, 30),
            _civ(15, 0, 30, 5),
            "1",
        )
        _sync(tmp_db, google_sync.sync_food_entries, "nutrition-log", [point])
        body, _ = await _call(
            _path(tmp_db), start_date="2026-03-14", end_date="2026-03-15", include_entries=True
        )
        assert [(e["date"], e["start_time"]) for e in body["food_entries"]] == [
            ("2026-03-15", "2026-03-14T23:30:00Z")
        ]

    async def test_two_entries_at_one_minute_come_back_as_two_in_order(self, tmp_db):
        """Each keeps its own fields and source, as measured: same start, own save time."""
        plum = _real(
            "nutritionLog",
            {**_FOOD, "food": "foods/plum"},
            "2026-03-15T12:00:00Z",
            "2026-03-15T12:00:04Z",
            _civ(15, 13),
            _civ(15, 13, 0, 4),
            "a",
        )
        bread = _real(
            "nutritionLog",
            {**_FOOD, "foodDisplayName": "Bread", "food": "foods/bread", "energy": {"kcal": "80"}},
            "2026-03-15T12:00:00Z",
            "2026-03-15T12:00:09Z",
            _civ(15, 13),
            _civ(15, 13, 0, 9),
            "b",
            source=_APP,
        )
        _sync(tmp_db, google_sync.sync_food_entries, "nutrition-log", [plum, bread])
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        got = [
            (e["foodDisplayName"], e["food"], e["energy"], e["end_time"], e["data_source"])
            for e in body["food_entries"]
        ]
        assert got == [
            ("Plum", "foods/plum", {"kcal": 30}, "2026-03-15T12:00:04Z", _SOURCE),
            ("Bread", "foods/bread", {"kcal": "80"}, "2026-03-15T12:00:09Z", _APP),
        ]

    async def test_days_come_back_in_date_order(self, tmp_db):
        points = [
            _entry("hydrationLog", _WATER, f"2026-03-{d}T12:00:00Z", d, name=str(d))
            for d in (14, 15)
        ]
        _sync(tmp_db, google_sync.sync_water_entries, "hydration-log", points)
        body, _ = await _call(
            _path(tmp_db), start_date="2026-03-14", end_date="2026-03-15", include_entries=True
        )
        assert [e["date"] for e in body["water_entries"]] == ["2026-03-14", "2026-03-15"]

    async def test_a_stored_point_of_the_wrong_shape_is_passed_over(self, tmp_db):
        good = _entry("nutritionLog", _FOOD, "2026-03-15T12:00:00Z", 15)
        db.save_food_entries(
            tmp_db,
            {"date": "2026-03-15", "points": json.dumps(["x", {"nutritionLog": "y"}, good])},
        )
        tmp_db.commit()
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        assert body["entry_count"] == 1

    async def test_entries_come_back_on_a_day_with_no_total(self, tmp_db):
        """The daily rollup and the entries are separate syncs; one can land first."""
        _store_both(tmp_db)
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        assert body["food_log"] == [] and body["count"] == 0
        assert body["entry_count"] == 2

    async def test_every_entry_is_counted_not_every_day(self, tmp_db):
        points = [
            _entry("hydrationLog", _WATER, "2026-03-15T12:00:00Z", 15, name="1"),
            _entry("hydrationLog", _WATER, "2026-03-15T14:00:00Z", 15, name="2"),
        ]
        _sync(tmp_db, google_sync.sync_water_entries, "hydration-log", points)
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        assert body["entry_count"] == 2

    async def test_a_field_google_adds_cannot_overwrite_the_entrys_own(self, tmp_db):
        point = _entry(
            "nutritionLog",
            {
                **_FOOD,
                "date": "x",
                "start_time": "x",
                "end_time": "x",
                "start_utc_offset": "x",
                "data_source": "x",
            },
            "2026-03-15T12:00:00Z",
            15,
        )
        _sync(tmp_db, google_sync.sync_food_entries, "nutrition-log", [point])
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", include_entries=True)
        (entry,) = body["food_entries"]
        assert {k: entry[k] for k in ("date", "start_time", "end_time", "start_utc_offset")} == {
            "date": "2026-03-15",
            "start_time": "2026-03-15T12:00:00Z",
            "end_time": "2026-03-15T12:00:30Z",
            "start_utc_offset": "0s",
        }
        assert entry["data_source"] == _SOURCE

    @pytest.mark.parametrize("stored", [b"not zlib", "5", "true", '{"a": 1}'])
    async def test_one_unreadable_day_costs_only_that_day(self, tmp_db, stored):
        """Text parsing to something other than a list is as unreadable as text that will not."""
        db.save_food_entries(tmp_db, {"date": "2026-03-14", "points": stored})
        _store_both(tmp_db)
        body, _ = await _call(
            _path(tmp_db), start_date="2026-03-14", end_date="2026-03-15", include_entries=True
        )
        assert [e["date"] for e in body["food_entries"]] == ["2026-03-15"]

    async def test_with_the_flag_the_three_refresh_together_over_the_window(self, tmp_db):
        """Together, so lunch cannot be among the entries and missing from the total."""
        _, calls = await _call(
            _path(tmp_db),
            start_date="2026-03-10",
            end_date="2026-03-15",
            include_entries=True,
            live=True,
        )
        assert [c.args for c in calls] == [
            (
                ("food_log", "food_entries", "water_entries"),
                date(2026, 3, 10),
                date(2026, 3, 15),
                True,
            )
        ]

    async def test_a_failing_read_of_the_entries_closes_its_connection(self):
        """The totals read is what tests/test_connections.py fails; this fails the other."""
        import sqlite3

        from google_health_mcp.tools.food_tools import health_get_food_log

        opened, closed = [], []

        class _Recording(sqlite3.Connection):
            def close(self):
                closed.append(self)
                super().close()

        def get_db(*args, **kwargs):
            conn = sqlite3.connect(":memory:", factory=_Recording, check_same_thread=False)
            opened.append(conn)
            return conn

        with (
            patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
            patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
            patch("google_health_mcp.tools.food_tools.refresh_before_query"),
            patch.object(db, "get_db", get_db),
            patch.object(db, "query_food_log", return_value=[]),
            patch.object(db, "query_day_lists", side_effect=RuntimeError("the read failed")),
            pytest.raises(RuntimeError),
        ):
            client.exists.return_value = True
            tokens.exists.return_value = True
            await health_get_food_log(include_entries=True)
        assert opened, "no connection was opened, so this proves nothing"
        assert closed == opened

    async def test_an_empty_window_says_so(self, tmp_path):
        from google_health_mcp import helpers

        body, _ = await _call(tmp_path / "empty.db", start_date="2026-03-15", include_entries=True)
        assert body == {
            "message": "No food log data found for this period.",
            "hint": "Food and water are logged by hand, so most days have none. "
            + helpers.LIVE_HINT,
        }
