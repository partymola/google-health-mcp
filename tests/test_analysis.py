"""Tests for the trend analysis logic."""

from datetime import date

import pytest

from google_health_mcp import db
from google_health_mcp.tools.analysis_tools import (
    _COMPARE_QUERY_FNS,
    _TREND_FNS,
    _avg,
    _compare_periods,
    _get_period_key,
    _parse_compare_range,
    _trend_activity,
    _trend_azm,
    _trend_breathing_rate,
    _trend_cardio_fitness,
    _trend_core_temperature,
    _trend_exercises,
    _trend_food_log,
    _trend_heart_rate,
    _trend_hrv,
    _trend_skin_temperature,
    _trend_sleep,
    _trend_spo2,
    _trend_weight,
)


class TestPeriodKey:
    """Test date-to-period bucketing."""

    def test_monthly(self):
        assert _get_period_key("2026-03-15", "monthly") == "2026-03"
        assert _get_period_key("2026-12-01", "monthly") == "2026-12"

    def test_weekly(self):
        key = _get_period_key("2026-03-15", "weekly")
        # 2026-03-15 is a Sunday, ISO week 11
        assert key.startswith("2026-W")

    def test_quarterly(self):
        assert _get_period_key("2026-01-15", "quarterly") == "2026-Q1"
        assert _get_period_key("2026-04-01", "quarterly") == "2026-Q2"
        assert _get_period_key("2026-07-31", "quarterly") == "2026-Q3"
        assert _get_period_key("2026-12-31", "quarterly") == "2026-Q4"


class TestAvg:
    """Test the averaging helper."""

    def test_normal(self):
        assert _avg([10, 20, 30]) == 20.0

    def test_empty(self):
        assert _avg([]) is None

    def test_single(self):
        assert _avg([42]) == 42.0

    def test_rounds_to_one_decimal(self):
        assert _avg([1, 2, 3]) == 2.0
        assert _avg([10, 11]) == 10.5


class TestTrendHeartRate:
    def test_basic(self, populated_db):
        result = _trend_heart_rate(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert "periods" in result
        assert result["data_type"] == "heart_rate"
        assert len(result["periods"]) == 1
        p = result["periods"][0]
        assert p["period"] == "2026-03"
        assert p["days"] == 5
        assert p["avg_resting_hr"] == 62.0  # avg of 60,61,62,63,64

    def test_empty(self, tmp_db):
        result = _trend_heart_rate(tmp_db, "2026-01-01", "2026-01-31", "monthly")
        assert "message" in result


class TestTrendActivity:
    def test_basic(self, populated_db):
        result = _trend_activity(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert result["data_type"] == "activity"
        p = result["periods"][0]
        assert p["days"] == 5
        # steps: 8000, 8500, 9000, 9500, 10000 -> avg 9000.0
        assert p["avg_steps"] == 9000.0
        # distances: 5.0, 5.5, 6.0, 6.5, 7.0 -> total 30.0
        assert p["total_distance_km"] == 30.0

    def test_empty(self, tmp_db):
        result = _trend_activity(tmp_db, "2026-01-01", "2026-01-31", "monthly")
        assert "message" in result


class TestTrendExercises:
    def test_basic(self, populated_db):
        result = _trend_exercises(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert result["data_type"] == "exercises"
        p = result["periods"][0]
        assert p["sessions"] == 3
        assert p["total_calories"] == 750  # 200 + 300 + 250

    def test_empty(self, tmp_db):
        result = _trend_exercises(tmp_db, "2026-01-01", "2026-01-31", "monthly")
        assert "message" in result


class TestTrendSleep:
    def test_basic(self, populated_db):
        result = _trend_sleep(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert result["data_type"] == "sleep"
        p = result["periods"][0]
        assert p["nights"] == 5
        assert "h" in p["avg_total_sleep"]  # formatted as Xh Ym

    def test_empty(self, tmp_db):
        result = _trend_sleep(tmp_db, "2026-01-01", "2026-01-31", "monthly")
        assert "message" in result


class TestTrendWeight:
    def test_basic(self, populated_db):
        result = _trend_weight(populated_db, "2026-03-10", "2026-03-16", "monthly")
        assert result["data_type"] == "weight"
        p = result["periods"][0]
        assert p["count"] == 3
        # weights: 80.0, 79.5, 79.0 -> avg 79.5
        assert p["avg_weight_kg"] == 79.5

    def test_empty(self, tmp_db):
        result = _trend_weight(tmp_db, "2026-01-01", "2026-01-31", "monthly")
        assert "message" in result


class TestTrendSpo2:
    def test_basic(self, populated_db):
        result = _trend_spo2(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert result["data_type"] == "spo2"
        p = result["periods"][0]
        assert p["nights"] == 5
        # avg: 96.0, 96.2, 96.4, 96.6, 96.8 -> avg 96.4
        assert p["avg_spo2"] == 96.4
        # min values: 93.0, 93.3, 93.6, 93.9, 94.2 -> min of mins = 93.0
        assert p["min_spo2"] == 93.0

    def test_empty(self, tmp_db):
        result = _trend_spo2(tmp_db, "2026-01-01", "2026-01-31", "monthly")
        assert "message" in result


class TestTheTwoSpo2Definitions:
    """A night's bounds mean one of two different things, and only one is an extreme.

    The 0.x era stored the observed nightly minimum and maximum; Google
    reports a confidence interval on the average. Averaged into one series
    the switchover reads as a change in the person rather than in the
    provider, so each period says how many nights of each it holds.
    """

    def test_a_period_of_confidence_intervals_reports_them(self, tmp_db):
        for i in range(2):
            db.save_spo2(
                tmp_db,
                {
                    "date": f"2026-03-{20 + i:02d}",
                    "avg": 96.0 + i,
                    "avg_ci_low": 94.0 + i,
                    "avg_ci_high": 98.0 + i,
                },
            )
        tmp_db.commit()

        p = _trend_spo2(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["nights_confidence_interval"] == 2
        assert p["avg_nightly_ci_low"] == 94.5
        assert p["avg_nightly_ci_high"] == 98.5

    def test_a_confidence_interval_is_never_reported_as_an_extreme(self, tmp_db):
        db.save_spo2(
            tmp_db,
            {"date": "2026-03-20", "avg": 96.0, "avg_ci_low": 94.0, "avg_ci_high": 98.0},
        )
        tmp_db.commit()

        p = _trend_spo2(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["min_spo2"] is None
        assert p["max_spo2"] is None
        assert p["nights_observed_extremes"] == 0

    def test_nights_carrying_one_end_of_a_pair_are_all_counted(self, tmp_db):
        """Counting one column reports the other's value against a short count.

        The missing end varies by night here - two with a minimum, one with a
        maximum - so the longer of the two lists is 2 where the answer is 3.
        """
        db.save_spo2(tmp_db, {"date": "2026-03-20", "avg": 96.0, "min": 93.0})
        db.save_spo2(tmp_db, {"date": "2026-03-21", "avg": 96.0, "min": 94.0})
        db.save_spo2(tmp_db, {"date": "2026-03-22", "avg": 96.0, "max": 99.0})
        tmp_db.commit()

        p = _trend_spo2(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["max_spo2"] == 99.0
        assert p["min_spo2"] == 93.0
        assert p["nights_observed_extremes"] == 3

    def test_a_period_holding_both_keeps_them_apart(self, populated_db):
        """The switchover month: five nights of extremes, then two of bounds."""
        for i in range(2):
            db.save_spo2(
                populated_db,
                {
                    "date": f"2026-03-{20 + i:02d}",
                    "avg": 95.0,
                    "avg_ci_low": 90.0,
                    "avg_ci_high": 99.5,
                },
            )
        populated_db.commit()

        p = _trend_spo2(populated_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["nights_observed_extremes"] == 5
        assert p["nights_confidence_interval"] == 2
        # The fixture's observed minimum, not the lower bound of 90.0.
        assert p["min_spo2"] == 93.0
        assert p["avg_nightly_ci_low"] == 90.0


class TestTrendHrv:
    def test_basic(self, populated_db):
        result = _trend_hrv(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert result["data_type"] == "hrv"
        p = result["periods"][0]
        assert p["nights"] == 5
        # daily_rmssd: 35.0, 37.0, 39.0, 41.0, 43.0 -> avg 39.0
        assert p["avg_daily_rmssd"] == 39.0
        # deep_rmssd: 40.0, 42.5, 45.0, 47.5, 50.0 -> avg 45.0
        assert p["avg_deep_rmssd"] == 45.0

    def test_empty(self, tmp_db):
        result = _trend_hrv(tmp_db, "2026-01-01", "2026-01-31", "monthly")
        assert "message" in result


class TestTrendCoreTemperature:
    def test_basic(self, populated_db):
        result = _trend_core_temperature(populated_db, "2026-03-10", "2026-03-12", "monthly")
        assert result["data_type"] == "core_temperature"
        p = result["periods"][0]
        # 4 manual readings across the window (two on 2026-03-11)
        assert p["readings"] == 4
        # values: 36.6, 37.8, 38.4, 37.1 -> avg 37.5, min 36.6, max 38.4
        assert p["avg_temp_celsius"] == 37.5
        assert p["min_temp_celsius"] == 36.6
        assert p["max_temp_celsius"] == 38.4
        # only 38.4 is >= 38 C
        assert p["readings_ge_38c"] == 1

    def test_empty(self, tmp_db):
        result = _trend_core_temperature(tmp_db, "2026-01-01", "2026-01-31", "monthly")
        assert "message" in result


class TestTrendAzm:
    def test_basic(self, populated_db):
        result = _trend_azm(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert result["data_type"] == "azm"
        p = result["periods"][0]
        assert p["days"] == 5
        # total: 30, 35, 40, 45, 50 -> avg 40.0, sum 200
        assert p["avg_total_azm"] == 40.0
        assert p["total_azm"] == 200
        # peak: 0, 1, 2, 3, 4 -> avg 2.0, and the zero counts as a reading
        assert p["avg_peak_minutes"] == 2.0
        assert p["avg_fat_burn_minutes"] == 24.0

    def test_empty(self, tmp_db):
        assert "message" in _trend_azm(tmp_db, "2026-01-01", "2026-01-31", "monthly")


class TestTrendBreathingRate:
    def test_basic(self, populated_db):
        result = _trend_breathing_rate(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert result["data_type"] == "breathing_rate"
        p = result["periods"][0]
        assert p["nights"] == 5
        # 14.0, 14.2, 14.4, 14.6, 14.8
        assert p["avg_breaths_per_min"] == 14.4
        assert p["min_breaths_per_min"] == 14.0
        assert p["max_breaths_per_min"] == 14.8

    def test_empty(self, tmp_db):
        assert "message" in _trend_breathing_rate(tmp_db, "2026-01-01", "2026-01-31", "monthly")


class TestTrendSkinTemperature:
    def test_basic(self, populated_db):
        result = _trend_skin_temperature(populated_db, "2026-03-10", "2026-03-14", "monthly")
        assert result["data_type"] == "skin_temperature"
        p = result["periods"][0]
        assert p["nights"] == 5
        # -0.2, -0.1, 0.0, 0.1, 0.2 - a deviation from baseline, so it goes both ways
        assert p["avg_nightly_relative"] == 0.0
        assert p["min_nightly_relative"] == pytest.approx(-0.2)
        assert p["max_nightly_relative"] == pytest.approx(0.2)

    def test_a_negative_night_is_not_dropped(self, tmp_db):
        """Below baseline is a reading, and a falsiness check would lose it.

        Asymmetric on purpose: the populated fixture's nights sum to zero, so
        a sum reported as an average would read correct there.
        """
        for day, value in (("2026-03-20", -0.4), ("2026-03-21", 0.0), ("2026-03-22", 0.7)):
            db.save_skin_temperature(tmp_db, {"date": day, "nightly_relative": value})
        tmp_db.commit()

        p = _trend_skin_temperature(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["nights"] == 3
        assert p["avg_nightly_relative"] == pytest.approx(0.1)
        assert p["min_nightly_relative"] == pytest.approx(-0.4)

    def test_empty(self, tmp_db):
        assert "message" in _trend_skin_temperature(tmp_db, "2026-01-01", "2026-01-31", "monthly")


class TestTrendFoodLog:
    def test_basic(self, populated_db):
        result = _trend_food_log(populated_db, "2026-03-10", "2026-03-13", "monthly")
        assert result["data_type"] == "food_log"
        p = result["periods"][0]
        assert p["days_logged"] == 4
        # calories: 2000, 2050, 2100, 2150 -> avg 2075.0
        assert p["avg_calories_in"] == 2075.0
        # water: 1500, 1600, 1700, 1800 -> avg 1650.0
        assert p["avg_water_ml"] == 1650.0

    def test_a_day_of_water_alone_still_counts(self, tmp_db):
        """Water without food is the common shape, and counting only meals hides it."""
        db.save_food_log(tmp_db, {"date": "2026-03-20", "water_ml": 900.0})
        tmp_db.commit()

        p = _trend_food_log(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["days_logged"] == 1
        assert p["avg_calories_in"] is None
        assert p["avg_water_ml"] == 900.0

    def test_a_month_of_both_counts_every_day(self, tmp_db):
        """The mixed month is the real one, and taking the longer list undercounts it."""
        db.save_food_log(tmp_db, {"date": "2026-03-20", "calories_in": 2000})
        for i in range(3):
            db.save_food_log(tmp_db, {"date": f"2026-03-{21 + i:02d}", "water_ml": 900.0})
        tmp_db.commit()

        p = _trend_food_log(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["days_logged"] == 4

    def test_empty(self, tmp_db):
        assert "message" in _trend_food_log(tmp_db, "2026-01-01", "2026-01-31", "monthly")


class TestTheTwoCardioFitnessDefinitions:
    """One provider reports a band, the other a single number.

    Collapsing the band to a point loses a real 4-wide range, and a series
    that averages the two ends only ends silently the day the single value
    starts arriving - so both are carried, each with its own count.
    """

    def test_a_period_of_single_values_reports_them(self, tmp_db):
        for i in range(2):
            db.save_cardio_fitness(tmp_db, {"date": f"2026-03-{20 + i:02d}", "vo2_max": 40.0 + i})
        tmp_db.commit()

        p = _trend_cardio_fitness(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["readings_single_value"] == 2
        assert p["avg_vo2_max"] == 40.5

    def test_a_single_value_never_becomes_a_band(self, tmp_db):
        db.save_cardio_fitness(tmp_db, {"date": "2026-03-20", "vo2_max": 40.0})
        tmp_db.commit()

        p = _trend_cardio_fitness(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["avg_vo2_max_low"] is None
        assert p["avg_vo2_max_high"] is None
        assert p["readings_range"] == 0

    def test_a_period_holding_both_keeps_them_apart(self, populated_db):
        db.save_cardio_fitness(populated_db, {"date": "2026-03-20", "vo2_max": 40.0})
        populated_db.commit()

        p = _trend_cardio_fitness(populated_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["readings_range"] == 3
        assert p["readings_single_value"] == 1
        assert p["readings"] == 4
        # The fixture's three bands, unaffected by the single value beside them.
        assert p["avg_vo2_max_low"] == 39.0
        assert p["avg_vo2_max_high"] == 43.0
        assert p["avg_vo2_max"] == 40.0

    def test_readings_carrying_one_end_of_the_band_are_all_counted(self, tmp_db):
        db.save_cardio_fitness(tmp_db, {"date": "2026-03-20", "vo2_max_low": 38.0})
        db.save_cardio_fitness(tmp_db, {"date": "2026-03-21", "vo2_max_low": 39.0})
        db.save_cardio_fitness(tmp_db, {"date": "2026-03-22", "vo2_max_high": 43.0})
        tmp_db.commit()

        p = _trend_cardio_fitness(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["readings_range"] == 3
        assert p["readings"] == 3

    def test_a_reading_carrying_both_is_counted_once(self, tmp_db):
        """`readings` is rows, not values - a row holding both is one reading."""
        db.save_cardio_fitness(
            tmp_db,
            {"date": "2026-03-20", "vo2_max_low": 38.0, "vo2_max_high": 42.0, "vo2_max": 40.0},
        )
        tmp_db.commit()

        p = _trend_cardio_fitness(tmp_db, "2026-03-01", "2026-03-31", "monthly")["periods"][0]
        assert p["readings"] == 1
        assert p["readings_range"] == 1
        assert p["readings_single_value"] == 1

    def test_empty(self, tmp_db):
        assert "message" in _trend_cardio_fitness(tmp_db, "2026-01-01", "2026-01-31", "monthly")


class TestParseCompareRange:
    def test_last_nd(self):
        result = _parse_compare_range("last_30d")
        assert result is not None
        start, end = result
        assert end == date.today()
        assert (end - start).days == 29

    def test_previous_nd(self):
        result = _parse_compare_range("previous_30d")
        assert result is not None
        start, end = result
        assert (end - start).days == 29

    def test_last_and_previous_nd_are_adjacent_non_overlapping(self):
        last = _parse_compare_range("last_14d")
        prev = _parse_compare_range("previous_14d")
        assert last is not None and prev is not None
        assert (last[1] - last[0]).days == 13
        assert (prev[1] - prev[0]).days == 13
        assert (last[0] - prev[1]).days == 1

    def test_month(self):
        result = _parse_compare_range("2026-03")
        assert result is not None
        start, end = result
        assert start == date(2026, 3, 1)
        assert end == date(2026, 3, 31)

    def test_quarter(self):
        result = _parse_compare_range("2026-Q1")
        assert result is not None
        start, end = result
        assert start == date(2026, 1, 1)
        assert end == date(2026, 3, 31)

    def test_quarter_q4(self):
        result = _parse_compare_range("2026-Q4")
        start, end = result
        assert start == date(2026, 10, 1)
        assert end == date(2026, 12, 31)

    def test_invalid(self):
        assert _parse_compare_range("garbage") is None
        assert _parse_compare_range("2026") is None


class TestComparePeriods:
    def test_valid_compare(self, populated_db):
        result = _compare_periods(populated_db, "activity", "2026-03 vs 2026-02")
        assert "period_1" in result
        assert "period_2" in result
        assert result["data_type"] == "activity"

    def test_invalid_format(self, populated_db):
        result = _compare_periods(populated_db, "activity", "just one period")
        assert "error" in result

    def test_invalid_data_type(self, populated_db):
        result = _compare_periods(populated_db, "invalid_type", "2026-03 vs 2026-02")
        assert "error" in result

    def test_the_two_dispatches_offer_the_same_types(self):
        """One message offers both, so a type missing from either is refused as it is
        recommended - and a dropped entry is otherwise silent."""
        assert set(_COMPARE_QUERY_FNS) == set(_TREND_FNS)
        # And each key reaches its own table: a mis-wired entry summarises the
        # wrong one silently, which equal key sets say nothing about.
        assert all(fn.__name__ == f"query_{name}" for name, fn in _COMPARE_QUERY_FNS.items())

    def test_a_day_of_nothing_is_still_a_day(self, tmp_db):
        """A rest day is 0 steps, and dropping it while counting the row inflates the average."""
        for day, steps in (("2026-03-10", 10000), ("2026-03-11", 0), ("2026-03-12", 2000)):
            db.save_activity(tmp_db, {"date": day, "steps": steps})
        tmp_db.commit()

        result = _compare_periods(tmp_db, "activity", "2026-03 vs 2026-02")
        assert result["period_1"]["count"] == 3
        assert result["period_1"]["avg_steps"] == 4000.0

    def test_the_remedy_offers_only_types_that_can_be_analysed(self, populated_db):
        """Not every cached type has a daily series: an ECG reading is an episode.

        The list came from the cache's own types, so it named two the
        dispatch refuses - and the message is the whole of what the caller
        has to go on.
        """
        offered = _compare_periods(populated_db, "ecg", "2026-03 vs 2026-02")["error"]
        offered = offered.split("Use: ")[1]
        for name in offered.rstrip(".").split(", "):
            assert name in _TREND_FNS, f"{name} is offered and cannot be analysed"

    def test_compare_heart_rate(self, populated_db):
        result = _compare_periods(populated_db, "heart_rate", "2026-03 vs 2026-02")
        assert result["period_1"]["count"] > 0
        assert result["period_2"]["count"] == 0

    def test_compare_exercises(self, populated_db):
        result = _compare_periods(populated_db, "exercises", "2026-03 vs 2026-02")
        assert result["period_1"]["count"] == 3

    def test_compare_spo2(self, populated_db):
        result = _compare_periods(populated_db, "spo2", "2026-03 vs 2026-02")
        assert result["period_1"]["count"] == 5

    def test_compare_cardio_fitness_sees_a_single_value(self, populated_db):
        """Compare mode reads the same columns the trend does, or it stops at the switchover."""
        db.save_cardio_fitness(populated_db, {"date": "2026-03-20", "vo2_max": 40.0})
        populated_db.commit()

        result = _compare_periods(populated_db, "cardio_fitness", "2026-03 vs 2026-02")
        assert result["period_1"]["avg_vo2_max"] == 40.0
        assert result["period_1"]["avg_vo2_max_low"] == 39.0

    def test_compare_core_temperature(self, populated_db):
        result = _compare_periods(populated_db, "core_temperature", "2026-03 vs 2026-02")
        assert result["period_1"]["count"] == 4
        assert result["period_1"]["max_temp_celsius"] == 38.4
        assert result["period_2"]["count"] == 0
