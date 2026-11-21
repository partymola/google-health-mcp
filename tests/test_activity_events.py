"""Sedentary periods, altitude gains and swim lengths: a day's events kept whole.

None of these points carries a name, and Google's own guide says a point's
interval can change after it is first sent, so no per-event key survives a
re-derivation. Each local day is one row holding the points the answer gave
for it, and a re-sync of the day replaces the list.
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync

# Fictional throughout.
_SOURCE = {"platform": "FITBIT", "recordingMethod": "PASSIVELY_MEASURED"}


def _civil(day, hours=0):
    return {"date": {"year": 2026, "month": 3, "day": day}, "time": {"hours": hours}}


def _event(field, start, end, civil_day, extra=None, source=None, civil_end_day=None):
    """No `name`: none of these types carries one. Google's merged sedentary
    stream carries no `dataSource` either (measured); the list types do."""
    if source is None and field != "sedentaryPeriod":
        source = _SOURCE
    point = {
        field: {
            "interval": {
                "startTime": start,
                "endTime": end,
                "startUtcOffset": "3600s",
                "endUtcOffset": "3600s",
                "civilStartTime": _civil(civil_day),
                "civilEndTime": _civil(civil_end_day or civil_day),
            },
            **(extra or {}),
        }
    }
    if source is not None:
        point["dataSource"] = source
    return point


_HANDLERS = [
    (
        google_sync.sync_sedentary_periods,
        "sedentary-period",
        "sedentaryPeriod",
        "reconcile",
        "sedentary_periods",
    ),
    (google_sync.sync_altitude_gains, "altitude", "altitude", "list", "altitude_gains"),
    (google_sync.sync_swim_lengths, "swim-lengths-data", "swimLengthsData", "list", "swim_lengths"),
]
_IDS = [table for *_, table in _HANDLERS]


def _sync(tmp_db, handler, data_type, view, points):
    """Answer only the type and view the handler should ask for."""

    def answer(asked, start, end, view_asked="list"):
        if (asked, view_asked) != (data_type, view):
            pytest.fail(f"asked for {asked} {view_asked}")
        return points

    with patch.object(
        google_sync.api,
        "list_google_data_points",
        side_effect=lambda t, s, e, view="list": answer(t, s, e, view),
    ):
        count = handler(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
    tmp_db.commit()
    return count


def _days(table, tmp_db, start="2026-03-01", end="2026-04-01"):
    return db.query_day_lists(tmp_db, table, start, end)


@pytest.mark.parametrize("handler,data_type,field,view,table", _HANDLERS, ids=_IDS)
class TestADayIsItsEvents:
    def test_the_day_holds_every_point_as_sent_in_answer_order(
        self, tmp_db, handler, data_type, field, view, table
    ):
        points = [
            _event(field, "2026-03-15T09:00:00Z", "2026-03-15T09:30:00Z", 15),
            _event(field, "2026-03-15T07:00:00Z", "2026-03-15T07:45:00Z", 15),
        ]
        assert _sync(tmp_db, handler, data_type, view, points) == 1
        (row,) = _days(table, tmp_db)
        assert row["date"] == "2026-03-15"
        assert row["points"] == points
        assert row["provider"] == "google"

    def test_points_are_dated_by_the_civil_start_the_filter_reads(
        self, tmp_db, handler, data_type, field, view, table
    ):
        """23:30 UTC on the 14th is the 15th an hour east, and so is its civil start."""
        point = _event(field, "2026-03-14T23:30:00Z", "2026-03-15T00:10:00Z", 15)
        _sync(tmp_db, handler, data_type, view, [point])
        assert [r["date"] for r in _days(table, tmp_db)] == ["2026-03-15"]

    def test_a_period_across_local_midnight_belongs_to_its_start_day(
        self, tmp_db, handler, data_type, field, view, table
    ):
        point = _event(field, "2026-03-15T22:50:00Z", "2026-03-15T23:20:00Z", 15, civil_end_day=16)
        _sync(tmp_db, handler, data_type, view, [point])
        assert [r["date"] for r in _days(table, tmp_db)] == ["2026-03-15"]

    def test_a_resync_replaces_the_days_list(self, tmp_db, handler, data_type, field, view, table):
        """A period Google moved or merged leaves nothing of its old self behind."""
        first = [
            _event(field, "2026-03-15T09:00:00Z", "2026-03-15T09:30:00Z", 15),
            _event(field, "2026-03-15T10:00:00Z", "2026-03-15T10:30:00Z", 15),
        ]
        merged = [_event(field, "2026-03-15T08:55:00Z", "2026-03-15T10:30:00Z", 15)]
        _sync(tmp_db, handler, data_type, view, first)
        _sync(tmp_db, handler, data_type, view, merged)
        (row,) = _days(table, tmp_db)
        assert row["points"] == merged

    def test_a_day_the_answer_lacks_keeps_what_it_had(
        self, tmp_db, handler, data_type, field, view, table
    ):
        """No row is ever removed: an empty answer for a day writes nothing."""
        _sync(
            tmp_db,
            handler,
            data_type,
            view,
            [_event(field, "2026-03-15T09:00:00Z", "2026-03-15T09:30:00Z", 15)],
        )
        _sync(
            tmp_db,
            handler,
            data_type,
            view,
            [_event(field, "2026-03-16T09:00:00Z", "2026-03-16T09:30:00Z", 16)],
        )
        assert [r["date"] for r in _days(table, tmp_db)] == ["2026-03-15", "2026-03-16"]

    def test_two_days_are_two_rows(self, tmp_db, handler, data_type, field, view, table):
        points = [
            _event(field, "2026-03-15T09:00:00Z", "2026-03-15T09:30:00Z", 15),
            _event(field, "2026-03-16T09:00:00Z", "2026-03-16T09:30:00Z", 16),
        ]
        assert _sync(tmp_db, handler, data_type, view, points) == 2
        assert [len(r["points"]) for r in _days(table, tmp_db)] == [1, 1]

    def test_the_request_has_a_day_of_margin_and_only_the_range_is_stored(
        self, tmp_db, handler, data_type, field, view, table
    ):
        """An edge day of a request may be answered thinly; neither edge of the
        range asked for is ever one, and a day outside the range is left alone."""
        asked = []

        def answer(t, s, e, view="list"):
            asked.append((s, e))
            return [
                _event(field, "2026-03-09T09:00:00Z", "2026-03-09T09:30:00Z", 9),
                _event(field, "2026-03-10T09:00:00Z", "2026-03-10T09:30:00Z", 10),
                _event(field, "2026-03-12T09:00:00Z", "2026-03-12T09:30:00Z", 12),
                _event(field, "2026-03-13T09:00:00Z", "2026-03-13T09:30:00Z", 13),
            ]

        with patch.object(google_sync.api, "list_google_data_points", side_effect=answer):
            assert handler(tmp_db, date(2026, 3, 10), date(2026, 3, 12)) == 2
        tmp_db.commit()
        assert asked == [(date(2026, 3, 9), date(2026, 3, 14))]
        assert [r["date"] for r in _days(table, tmp_db)] == ["2026-03-10", "2026-03-12"]

    @pytest.mark.parametrize(
        "broken",
        ["not an object", {}, {"interval": {"startTime": "2026-03-15T09:00:00Z"}}],
        ids=["not-an-object", "no-interval", "no-civil-start"],
    )
    def test_a_point_that_cannot_be_dated_is_skipped(
        self, tmp_db, handler, data_type, field, view, table, broken
    ):
        assert _sync(tmp_db, handler, data_type, view, [{field: broken}]) == 0
        assert _days(table, tmp_db) == []


def test_only_day_list_tables_are_read_as_one(tmp_db):
    with pytest.raises(ValueError):
        db.query_day_lists(tmp_db, "heart_rate", "2026-03-01", "2026-04-01")


def test_the_days_list_is_stored_compressed(tmp_db):
    point = _event("sedentaryPeriod", "2026-03-15T09:00:00Z", "2026-03-15T09:30:00Z", 15)
    _sync(tmp_db, google_sync.sync_sedentary_periods, "sedentary-period", "reconcile", [point])
    (kind,) = tmp_db.execute("SELECT typeof(points) FROM sedentary_periods").fetchone()
    assert kind == "blob"


def test_an_unreadable_stored_list_reads_as_absent(tmp_db):
    db.save_sedentary_periods(
        tmp_db, {"date": "2026-03-15", "points": b"not zlib", "provider": "google"}
    )
    tmp_db.commit()
    (row,) = _days("sedentary_periods", tmp_db)
    assert row["points"] is None


@pytest.mark.parametrize(
    "table,column,judged",
    [
        ("altitude_gains", "points", False),
        ("swim_lengths", "points", False),
        ("sedentary_periods", "points", True),
        ("respiratory_rate_sleep", "points", True),
        ("heart_rate_zones", "zone_boundaries", True),
    ],
)
def test_which_new_series_are_judged_as_stopped(tmp_db, table, column, judged):
    """Altitude is recorded only on days with a climb, and passive swim detection
    can end with nothing broken; the other new series stopping is worth a warning."""
    from datetime import timedelta

    from google_health_mcp import doctor

    start = date(2026, 3, 1)
    for i in range(30):
        day = (start + timedelta(days=i)).isoformat()
        getattr(db, f"save_{table}")(tmp_db, {"date": day, column: "[]"})
    for i in range(50):
        db.save_sleep(tmp_db, {"date": (start + timedelta(days=i)).isoformat(), "total_minutes": 1})
    tmp_db.commit()
    findings = doctor._check_stopped_series(tmp_db, doctor._newest_per_type(tmp_db))
    stopped = {f.name for f in findings if f.check == doctor.STOPPED_SERIES}
    assert (f"{table} series" in stopped) is judged


async def _call(db_path, **kwargs):
    from google_health_mcp.tools.activity_tools import health_get_activity_events

    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch("google_health_mcp.tools.activity_tools.refresh_before_query") as refresh,
        patch.object(db, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        body = json.loads(await health_get_activity_events(**kwargs))
    return body, refresh.call_args_list


def _path(tmp_db):
    return Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])


_VALUES = {
    "sedentaryPeriod": {},
    "altitude": {"gainMillimeters": "3000"},
    "swimLengthsData": {"strokeCount": "0"},
}


class TestTheTool:
    @pytest.mark.parametrize("handler,data_type,field,view,table", _HANDLERS, ids=_IDS)
    async def test_each_event_comes_back_as_its_interval_and_values(
        self, tmp_db, handler, data_type, field, view, table
    ):
        point = _event(field, "2026-03-15T09:00:00Z", "2026-03-15T09:01:00Z", 15, _VALUES[field])
        _sync(tmp_db, handler, data_type, view, [point])
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", end_date="2026-03-15")
        assert body[table] == [
            {
                "date": "2026-03-15",
                "start_time": "2026-03-15T09:00:00Z",
                "end_time": "2026-03-15T09:01:00Z",
                **_VALUES[field],
            }
        ]
        assert body["count"] == 1

    async def test_every_event_is_counted_not_every_day(self, tmp_db):
        _sync(
            tmp_db,
            google_sync.sync_sedentary_periods,
            "sedentary-period",
            "reconcile",
            [
                _event("sedentaryPeriod", "2026-03-15T09:00:00Z", "2026-03-15T09:30:00Z", 15),
                _event("sedentaryPeriod", "2026-03-15T11:00:00Z", "2026-03-15T11:30:00Z", 15),
            ],
        )
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", end_date="2026-03-15")
        assert body["count"] == 2

    async def test_one_unreadable_day_costs_only_that_day(self, tmp_db):
        db.save_sedentary_periods(tmp_db, {"date": "2026-03-14", "points": b"not zlib"})
        _sync(
            tmp_db,
            google_sync.sync_sedentary_periods,
            "sedentary-period",
            "reconcile",
            [_event("sedentaryPeriod", "2026-03-15T09:00:00Z", "2026-03-15T09:30:00Z", 15)],
        )
        body, _ = await _call(_path(tmp_db), start_date="2026-03-14", end_date="2026-03-15")
        assert [e["date"] for e in body["sedentary_periods"]] == ["2026-03-15"]

    async def test_live_reaches_every_refresh(self, tmp_db):
        _, calls = await _call(_path(tmp_db), start_date="2026-03-15", live=True)
        assert calls and all(c.args[3] is True for c in calls)

    async def test_a_field_google_adds_cannot_overwrite_the_events_own(self, tmp_db):
        point = _event(
            "altitude",
            "2026-03-15T09:00:00Z",
            "2026-03-15T09:01:00Z",
            15,
            {"date": "not ours", "start_time": "not ours", "end_time": "not ours"},
        )
        _sync(tmp_db, google_sync.sync_altitude_gains, "altitude", "list", [point])
        body, _ = await _call(_path(tmp_db), start_date="2026-03-15", end_date="2026-03-15")
        (event,) = body["altitude_gains"]
        assert (event["date"], event["start_time"], event["end_time"]) == (
            "2026-03-15",
            "2026-03-15T09:00:00Z",
            "2026-03-15T09:01:00Z",
        )

    async def test_the_records_come_back_whole_only_when_asked(self, tmp_db):
        point = _event(
            "swimLengthsData",
            "2026-03-15T09:00:00Z",
            "2026-03-15T09:01:00Z",
            15,
            {"strokeCount": "0"},
        )
        _sync(tmp_db, google_sync.sync_swim_lengths, "swim-lengths-data", "list", [point])
        body, _ = await _call(
            _path(tmp_db), start_date="2026-03-15", end_date="2026-03-15", include_records=True
        )
        assert body["swim_lengths"] == [{"date": "2026-03-15", "record": point}]

    async def test_it_refreshes_the_three_types_over_the_window(self, tmp_db):
        _, calls = await _call(_path(tmp_db), start_date="2026-03-10", end_date="2026-03-15")
        assert {c.args[0] for c in calls} == {"sedentary_periods", "altitude_gains", "swim_lengths"}
        assert {(c.args[1], c.args[2]) for c in calls} == {(date(2026, 3, 10), date(2026, 3, 15))}

    async def test_the_default_window_is_a_week(self, tmp_db):
        _, calls = await _call(_path(tmp_db), end_date="2026-03-15")
        assert {c.args[1] for c in calls} == {date(2026, 3, 8)}

    async def test_an_empty_window_says_so(self, tmp_path):
        body, _ = await _call(tmp_path / "empty.db", start_date="2026-03-15")
        assert body["message"] == "No activity events found for this period."
