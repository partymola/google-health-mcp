"""Sample types: measurements taken at an instant rather than summarised per day.

Weight and body fat are two data types landing in one table, and a reading
arrives with the moment it was taken rather than a date - so the day it
belongs to comes from the local civil time, not from the UTC instant.
"""

import sqlite3
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
        """No Google source. A derived BMI beside an imported history of
        reported ones is a residual built from estimates, inside a column."""
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

    def test_everything_else_the_reading_carries_is_stored(self, tmp_db, sync):
        point = _sample(
            "coreBodyTemperature",
            {"temperatureCelsius": 37.2, "id": "abc", "measurementLocation": "EAR"},
        )
        point["dataSource"] = {"platform": "FICTIONAL"}
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [point]})
        (row,) = db.query_core_temperature(tmp_db, "2026-03-15", "2026-03-15")
        assert row["reading_id"] == "abc"
        assert row["measurement_location"] == "EAR"
        assert row["data_source"] == {"platform": "FICTIONAL"}
        assert row["provider"] == "google"

    def test_a_re_sync_fills_a_reading_stored_before_those_fields_were(self, tmp_db, sync):
        """A reading already held is corrected, not ignored, so its new
        columns fill on the next sync of its window."""
        db.save_core_temperature(
            tmp_db, {"datetime": "2026-03-15T11:39:50", "date": "2026-03-15", "temp_celsius": 37.2}
        )
        tmp_db.commit()
        point = _sample("coreBodyTemperature", {"temperatureCelsius": 37.2, "id": "abc"})
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [point]})
        (row,) = db.query_core_temperature(tmp_db, "2026-03-15", "2026-03-15")
        assert row["reading_id"] == "abc"
        assert row["provider"] == "google"

    def test_an_edited_value_replaces_the_earlier_one(self, tmp_db, sync):
        """Measured: an edit in the app keeps the reading's id and changes its
        value. The value is part of the key, so without this the corrected
        39.8 would stay beside the 36.8 and still read as a fever."""
        for celsius in (39.8, 36.8):
            point = _sample("coreBodyTemperature", {"temperatureCelsius": celsius, "id": "abc"})
            sync(google_sync.sync_core_temperature, {"core-body-temperature": [point]})
        rows = db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")
        assert [(r["temp_celsius"], r["reading_id"]) for r in rows] == [(36.8, "abc")]

    def test_an_edited_time_replaces_the_earlier_one(self, tmp_db, sync):
        """Only a value edit was measured; a time edit keeping the id is handled alike."""
        for day in (14, 15):
            point = _sample(
                "coreBodyTemperature", {"temperatureCelsius": 37.2, "id": "abc"}, day=day
            )
            sync(google_sync.sync_core_temperature, {"core-body-temperature": [point]})
        rows = db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")
        assert [r["date"] for r in rows] == ["2026-03-15"]

    def test_a_reading_this_sync_did_not_write_is_never_removed(self, tmp_db, sync):
        db.save_core_temperature(
            tmp_db, {"datetime": "2026-03-15T08:00:00", "date": "2026-03-15", "temp_celsius": 38.0}
        )
        tmp_db.commit()
        point = _sample("coreBodyTemperature", {"temperatureCelsius": 36.8, "id": "abc"})
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [point]})
        rows = db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")
        assert sorted(r["temp_celsius"] for r in rows) == [36.8, 38.0]

    def test_a_reading_deleted_in_the_app_is_removed(self, tmp_db, sync):
        point = _sample("coreBodyTemperature", {"temperatureCelsius": 37.2, "id": "abc"})
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [point]})
        sync(google_sync.sync_core_temperature, {"core-body-temperature": []})
        assert db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01") == []

    def test_a_reading_the_sync_cannot_read_is_no_evidence(self, tmp_db, sync):
        stored = _sample("coreBodyTemperature", {"temperatureCelsius": 37.0, "id": "abc"})
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [stored]})
        unreadable = _sample("coreBodyTemperature", {"id": "abc"}, hours=12)
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [unreadable]})
        assert len(db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")) == 1

    def test_an_id_sent_under_two_keys_in_one_answer_keeps_both(self, tmp_db, sync):
        """What the answer holds is kept, whatever id it carries."""
        points = [
            _sample("coreBodyTemperature", {"temperatureCelsius": 37.0, "id": "abc"}, hours=10),
            _sample("coreBodyTemperature", {"temperatureCelsius": 38.0, "id": "abc"}, hours=11),
        ]
        for _ in range(2):
            sync(google_sync.sync_core_temperature, {"core-body-temperature": points})
        rows = db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")
        assert sorted(r["temp_celsius"] for r in rows) == [37.0, 38.0]

    def test_a_row_outside_the_window_is_never_removed(self, tmp_db):
        """The answer speaks for the dates asked for and no others."""
        old = _sample("coreBodyTemperature", {"temperatureCelsius": 37.0, "id": "abc"}, day=5)
        new = _sample("coreBodyTemperature", {"temperatureCelsius": 37.0, "id": "abc"}, day=20)
        for point, start, end in (
            (old, date(2026, 3, 1), date(2026, 3, 6)),
            (new, date(2026, 3, 18), date(2026, 3, 21)),
        ):
            with patch.object(google_sync.api, "list_google_data_points", return_value=[point]):
                google_sync.sync_core_temperature(tmp_db, start, end)
            tmp_db.commit()
        rows = db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")
        assert [r["date"] for r in rows] == ["2026-03-05", "2026-03-20"]

    def test_a_save_that_fails_removes_nothing(self, tmp_db, sync):
        """The error path commits, so a removal made before a failed save would stay."""
        first = _sample("coreBodyTemperature", {"temperatureCelsius": 39.8, "id": "abc"})
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [first]})
        edited = _sample("coreBodyTemperature", {"temperatureCelsius": 36.8, "id": "abc"})
        with (
            patch.object(db, "save_core_temperature", side_effect=sqlite3.OperationalError("x")),
            pytest.raises(sqlite3.OperationalError),
        ):
            sync(google_sync.sync_core_temperature, {"core-body-temperature": [edited]})
        tmp_db.commit()
        rows = db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")
        assert [r["temp_celsius"] for r in rows] == [39.8]

    def test_a_reading_sent_again_unchanged_is_kept(self, tmp_db, sync):
        point = _sample("coreBodyTemperature", {"temperatureCelsius": 37.2, "id": "abc"})
        for _ in range(2):
            sync(google_sync.sync_core_temperature, {"core-body-temperature": [point]})
        (row,) = db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")
        assert (row["temp_celsius"], row["reading_id"]) == (37.2, "abc")

    def test_what_a_reading_lacks_is_stored_as_absent(self, tmp_db, sync):
        full = _sample(
            "coreBodyTemperature",
            {"temperatureCelsius": 37.2, "measurementLocation": "EAR", "id": "abc"},
        )
        full["dataSource"] = {"platform": "FICTIONAL"}
        without = _sample("coreBodyTemperature", {"temperatureCelsius": 37.2})
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [full]})
        sync(google_sync.sync_core_temperature, {"core-body-temperature": [without]})
        (row,) = db.query_core_temperature(tmp_db, "2026-03-15", "2026-03-15")
        assert row["measurement_location"] is None
        assert row["reading_id"] is None
        assert row["data_source"] is None

    def test_a_reading_with_no_value_is_skipped(self, tmp_db, sync):
        point = _sample("coreBodyTemperature", {"id": "abc"})
        assert sync(google_sync.sync_core_temperature, {"core-body-temperature": [point]}) == 0
        assert db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01") == []


def test_one_function_removes_rows():
    """Every removal goes through one function, so its callers are the whole
    list of what may remove a row. A second DELETE is a new removal path and
    should arrive as a decision."""
    import ast
    import re
    from pathlib import Path

    import google_health_mcp

    found = []
    for path in sorted(Path(google_health_mcp.__file__).parent.rglob("*.py")):
        tree = ast.parse(path.read_text())
        # Every string in the module, each named by the innermost function
        # holding it: async tools and module-level text included.
        owner = {}
        for func in ast.walk(tree):
            if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(func):
                    owner[id(node)] = func.name
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"\bdelete\s+from\b", node.value, re.IGNORECASE):
                    found.append((path.name, owner.get(id(node), "<module>")))
    assert found == [("db.py", "remove_rows")]
