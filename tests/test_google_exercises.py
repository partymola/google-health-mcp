"""Exercises: the one table where the two identifier spaces cannot meet.

An imported workout carries its source's identifier; Google keys each one by
resource name. Neither can collide with the other, so the same session would
insert twice rather than being corrected - which is why identity is settled
here.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync


def _exercise(
    name="users/me/dataTypes/exercise/dataPoints/fake-1",
    start="2026-03-15T09:43:50Z",
    offset="3600s",
    duration="1576.800s",
    display="Cycle",
    metrics=None,
    device="Fictional Watch",
):
    point = {
        "exercise": {
            "displayName": display,
            "exerciseType": "BIKING",
            "activeDuration": duration,
            "interval": {"startTime": start, "startUtcOffset": offset},
            "metricsSummary": metrics if metrics is not None else {"caloriesKcal": 89},
        }
    }
    if name is not None:
        point["name"] = name
    if device is not None:
        point["dataSource"] = {"device": {"displayName": device}}
    return point


@pytest.fixture
def sync(tmp_db):
    def run(points):
        with patch.object(google_sync.api, "list_google_data_points", return_value=points):
            count = google_sync.sync_exercises(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        return count

    return run


class TestIdentity:
    def test_a_workout_is_keyed_by_its_resource_name(self, tmp_db, sync):
        sync([_exercise(name="users/me/dataTypes/exercise/dataPoints/abc")])
        rows = db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")
        assert rows[0]["log_id"] == "users/me/dataTypes/exercise/dataPoints/abc"

    def test_a_workout_with_no_identifier_is_skipped(self, tmp_db, sync):
        """An empty id is not an identity: every such workout would overwrite the last."""
        assert sync([_exercise(name=None)]) == 0
        assert db.query_exercises(tmp_db, "2026-03-01", "2026-04-01") == []

    def test_re_syncing_corrects_rather_than_duplicating(self, tmp_db, sync):
        sync([_exercise(metrics={"caloriesKcal": 89})])
        sync([_exercise(metrics={"caloriesKcal": 95})])
        rows = db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")
        assert len(rows) == 1
        assert rows[0]["calories"] == 95

    def test_a_day_an_earlier_provider_recorded_is_left_to_it(self, tmp_db, sync):
        """The two id spaces cannot collide, so the same session lands twice.

        Nothing stored can tell that it is the same session either: keying on
        (start_time, duration, name) collides across the Strava mirrors of
        watch-tracked workouts, so deduping on it would delete real rows.
        Days another provider already covered are therefore skipped.
        """
        db.save_exercise(tmp_db, "1234567", {"date": "2026-03-15", "name": "Ride", "calories": 90})
        tmp_db.commit()

        assert sync([_exercise()]) == 0
        rows = db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")
        assert len(rows) == 1
        assert rows[0]["log_id"] == "1234567"
        assert rows[0]["calories"] == 90

    def test_a_day_the_earlier_provider_missed_is_still_filled(self, tmp_db, sync):
        """Per day, not a cut-off date: a day it never recorded has nothing to duplicate.

        The gap is deliberately *inside* the earlier provider's history - a
        workout after its last day would be stored under either rule, so it
        would not tell the two apart.
        """
        db.save_exercise(tmp_db, "1234567", {"date": "2026-03-20", "name": "Ride"})
        tmp_db.commit()

        assert sync([_exercise(start="2026-03-15T09:43:50Z")]) == 1
        assert len(db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")) == 2

    def test_an_install_with_no_earlier_history_takes_everything(self, tmp_db, sync):
        assert sync([_exercise()]) == 1


class TestTheFields:
    def test_the_duration_is_seconds_rounded_to_minutes(self, tmp_db, sync):
        """activeDuration is a string like '1576.800s'; the column is minutes."""
        sync([_exercise(duration="1576.800s")])
        assert db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")[0]["duration_min"] == 26

    def test_a_malformed_duration_is_absent_rather_than_zero(self, tmp_db, sync):
        sync([_exercise(duration="not-a-duration")])
        assert db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")[0]["duration_min"] is None

    def test_distance_converts_from_millimetres(self, tmp_db, sync):
        sync([_exercise(metrics={"distanceMillimeters": "5000000"})])
        assert db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")[0]["distance_km"] == 5.0

    def test_metrics_a_workout_lacks_stay_absent(self, tmp_db, sync):
        """Steps and distance are absent on some exercise types, present on others."""
        sync([_exercise(metrics={"caloriesKcal": 89})])
        row = db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")[0]
        assert row["calories"] == 89
        assert row["steps"] is None
        assert row["distance_km"] is None

    def test_the_device_becomes_the_source(self, tmp_db, sync):
        """That column already held whatever logged a workout."""
        sync([_exercise(device="Fictional Watch")])
        assert db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")[0]["source"] == (
            "Fictional Watch"
        )

    def test_the_date_is_local_not_utc(self, tmp_db, sync):
        """A workout starting at 00:30 local belongs to that day."""
        sync([_exercise(start="2026-03-15T23:30:00Z", offset="3600s")])
        assert db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")[0]["date"] == "2026-03-16"

    def test_the_display_name_is_preferred_over_the_enum(self, tmp_db, sync):
        sync([_exercise(display="Cycle")])
        assert db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")[0]["name"] == "Cycle"
