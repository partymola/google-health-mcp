"""Sample types: measurements taken at an instant rather than summarised per day.

Weight and body fat are two data types landing in one table, and a reading
arrives with the moment it was taken rather than a date - so the day it
belongs to comes from the local civil time, not from the UTC instant.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync


def _sample(field, value, day=15, hours=11, offset="3600s"):
    return {
        field: {
            "sampleTime": {
                "civilTime": {
                    "date": {"year": 2026, "month": 3, "day": day},
                    "time": {"hours": hours, "minutes": 39, "seconds": 50},
                },
                "physicalTime": f"2026-03-{day:02d}T{hours - 1:02d}:39:50Z",
                "utcOffset": offset,
            },
            **value,
        }
    }


@pytest.fixture
def sync(tmp_db):
    def run(handler, by_type):
        with patch.object(
            google_sync.api,
            "list_google_data_points",
            side_effect=lambda t, s, e: by_type.get(t, []),
        ):
            count = handler(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        return count

    return run


class TestWeight:
    def test_grams_become_kilograms(self, tmp_db, sync):
        """The column has always held kilograms; the API sends grams."""
        sync(google_sync.sync_weight, {"weight": [_sample("weight", {"weightGrams": 72762})]})
        assert db.query_weight(tmp_db, "2026-03-15", "2026-03-15")[0]["weight_kg"] == 72.762

    def test_body_fat_lands_in_the_same_row(self, tmp_db, sync):
        """Two data types, one table - a reading is one event on the scales."""
        sync(
            google_sync.sync_weight,
            {
                "weight": [_sample("weight", {"weightGrams": 72762})],
                "body-fat": [_sample("bodyFat", {"percentage": 18.5})],
            },
        )
        rows = db.query_weight(tmp_db, "2026-03-15", "2026-03-15")
        assert len(rows) == 1
        assert rows[0]["weight_kg"] == 72.762
        assert rows[0]["fat_pct"] == 18.5

    def test_bmi_is_never_written(self, tmp_db, sync):
        """No Google source. A derived BMI beside three years of reported ones
        is a residual built from estimates, inside a column."""
        db.save_weight(tmp_db, {"date": "2026-03-15", "bmi": 25.0})
        tmp_db.commit()
        sync(google_sync.sync_weight, {"weight": [_sample("weight", {"weightGrams": 72762})]})
        assert db.query_weight(tmp_db, "2026-03-15", "2026-03-15")[0]["bmi"] == 25.0

    def test_the_day_comes_from_local_civil_time(self, tmp_db, sync):
        """A reading at 00:30 local is that day, not the UTC day before."""
        sync(
            google_sync.sync_weight,
            {"weight": [_sample("weight", {"weightGrams": 72000}, day=16, hours=0)]},
        )
        assert db.query_weight(tmp_db, "2026-03-01", "2026-04-01")[0]["date"] == "2026-03-16"

    def test_a_reading_with_no_time_is_skipped(self, tmp_db, sync):
        assert sync(google_sync.sync_weight, {"weight": [{"weight": {"weightGrams": 72000}}]}) == 0


class TestCoreTemperature:
    def test_a_reading_is_stored_against_its_timestamp(self, tmp_db, sync):
        sync(
            google_sync.sync_core_temperature,
            {
                "core-body-temperature": [
                    _sample("coreBodyTemperature", {"temperatureCelsius": 37.2, "id": "abc"})
                ]
            },
        )
        rows = db.query_core_temperature(tmp_db, "2026-03-15", "2026-03-15")
        assert len(rows) == 1
        assert rows[0]["temp_celsius"] == 37.2
        assert rows[0]["datetime"].startswith("2026-03-15T11:39:50")

    def test_two_readings_the_same_day_are_both_kept(self, tmp_db, sync):
        sync(
            google_sync.sync_core_temperature,
            {
                "core-body-temperature": [
                    _sample("coreBodyTemperature", {"temperatureCelsius": 37.2}, hours=11),
                    _sample("coreBodyTemperature", {"temperatureCelsius": 36.8}, hours=18),
                ]
            },
        )
        assert len(db.query_core_temperature(tmp_db, "2026-03-15", "2026-03-15")) == 2

    def test_re_syncing_does_not_duplicate(self, tmp_db, sync):
        points = {
            "core-body-temperature": [_sample("coreBodyTemperature", {"temperatureCelsius": 37.2})]
        }
        sync(google_sync.sync_core_temperature, points)
        sync(google_sync.sync_core_temperature, points)
        assert len(db.query_core_temperature(tmp_db, "2026-03-15", "2026-03-15")) == 1
