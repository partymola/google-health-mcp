"""food_log and cardio_fitness, the two tables with a daily value and no session.

Neither type returns data on the account these were built against, so the field
names come from the published schemas rather than from a reading. What can be
pinned without live data is where each value lands: cardio fitness keeps a
reported band and a single value in separate columns, because one number
written into both ends of the band would quietly destroy it, and food log is
assembled from two independent sources that must not blank each other on a day
only one of them reports.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync


def _daily_point(field, value, day=15):
    return {field: {"date": {"year": 2026, "month": 3, "day": day}, **value}}


def _rollup_point(field, value, day=15):
    return {
        "civilStartTime": {"date": {"year": 2026, "month": 3, "day": day}, "time": {}},
        field: value,
    }


@pytest.fixture
def sync_cardio_fitness(tmp_db):
    def run(points):
        with patch.object(google_sync.api, "list_google_data_points", return_value=points):
            count = google_sync.sync_cardio_fitness(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        return count

    return run


@pytest.fixture
def sync_food_log(tmp_db):
    def run(rollups=None, listed=None):
        rollups = rollups or {}
        with (
            patch.object(
                google_sync.api, "daily_roll_up", side_effect=lambda t, s, e: rollups.get(t, [])
            ),
            patch.object(
                google_sync.api, "list_google_data_points", return_value=listed or []
            ) as listing,
        ):
            count = google_sync.sync_food_log(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        return count, listing

    return run


class TestCardioFitnessKeepsTwoDefinitionsApart:
    def test_the_single_value_lands_in_its_own_column(self, tmp_db, sync_cardio_fitness):
        sync_cardio_fitness([_daily_point("dailyVo2Max", {"vo2Max": 42.5})])
        row = db.query_cardio_fitness(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["vo2_max"] == 42.5

    def test_a_stored_range_is_never_overwritten_by_it(self, tmp_db, sync_cardio_fitness):
        """A single value is not a band, and the band columns are not its ends.

        Writing the one value into both ends of the band would collapse a real
        four-wide range and read as a measurement rather than as a change of
        provider - and a trend averaging the two columns steps on the day the
        imported series ends.
        """
        db.save_cardio_fitness(
            tmp_db, {"date": "2026-03-15", "vo2_max_low": 40.0, "vo2_max_high": 44.0}
        )
        tmp_db.commit()
        sync_cardio_fitness([_daily_point("dailyVo2Max", {"vo2Max": 42.0})])
        row = db.query_cardio_fitness(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert (row["vo2_max_low"], row["vo2_max_high"]) == (40.0, 44.0)
        assert row["vo2_max"] == 42.0

    def test_a_point_carrying_no_value_stores_nothing(self, tmp_db, sync_cardio_fitness):
        """A day with no reading is not a day of zero cardio fitness."""
        sync_cardio_fitness([_daily_point("dailyVo2Max", {"cardioFitnessLevel": "GOOD"})])
        assert db.query_cardio_fitness(tmp_db, "2026-03-15", "2026-03-15") == []


class TestFoodLogMergesItsTwoSources:
    def test_energy_and_water_land_in_one_row(self, tmp_db, sync_food_log):
        sync_food_log(
            rollups={
                "nutrition-log": [_rollup_point("nutritionLog", {"energy": {"kcalSum": 1850.5}})],
                "hydration-log": [
                    _rollup_point("hydrationLog", {"amountConsumed": {"millilitersSum": 1250.0}})
                ],
            }
        )
        row = db.query_food_log(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["calories_in"] == 1850
        assert row["water_ml"] == 1250

    def test_a_day_with_only_water_keeps_the_calories_already_stored(self, tmp_db, sync_food_log):
        """The two sources are independent, and a quiet one withdraws nothing."""
        db.save_food_log(tmp_db, {"date": "2026-03-15", "calories_in": 2000, "water_ml": 500})
        tmp_db.commit()
        sync_food_log(
            rollups={
                "hydration-log": [
                    _rollup_point("hydrationLog", {"amountConsumed": {"millilitersSum": 900.0}})
                ]
            }
        )
        row = db.query_food_log(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["calories_in"] == 2000
        assert row["water_ml"] == 900

    def test_the_list_form_leaf_names_are_not_what_it_reads(self, tmp_db, sync_food_log):
        """A rollup's value fields carry a `Sum` suffix the list's do not.

        Reading a rollup with the list's inner name parses to nothing while the
        request succeeds, which is indistinguishable from a day nobody logged.
        """
        sync_food_log(
            rollups={
                "nutrition-log": [_rollup_point("nutritionLog", {"energy": {"kcal": 1850.5}})],
                "hydration-log": [
                    _rollup_point("hydrationLog", {"amountConsumed": {"milliliters": 1250.0}})
                ],
            }
        )
        assert db.query_food_log(tmp_db, "2026-03-15", "2026-03-15") == []

    def test_it_reads_the_rollup_rather_than_the_list(self, tmp_db, sync_food_log):
        """Both types answer either way, and only one of them is a day's total.

        The list returns one point per logged entry, so a client reading it
        stores the last apple of the day as the day's intake.
        """
        _, listing = sync_food_log(
            rollups={
                "nutrition-log": [_rollup_point("nutritionLog", {"energy": {"kcalSum": 1850.5}})]
            }
        )
        assert not listing.called

    def test_a_day_neither_source_reports_stores_nothing(self, tmp_db, sync_food_log):
        sync_food_log(rollups={})
        assert db.query_food_log(tmp_db, "2026-03-01", "2026-04-01") == []
