"""What Google no longer holds for a hand-logged type leaves the cache.

A person deletes food, drink, temperatures, weigh-ins and workouts in the
app, and Google then stops sending them, with nothing saying so. Once a
sync has Google's complete answer for its dates, what this provider wrote
for those dates and the answer no longer holds is removed, so the cache
answers as Google does. Imported rows and rows outside the dates synced
are never touched, and an answer holding a point the sync cannot read
removes nothing, since that point may be one already stored.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync

# Fictional throughout.
_START, _END = date(2026, 3, 10), date(2026, 3, 16)


def _run(tmp_db, handler, lists=None, rollups=None):
    lists, rollups = lists or {}, rollups or {}
    with (
        patch.object(
            google_sync.api,
            "list_google_data_points",
            side_effect=lambda t, s, e, view="list": lists.get(t, []),
        ),
        patch.object(
            google_sync.api, "daily_roll_up", side_effect=lambda t, s, e: rollups.get(t, [])
        ),
        patch.object(google_sync.api, "export_exercise_tcx", return_value="<tcx/>"),
    ):
        count = handler(tmp_db, _START, _END)
    tmp_db.commit()
    return count


def _civil(day, hours=12):
    return {"date": {"year": 2026, "month": 3, "day": day}, "time": {"hours": hours}}


def _entry(field, values, day, name):
    return {
        "name": f"users/1/dataTypes/x/dataPoints/{name}",
        field: {
            "interval": {
                "startTime": f"2026-03-{day:02d}T12:00:00Z",
                "endTime": f"2026-03-{day:02d}T12:00:30Z",
                "civilStartTime": _civil(day),
            },
            **values,
        },
    }


def _sample(field, values, day, name, hours=11):
    return {
        "name": f"users/1/dataTypes/x/dataPoints/{name}",
        field: {
            "sampleTime": {
                "civilTime": _civil(day, hours),
                "physicalTime": f"2026-03-{day:02d}T{hours:02d}:00:00Z",
                "utcOffset": "0s",
            },
            **values,
        },
    }


def _rollup(field, values, day):
    return {"civilStartTime": {"date": {"year": 2026, "month": 3, "day": day}}, field: values}


def _workout(day, name, gps=False):
    return {
        "name": f"users/1/dataTypes/exercise/dataPoints/{name}",
        "exercise": {
            "interval": {
                "startTime": f"2026-03-{day:02d}T09:00:00Z",
                "endTime": f"2026-03-{day:02d}T10:00:00Z",
                "startUtcOffset": "0s",
            },
            "exerciseType": "WALKING",
            "exerciseMetadata": {"hasGps": gps},
        },
    }


class TestFoodAndDrink:
    @pytest.mark.parametrize(
        "handler,data_type,field,values,table",
        [
            (
                google_sync.sync_food_entries,
                "nutrition-log",
                "nutritionLog",
                {"energy": {}},
                "food_entries",
            ),
            (
                google_sync.sync_water_entries,
                "hydration-log",
                "hydrationLog",
                {"amountConsumed": {}},
                "water_entries",
            ),
        ],
        ids=["food", "water"],
    )
    def test_a_day_emptied_in_the_app_leaves_the_cache(
        self, tmp_db, handler, data_type, field, values, table
    ):
        both = [_entry(field, values, 12, "1"), _entry(field, values, 13, "2")]
        _run(tmp_db, handler, {data_type: both})
        _run(tmp_db, handler, {data_type: both[1:]})
        assert [
            r["date"] for r in db.query_day_lists(tmp_db, table, "2026-03-01", "2026-04-01")
        ] == ["2026-03-13"]

    def test_a_days_totals_follow_what_google_still_holds(self, tmp_db):
        water = {"amountConsumed": {"millilitersSum": 250}}
        food = {"energy": {"kcalSum": 300}}
        _run(
            tmp_db,
            google_sync.sync_food_log,
            rollups={
                "hydration-log": [
                    _rollup("hydrationLog", water, 12),
                    _rollup("hydrationLog", water, 13),
                ],
                "nutrition-log": [_rollup("nutritionLog", food, 13)],
            },
        )
        # The 12th's only drink is deleted; the 13th's food is deleted, its drink kept.
        _run(
            tmp_db,
            google_sync.sync_food_log,
            rollups={"hydration-log": [_rollup("hydrationLog", water, 13)]},
        )
        rows = db.query_food_log(tmp_db, "2026-03-01", "2026-04-01")
        assert [(r["date"], r["calories_in"], r["water_ml"]) for r in rows] == [
            ("2026-03-13", None, 250)
        ]

    def test_an_imported_total_is_never_touched(self, tmp_db):
        db.save_food_log(tmp_db, {"date": "2026-03-12", "calories_in": 2000, "water_ml": 500})
        tmp_db.commit()
        _run(tmp_db, google_sync.sync_food_log)
        (row,) = db.query_food_log(tmp_db, "2026-03-01", "2026-04-01")
        assert (row["calories_in"], row["water_ml"]) == (2000, 500)


class TestReadings:
    def test_a_deleted_temperature_leaves_the_cache(self, tmp_db):
        kept = _sample("coreBodyTemperature", {"temperatureCelsius": 37.0, "id": "a"}, 12, "a")
        gone = _sample("coreBodyTemperature", {"temperatureCelsius": 38.0, "id": "b"}, 13, "b")
        _run(tmp_db, google_sync.sync_core_temperature, {"core-body-temperature": [kept, gone]})
        _run(tmp_db, google_sync.sync_core_temperature, {"core-body-temperature": [kept]})
        rows = db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")
        assert [r["temp_celsius"] for r in rows] == [37.0]

    @pytest.mark.parametrize(
        "handler,data_type,field,values,query",
        [
            (
                google_sync.sync_weight_readings,
                "weight",
                "weight",
                {"weightGrams": 70000},
                db.query_weight_readings,
            ),
            (
                google_sync.sync_body_fat_readings,
                "body-fat",
                "bodyFat",
                {"percentage": 20},
                db.query_body_fat_readings,
            ),
        ],
        ids=["weigh-ins", "body-fat"],
    )
    def test_a_deleted_reading_leaves_the_cache(
        self, tmp_db, handler, data_type, field, values, query
    ):
        points = [_sample(field, values, 12, "a"), _sample(field, values, 12, "b", hours=18)]
        _run(tmp_db, handler, {data_type: points})
        _run(tmp_db, handler, {data_type: points[:1]})
        assert [
            r["reading_id"].rsplit("/", 1)[1] for r in query(tmp_db, "2026-03-01", "2026-04-01")
        ] == ["a"]

    def test_a_days_weight_follows_what_google_still_holds(self, tmp_db):
        weigh = _sample("weight", {"weightGrams": 70000}, 12, "w")
        fat = _sample("bodyFat", {"percentage": 20}, 13, "f")
        _run(tmp_db, google_sync.sync_weight, {"weight": [weigh], "body-fat": [fat]})
        _run(tmp_db, google_sync.sync_weight, {"body-fat": [fat]})
        rows = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert [(r["date"], r["weight_kg"], r["fat_pct"]) for r in rows] == [
            ("2026-03-13", None, 20)
        ]

    def test_a_deleted_weigh_in_is_withdrawn_while_the_days_body_fat_stays(self, tmp_db):
        weigh = _sample("weight", {"weightGrams": 70000}, 12, "w")
        fat = _sample("bodyFat", {"percentage": 20}, 12, "f")
        _run(tmp_db, google_sync.sync_weight, {"weight": [weigh], "body-fat": [fat]})
        _run(tmp_db, google_sync.sync_weight, {"body-fat": [fat]})
        (row,) = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert (row["weight_kg"], row["fat_pct"]) == (None, 20)

    def test_an_imported_weight_is_never_touched(self, tmp_db):
        db.save_weight(tmp_db, {"date": "2026-03-12", "weight_kg": 71.0, "bmi": 22.0})
        tmp_db.commit()
        _run(tmp_db, google_sync.sync_weight)
        (row,) = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert (row["weight_kg"], row["bmi"]) == (71.0, 22.0)

    def test_a_day_keeps_an_imported_bmi_beside_a_withdrawn_weight(self, tmp_db):
        """The row is the day's; only what Google wrote into it is withdrawn."""
        db.save_weight(tmp_db, {"date": "2026-03-12", "bmi": 22.0})
        tmp_db.commit()
        _run(
            tmp_db,
            google_sync.sync_weight,
            {"weight": [_sample("weight", {"weightGrams": 70000}, 12, "w")]},
        )
        _run(tmp_db, google_sync.sync_weight)
        (row,) = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert (row["weight_kg"], row["bmi"]) == (None, 22.0)


class TestWorkouts:
    def test_a_deleted_workout_and_its_route_leave_the_cache(self, tmp_db):
        walks = [_workout(12, "a", gps=True), _workout(13, "b", gps=True)]
        _run(tmp_db, google_sync.sync_exercises, {"exercise": walks})
        _run(tmp_db, google_sync.sync_exercise_routes, {"exercise": walks})
        _run(tmp_db, google_sync.sync_exercises, {"exercise": walks[1:]})
        rows = db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")
        assert [r["log_id"].rsplit("/", 1)[1] for r in rows] == ["b"]
        assert db.route_ids(tmp_db) == {walks[1]["name"]}

    def test_an_imported_workout_is_never_touched(self, tmp_db):
        db.save_exercise(tmp_db, "imported-1", {"date": "2026-03-12", "name": "Walk"})
        tmp_db.commit()
        _run(tmp_db, google_sync.sync_exercises)
        assert len(db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")) == 1


class TestTheLimits:
    @pytest.mark.parametrize("day,first,last", [(5, 1, 6), (20, 18, 21)], ids=["before", "after"])
    def test_nothing_outside_the_dates_synced_is_removed(self, tmp_db, day, first, last):
        point = _sample("coreBodyTemperature", {"temperatureCelsius": 37.0, "id": "a"}, day, "a")
        with patch.object(google_sync.api, "list_google_data_points", return_value=[point]):
            google_sync.sync_core_temperature(tmp_db, date(2026, 3, first), date(2026, 3, last))
        tmp_db.commit()
        _run(tmp_db, google_sync.sync_core_temperature)
        assert len(db.query_core_temperature(tmp_db, "2026-03-01", "2026-04-01")) == 1

    _TEMPERATURE = _sample("coreBodyTemperature", {"temperatureCelsius": 37.0, "id": "a"}, 12, "a")
    _WEIGH_IN = _sample("weight", {"weightGrams": 70000}, 12, "a")
    _WATER = {"amountConsumed": {"millilitersSum": 250}}

    @pytest.mark.parametrize(
        "handler,table,stored,unreadable",
        [
            (
                google_sync.sync_core_temperature,
                "core_temperature",
                {"core-body-temperature": [_TEMPERATURE]},
                {"core-body-temperature": [{"coreBodyTemperature": {}}]},
            ),
            (
                google_sync.sync_weight_readings,
                "weight_readings",
                {"weight": [_WEIGH_IN]},
                {"weight": [{"name": _WEIGH_IN["name"], "weight": {}}]},
            ),
            (
                google_sync.sync_weight_readings,
                "weight_readings",
                {"weight": [_WEIGH_IN]},
                {"weight": [{"weight": _WEIGH_IN["weight"]}]},
            ),
            (
                google_sync.sync_weight,
                "weight",
                {"weight": [_WEIGH_IN]},
                {"weight": [_sample("weight", {}, 12, "b")]},
            ),
            (
                google_sync.sync_weight,
                "weight",
                {"weight": [_WEIGH_IN]},
                {"weight": [{"weight": {"weightGrams": 70000}}]},
            ),
            (
                google_sync.sync_exercises,
                "exercises",
                {"exercise": [_workout(12, "a")]},
                {"exercise": [{"exercise": {"interval": {}}}]},
            ),
            (
                google_sync.sync_exercises,
                "exercises",
                {"exercise": [_workout(12, "a")]},
                {"exercise": [{"name": _workout(12, "a")["name"], "exercise": {"interval": {}}}]},
            ),
            (
                google_sync.sync_food_entries,
                "food_entries",
                {"nutrition-log": [_entry("nutritionLog", {"energy": {}}, 12, "a")]},
                {"nutrition-log": [{"nutritionLog": {}}]},
            ),
        ],
        ids=[
            "temperature",
            "weigh-in-undated",
            "weigh-in-unnamed",
            "weight-day-no-grams",
            "weight-undated",
            "workout-unnamed",
            "workout-undated",
            "food",
        ],
    )
    def test_an_answer_with_a_point_it_cannot_read_removes_nothing(
        self, tmp_db, handler, table, stored, unreadable
    ):
        """A point with no name or date may be any stored one, so nothing goes;
        one with a name or date but no value still holds its reading or day."""
        _run(tmp_db, handler, stored)
        _run(tmp_db, handler, unreadable)
        (count,) = tmp_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        assert count == 1

    @pytest.mark.parametrize(
        "source,unreadable",
        [
            ("hydration-log", {"hydrationLog": _WATER}),
            ("hydration-log", _rollup("hydrationLog", {"amountConsumed": {}}, 12)),
            ("nutrition-log", {"nutritionLog": {"energy": {"kcalSum": 300}}}),
        ],
        ids=["water-undated", "water-no-value", "food-undated"],
    )
    def test_a_totals_answer_it_cannot_read_withdraws_nothing(self, tmp_db, source, unreadable):
        _run(
            tmp_db,
            google_sync.sync_food_log,
            rollups={"hydration-log": [_rollup("hydrationLog", self._WATER, 12)]},
        )
        _run(tmp_db, google_sync.sync_food_log, rollups={source: [unreadable]})
        (row,) = db.query_food_log(tmp_db, "2026-03-01", "2026-04-01")
        assert row["water_ml"] == 250

    def test_a_body_fat_reading_with_no_value_still_holds_its_day(self, tmp_db):
        fat = _sample("bodyFat", {"percentage": 20}, 12, "f")
        _run(tmp_db, google_sync.sync_weight, {"body-fat": [fat]})
        _run(tmp_db, google_sync.sync_weight, {"body-fat": [_sample("bodyFat", {}, 12, "f")]})
        (row,) = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert row["fat_pct"] == 20

    def test_an_unreadable_workout_keeps_every_route(self, tmp_db):
        walks = [_workout(12, "a", gps=True), _workout(13, "b", gps=True)]
        _run(tmp_db, google_sync.sync_exercises, {"exercise": walks})
        _run(tmp_db, google_sync.sync_exercise_routes, {"exercise": walks})
        _run(tmp_db, google_sync.sync_exercises, {"exercise": [{"exercise": {}}]})
        assert len(db.route_ids(tmp_db)) == 2

    def test_a_workout_left_to_an_import_is_still_held(self, tmp_db):
        """A day an import later fills is skipped, and Google still has the workout."""
        _run(tmp_db, google_sync.sync_exercises, {"exercise": [_workout(12, "a")]})
        db.save_exercise(tmp_db, "imported-1", {"date": "2026-03-12", "name": "Walk"})
        tmp_db.commit()
        _run(tmp_db, google_sync.sync_exercises, {"exercise": [_workout(12, "a")]})
        assert len(db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")) == 2

    def test_a_workout_dated_before_the_dates_synced_is_never_removed(self, tmp_db):
        with patch.object(
            google_sync.api, "list_google_data_points", return_value=[_workout(9, "a")]
        ):
            google_sync.sync_exercises(tmp_db, date(2026, 3, 1), _END)
        tmp_db.commit()
        _run(tmp_db, google_sync.sync_exercises)
        assert len(db.query_exercises(tmp_db, "2026-03-01", "2026-04-01")) == 1

    def test_each_removal_is_logged_as_a_count(self, tmp_db, caplog):
        water = {"amountConsumed": {"millilitersSum": 250}}
        _run(
            tmp_db,
            google_sync.sync_food_log,
            rollups={"hydration-log": [_rollup("hydrationLog", water, 12)]},
        )
        _run(tmp_db, google_sync.sync_exercises, {"exercise": [_workout(12, "a")]})
        with caplog.at_level("INFO", logger=google_sync.__name__):
            _run(tmp_db, google_sync.sync_food_log)
            _run(tmp_db, google_sync.sync_exercises)
        assert "withdrew from 1 food_log row(s) for 2026-03-10 to 2026-03-16" in caplog.text
        assert "removed 1 exercises row(s)" in caplog.text

    def test_a_workout_dated_a_day_off_its_civil_day_is_still_in_the_answer(self, tmp_db):
        """Google filters by civil start; the request's margin reaches the stored date."""
        asked = []

        def answer(t, s, e, view="list"):
            asked.append((s, e))
            return [_workout(16, "a")]

        with patch.object(google_sync.api, "list_google_data_points", side_effect=answer):
            google_sync.sync_exercises(tmp_db, _START, _END)
        assert asked == [(date(2026, 3, 9), date(2026, 3, 18))]


class TestImportedFigures:
    def test_an_imported_figure_beside_googles_weight_is_never_withdrawn(self, tmp_db):
        db.save_weight(
            tmp_db, {"date": "2026-03-12", "weight_kg": 71.0, "bmi": 22.0, "fat_pct": 19.0}
        )
        tmp_db.commit()
        weigh = _sample("weight", {"weightGrams": 70000}, 12, "w")
        _run(tmp_db, google_sync.sync_weight, {"weight": [weigh]})
        _run(tmp_db, google_sync.sync_weight, {"weight": [weigh]})
        (row,) = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert (row["weight_kg"], row["fat_pct"], row["bmi"]) == (70.0, 19.0, 22.0)

    def test_googles_own_figure_is_withdrawn_beside_an_imported_one(self, tmp_db):
        db.save_weight(tmp_db, {"date": "2026-03-12", "bmi": 22.0, "fat_pct": 19.0})
        tmp_db.commit()
        weigh = _sample("weight", {"weightGrams": 70000}, 12, "w")
        _run(tmp_db, google_sync.sync_weight, {"weight": [weigh]})
        _run(tmp_db, google_sync.sync_weight)
        (row,) = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert (row["weight_kg"], row["fat_pct"], row["bmi"]) == (None, 19.0, 22.0)

    def test_an_import_after_a_withdrawal_is_not_withdrawn_later(self, tmp_db):
        """Once withdrawn, a column is no longer Google's, whoever writes it next."""
        db.save_weight(tmp_db, {"date": "2026-03-12", "bmi": 22.0})
        tmp_db.commit()
        weigh = _sample("weight", {"weightGrams": 70000}, 12, "w")
        _run(tmp_db, google_sync.sync_weight, {"weight": [weigh]})
        _run(tmp_db, google_sync.sync_weight)
        db.save_weight(tmp_db, {"date": "2026-03-12", "weight_kg": 71.0})
        tmp_db.commit()
        _run(tmp_db, google_sync.sync_weight)
        (row,) = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert (row["weight_kg"], row["bmi"]) == (71.0, 22.0)

    def test_the_record_of_googles_columns_stays_out_of_the_reply(self, tmp_db):
        _run(
            tmp_db,
            google_sync.sync_weight,
            {"weight": [_sample("weight", {"weightGrams": 70000}, 12, "w")]},
        )
        (row,) = db.query_weight(tmp_db, "2026-03-01", "2026-04-01")
        assert "google_columns" not in row
