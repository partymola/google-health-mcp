"""Normalising Google data points into the cache's tables.

Every assertion here guards a way of producing a number that looks right.
The API sends integers as JSON strings, sends absence rather than zero, and
reports two of these quantities with a different definition from the one the
same column holds for a row that arrived by import.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync
from tests.conftest import dated_but_empty


def _day(year=2026, month=3, day=15):
    return {"date": {"year": year, "month": month, "day": day}}


def _points(field, *values):
    return [{field: {**_day(day=15 + n), **v}} for n, v in enumerate(values)]


@pytest.fixture
def stored(tmp_db):
    def run(handler, points):
        with patch.object(google_sync.api, "list_google_data_points", return_value=points):
            count = handler(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        return count

    return run


class TestNumbersArriveAsStrings:
    def test_a_string_integer_is_stored_as_a_number(self, tmp_db, stored):
        """`beatsPerMinute` is '73', not 73 - the API serialises int64 as text."""
        stored(
            google_sync.sync_heart_rate,
            _points("dailyRestingHeartRate", {"beatsPerMinute": "73"}),
        )
        row = db.query_heart_rate(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["resting_hr"] == 73
        assert isinstance(row["resting_hr"], int)

    def test_an_unparseable_number_becomes_absent_rather_than_zero(self, tmp_db, stored):
        stored(
            google_sync.sync_heart_rate,
            _points("dailyRestingHeartRate", {"beatsPerMinute": "not-a-number"}),
        )
        assert db.query_heart_rate(tmp_db, "2026-03-15", "2026-03-15") == []

    def test_a_json_true_is_not_a_measurement_of_one(self, tmp_db, stored):
        """`bool` is a subclass of `int`, so `int(True)` is 1 and no conversion
        raises. A resting heart rate of 1 is a plausible-looking wrong number
        of exactly the kind every other rule in this module exists to stop."""
        stored(
            google_sync.sync_heart_rate,
            _points("dailyRestingHeartRate", {"beatsPerMinute": True}),
        )
        assert db.query_heart_rate(tmp_db, "2026-03-15", "2026-03-15") == []


class TestAbsenceIsNotZero:
    def test_a_point_with_no_value_writes_no_row(self, tmp_db, stored):
        """A day off the wrist is not a day of zero, and no row claims it was.

        Whatever the day already holds is what a reader gets: preserving it is
        `TestAQuietDayCostsNothing` below.
        """
        stored(google_sync.sync_breathing_rate, _points("dailyRespiratoryRate", {}))
        assert db.query_breathing_rate(tmp_db, "2026-03-15", "2026-03-15") == []

    def test_a_genuine_zero_is_kept(self, tmp_db, stored):
        stored(
            google_sync.sync_breathing_rate,
            _points("dailyRespiratoryRate", {"breathsPerMinute": 0}),
        )
        assert (
            db.query_breathing_rate(tmp_db, "2026-03-15", "2026-03-15")[0]["breaths_per_min"] == 0
        )


class TestSpo2KeepsItsTwoDefinitionsApart:
    def test_the_confidence_bounds_never_reach_min_and_max(self, tmp_db, stored):
        """Google's bounds are a confidence interval on the average.

        `min`/`max` hold observed nightly extremes. Writing a confidence bound
        into them makes `_trend_spo2` report a change of definition as a
        change in the body.
        """
        stored(
            google_sync.sync_spo2,
            _points(
                "dailyOxygenSaturation",
                {
                    "averagePercentage": 95.7,
                    "lowerBoundPercentage": 92.8,
                    "upperBoundPercentage": 98.2,
                },
            ),
        )
        row = db.query_spo2(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["avg"] == 95.7
        assert row["avg_ci_low"] == 92.8
        assert row["avg_ci_high"] == 98.2
        assert row["min"] is None
        assert row["max"] is None

    def test_an_imported_row_keeps_its_extremes(self, tmp_db, stored):
        """The upsert preserves them, so an imported history survives a sync."""
        db.save_spo2(tmp_db, {"date": "2026-03-15", "avg": 96.0, "min": 91.0, "max": 99.0})
        tmp_db.commit()
        stored(
            google_sync.sync_spo2,
            _points("dailyOxygenSaturation", {"averagePercentage": 95.7}),
        )
        row = db.query_spo2(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["min"] == 91.0
        assert row["max"] == 99.0
        assert row["avg"] == 95.7


class TestSkinTemperatureContinuesItsSeries:
    def test_the_relative_value_is_the_difference(self, tmp_db, stored):
        """The column holds a delta from baseline; Google reports both absolutes.

        One subtraction keeps the column meaning one thing rather than two,
        and both absolutes are stored as well since they are strictly more.
        """
        stored(
            google_sync.sync_skin_temperature,
            _points(
                "dailySleepTemperatureDerivations",
                {"nightlyTemperatureCelsius": 41.5, "baselineTemperatureCelsius": 41.4},
            ),
        )
        row = db.query_skin_temperature(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["nightly_absolute"] == 41.5
        assert row["baseline"] == 41.4
        assert abs(row["nightly_relative"] - 0.1) < 1e-9

    def test_without_a_baseline_the_relative_value_is_absent(self, tmp_db, stored):
        """A baseline is optional, and subtracting nothing is not zero drift."""
        stored(
            google_sync.sync_skin_temperature,
            _points("dailySleepTemperatureDerivations", {"nightlyTemperatureCelsius": 41.5}),
        )
        row = db.query_skin_temperature(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["nightly_absolute"] == 41.5
        assert row["nightly_relative"] is None


class TestTheRowsThemselves:
    def test_the_date_comes_from_the_point_not_the_window(self, tmp_db, stored):
        """Asking for a month and binning everything to its first day is silent."""
        stored(
            google_sync.sync_breathing_rate,
            _points("dailyRespiratoryRate", {"breathsPerMinute": 15}, {"breathsPerMinute": 16}),
        )
        dates = [r["date"] for r in db.query_breathing_rate(tmp_db, "2026-03-01", "2026-04-01")]
        assert dates == ["2026-03-15", "2026-03-16"]

    def test_every_row_records_its_provider(self, tmp_db, stored):
        stored(
            google_sync.sync_hrv,
            _points("dailyHeartRateVariability", {"averageHeartRateVariabilityMilliseconds": 28.4}),
        )
        assert db.query_hrv(tmp_db, "2026-03-15", "2026-03-15")[0]["provider"] == "google"

    def test_hrv_keeps_its_two_measures_apart(self, tmp_db, stored):
        stored(
            google_sync.sync_hrv,
            _points(
                "dailyHeartRateVariability",
                {
                    "averageHeartRateVariabilityMilliseconds": 28.4,
                    "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 30.1,
                },
            ),
        )
        row = db.query_hrv(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["daily_rmssd"] == 28.4
        assert row["deep_rmssd"] == 30.1

    def test_a_point_with_no_date_is_skipped_rather_than_guessed(self, tmp_db, stored):
        count = stored(
            google_sync.sync_breathing_rate,
            [{"dailyRespiratoryRate": {"breathsPerMinute": 15}}],
        )
        assert count == 0
        assert db.query_breathing_rate(tmp_db, "2026-03-01", "2026-04-01") == []

    def test_the_count_is_of_rows_written(self, tmp_db, stored):
        count = stored(
            google_sync.sync_breathing_rate,
            _points("dailyRespiratoryRate", {"breathsPerMinute": 15}, {"breathsPerMinute": 16}),
        )
        assert count == 2


class TestTheWindowTheLoopHandsOver:
    def test_the_last_day_is_included(self, tmp_db):
        """The loop's end date is inclusive, Google's ranges are closed-open.

        Passing it straight through would drop the most recent day on every
        run - invisibly, because that day arrives the next time it is no
        longer the last one.
        """
        seen = {}

        def capture(data_type, start, end):
            seen[data_type] = (start, end)
            return []

        with patch.object(google_sync.api, "list_google_data_points", side_effect=capture):
            google_sync.sync_breathing_rate(tmp_db, date(2026, 3, 1), date(2026, 3, 10))
        assert seen["daily-respiratory-rate"] == (date(2026, 3, 1), date(2026, 3, 11))

    def test_a_single_day_sync_asks_for_that_day(self, tmp_db):
        seen = {}

        def capture(data_type, start, end):
            seen[data_type] = (start, end)
            return []

        with patch.object(google_sync.api, "list_google_data_points", side_effect=capture):
            google_sync.sync_breathing_rate(tmp_db, date(2026, 3, 10), date(2026, 3, 10))
        assert seen["daily-respiratory-rate"] == (date(2026, 3, 10), date(2026, 3, 11))


class TestAQuietDayCostsNothing:
    """Google reporting a day without a value must not withdraw the stored one.

    This is the destruction the whole upsert exists to prevent, arriving from
    the other side: not a writer naming fewer columns, but a writer naming a
    column with nothing in it.
    """

    def _seed(self, tmp_db):
        db.save_heart_rate(tmp_db, "2026-03-15", 52, [{"name": "Peak", "minutes": 12}])
        db.save_spo2(tmp_db, {"date": "2026-03-15", "avg": 96.4, "min": 91.0, "max": 99.0})
        db.save_hrv(tmp_db, {"date": "2026-03-15", "daily_rmssd": 30.0, "deep_rmssd": 33.0})
        db.save_breathing_rate(tmp_db, {"date": "2026-03-15", "breaths_per_min": 14.2})
        db.save_skin_temperature(tmp_db, {"date": "2026-03-15", "nightly_relative": 0.9})
        tmp_db.commit()

    @pytest.mark.parametrize(
        "handler,field,table,column,kept",
        [
            (google_sync.sync_spo2, "dailyOxygenSaturation", "spo2", "avg", 96.4),
            (google_sync.sync_hrv, "dailyHeartRateVariability", "hrv", "deep_rmssd", 33.0),
            (
                google_sync.sync_breathing_rate,
                "dailyRespiratoryRate",
                "breathing_rate",
                "breaths_per_min",
                14.2,
            ),
            (
                google_sync.sync_skin_temperature,
                "dailySleepTemperatureDerivations",
                "skin_temperature",
                "nightly_relative",
                0.9,
            ),
        ],
    )
    def test_a_day_with_no_value_leaves_the_stored_one(
        self, tmp_db, stored, handler, field, table, column, kept
    ):
        self._seed(tmp_db)
        stored(handler, [{field: {"date": {"year": 2026, "month": 3, "day": 15}}}])
        rows = getattr(db, f"query_{table}")(tmp_db, "2026-03-15", "2026-03-15")
        assert rows[0][column] == kept

    def test_resting_heart_rate_never_clears_the_stored_zones(self, tmp_db, stored):
        """save_heart_rate always names `zones`, so this writer must not use it."""
        self._seed(tmp_db)
        stored(
            google_sync.sync_heart_rate,
            [
                {
                    "dailyRestingHeartRate": {
                        "date": {"year": 2026, "month": 3, "day": 15},
                        "beatsPerMinute": "60",
                    }
                }
            ],
        )
        row = db.query_heart_rate(tmp_db, "2026-03-15", "2026-03-15")[0]
        assert row["resting_hr"] == 60
        assert row["zones"] == [{"name": "Peak", "minutes": 12}]
        assert row["provider"] == "google"


class TestNoWriterStampsADayItDidNotMeasure:
    """The class the azm defect belonged to, rather than the instance.

    A row carrying nothing but its key and `provider` stores no measurement,
    is returned by the query tool as an entry, counts toward the period counts
    in compare mode, and takes the day away from whichever writer put a value
    in it. `_upsert`'s own "nothing to store" guard does not catch it, because
    `provider` is a column like any other.
    """

    def test_no_handler_writes_a_row_that_is_only_a_key_and_a_provider(self, tmp_db, monkeypatch):
        """Driven over every handler at once, so a new writer with that shape
        fails without anyone remembering to write it a test."""
        written: list[tuple[str, dict]] = []
        monkeypatch.setattr(db, "_upsert", lambda conn, table, row: written.append((table, row)))
        points = [dated_but_empty(t.field) for t in google_sync.api.GOOGLE_TYPES.values()]

        for name, handler in google_sync.GOOGLE_SYNC_HANDLERS.items():
            written.clear()
            with (
                patch.object(google_sync.api, "list_google_data_points", return_value=points),
                patch.object(google_sync.api, "daily_roll_up", return_value=points),
            ):
                handler(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
            for table, row in written:
                # .get, not [table]: core_temperature is outside _UPSERT_KEYS,
                # and a writer routed through the upsert to fill its provider
                # should fail this assertion rather than raise a KeyError.
                assert set(row) - {"provider"} - set(db._UPSERT_KEYS.get(table, ())), (
                    f"{name} wrote a row into {table} carrying nothing but its key and provider"
                )
