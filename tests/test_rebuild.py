"""A rebuild fetches a date range again and reports every row that changes.

The dates are synced again over what is held, and a row Google sent before
and no longer holds is removed, so the cache ends holding what Google holds
now while an imported value beside Google's stays. The report lists what
went and what changed, and without `apply` the work runs on a copy, so
nothing in the cache moves: a person or a model reads the list and decides.
"""

import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest

from google_health_mcp import api, db
from google_health_mcp.tools import google_sync, sync_tools

# Fictional throughout.


def _resting(day, bpm):
    return {
        "dailyRestingHeartRate": {
            "date": {"year": 2026, "month": 3, "day": day},
            "beatsPerMinute": bpm,
        }
    }


@pytest.fixture
def cache(tmp_path):
    path = tmp_path / "cache.db"
    with patch.object(db, "DB_PATH", path):
        conn = db.get_db(path)
        for day, bpm in ((12, 60), (13, 61)):
            db.save_heart_rate_row(
                conn, {"date": f"2026-03-{day}", "resting_hr": bpm, "provider": "google"}
            )
        db.save_heart_rate_row(conn, {"date": "2026-03-14", "resting_hr": 59})  # imported
        db.save_heart_rate_row(conn, {"date": "2026-03-20", "resting_hr": 58, "provider": "google"})
        conn.commit()
        conn.close()
        yield path


def _rebuild(points, apply, types=("heart_rate",), failing=False, until="2026-03-16"):
    def answer(data_type, start, end, view="list"):
        if failing:
            raise api.HealthAPIError("API error 500 for daily-resting-heart-rate list")
        return points

    with (
        patch.object(google_sync.api, "list_google_data_points", side_effect=answer),
        patch.object(sync_tools.auth, "granted_scopes", return_value=None),
        patch.object(sync_tools.auth, "refresh_google_token"),
        patch.object(sync_tools.config, "OFFLINE_MODE", False),
    ):
        return sync_tools.rebuild(list(types), "2026-03-10", until, apply=apply)


def _stored(path):
    conn = db.get_db(path)
    try:
        return {
            r["date"]: r["resting_hr"]
            for r in db.query_heart_rate(conn, "2026-03-01", "2026-04-01")
        }
    finally:
        conn.close()


_BEFORE = {"2026-03-12": 60, "2026-03-13": 61, "2026-03-14": 59, "2026-03-20": 58}


def test_a_preview_reports_and_changes_nothing(cache):
    report = _rebuild([_resting(13, 63)], apply=False)
    assert report["applied"] is False
    result = report["types"]["heart_rate"]
    assert [r["date"] for r in result["removed"]] == ["2026-03-12"]
    assert result["removed"][0]["resting_hr"] == 60
    assert result["changed"] == [
        {"date": "2026-03-13", "columns": {"resting_hr": {"before": 61, "after": 63}}}
    ]
    assert (result["added"], result["unchanged"]) == (0, 0)
    assert _stored(cache) == _BEFORE


def test_applied_the_cache_holds_what_google_holds(cache):
    report = _rebuild([_resting(13, 63)], apply=True)
    assert report["applied"] is True
    assert _stored(cache) == {"2026-03-13": 63, "2026-03-14": 59, "2026-03-20": 58}


def test_an_imported_row_and_a_row_outside_the_dates_are_never_touched(cache):
    report = _rebuild([_resting(15, 57)], apply=True)
    assert [r["date"] for r in report["types"]["heart_rate"]["removed"]] == [
        "2026-03-12",
        "2026-03-13",
    ]
    assert _stored(cache) == {"2026-03-14": 59, "2026-03-15": 57, "2026-03-20": 58}


def test_an_answer_holding_nothing_it_could_store_removes_nothing(cache):
    # A row long before the dates must not make the fetch look as if it wrote something.
    _write(cache, "heart_rate", {"date": "2026-03-01", "resting_hr": 56, "provider": "google"})
    report = _rebuild([], apply=True)
    heart = report["types"]["heart_rate"]
    assert heart["removed"] == []
    assert "nothing was removed" in heart["note"]
    assert _stored(cache) == {**_BEFORE, "2026-03-01": 56}


def test_a_type_that_held_nothing_carries_no_note(cache):
    report = _rebuild_with(["ecg"])
    assert "note" not in report["types"]["ecg"]


def test_a_write_on_the_day_either_side_alone_does_not_count_as_an_answer(cache):
    report = _rebuild([_resting(9, 55)], apply=True)
    heart = report["types"]["heart_rate"]
    assert "nothing was removed" in heart["note"]
    assert {d: v for d, v in _stored(cache).items() if d != "2026-03-09"} == _BEFORE


def test_a_row_another_process_writes_meanwhile_is_kept(cache):
    def handler(conn, start, end):
        other = db.get_db(cache)
        db.save_heart_rate_row(
            other, {"date": "2026-03-15", "resting_hr": 57, "provider": "google"}
        )
        other.commit()
        other.close()
        db.save_heart_rate_row(conn, {"date": "2026-03-13", "resting_hr": 61, "provider": "google"})
        conn.commit()
        return 1

    with (
        patch.object(sync_tools.auth, "granted_scopes", return_value=None),
        patch.object(sync_tools.auth, "refresh_google_token"),
    ):
        sync_tools.rebuild(
            ["heart_rate"], "2026-03-10", "2026-03-16", apply=True, handlers={"heart_rate": handler}
        )
    assert _stored(cache) == {
        "2026-03-13": 61,
        "2026-03-14": 59,
        "2026-03-15": 57,
        "2026-03-20": 58,
    }


def test_a_reading_on_the_first_day_that_google_files_under_the_day_before_is_kept(cache):
    """An ECG is filtered on its UTC start and dated by its local one."""
    name = "users/me/dataTypes/electrocardiogram/dataPoints/1"
    _write(cache, "ecg", {"reading_id": name, "date": "2026-03-10", "provider": "google"})
    reading = {
        "name": name,
        "electrocardiogram": {
            "interval": {"startTime": "2026-03-09T23:30:00Z", "startUtcOffset": "3600s"}
        },
    }
    later = {
        "name": name.replace("/1", "/2"),
        "electrocardiogram": {
            "interval": {"startTime": "2026-03-12T10:00:00Z", "startUtcOffset": "3600s"}
        },
    }

    def listed(data_type, start, end, view="list"):
        return [later] + ([reading] if start <= date(2026, 3, 9) < end else [])

    first, second, third, fourth, fifth = _sync_answers()
    with first, second, third, fourth, fifth:
        with patch.object(google_sync.api, "list_google_data_points", side_effect=listed):
            report = sync_tools.rebuild(["ecg"], "2026-03-10", "2026-03-16", apply=True)
    assert report["types"]["ecg"]["removed"] == []
    assert name in [r["reading_id"] for r in _rows(cache, "ecg")]


@pytest.mark.parametrize("day", ["2026-03-05", "2026-03-09", "2026-03-17"])
def test_a_row_outside_the_dates_is_never_removed(cache, day):
    """Including the day either side, which the fetch reaches and cannot judge."""
    _write(cache, "heart_rate", {"date": day, "resting_hr": 57, "provider": "google"})
    _rebuild([_resting(13, 61)], apply=True)
    stored = _stored(cache)
    assert stored[day] == 57
    # And it does not stop the row inside the dates that Google dropped from going.
    assert "2026-03-12" not in stored


def test_rows_compacted_after_the_fetch_are_still_judged(cache):
    """Compaction rewrites text an old release stored, which must not count as a fetch."""
    conn = db.get_db(cache)
    conn.execute(
        "INSERT INTO sleep_sessions (session_id, date, record, provider) VALUES (?, ?, ?, ?)",
        ("old", "2026-03-12", "{}", "google"),
    )
    conn.commit()
    conn.close()
    session = {
        "name": "new",
        "sleep": {"interval": {"endTime": "2026-03-13T07:00:00Z", "endUtcOffset": "0s"}},
    }
    report = _rebuild_with(["sleep_sessions"], lists={"sleep": [session]})
    assert [r["session_id"] for r in report["types"]["sleep_sessions"]["removed"]] == ["old"]
    assert [r["session_id"] for r in _rows(cache, "sleep_sessions")] == ["new"]


def test_a_row_compaction_packs_is_not_reported_as_changed(cache):
    conn = db.get_db(cache)
    conn.execute(
        "INSERT INTO sleep_sessions (session_id, date, record, provider) VALUES (?, ?, ?, ?)",
        ("old", "2026-03-09", "{}", "google"),
    )
    conn.commit()
    conn.close()
    session = {
        "name": "new",
        "sleep": {"interval": {"endTime": "2026-03-13T07:00:00Z", "endUtcOffset": "0s"}},
    }
    report = _rebuild_with(["sleep_sessions"], lists={"sleep": [session]})
    assert report["types"]["sleep_sessions"]["changed"] == []


def test_a_row_google_sends_unchanged_is_counted_not_listed(cache):
    result = _rebuild([_resting(12, 60), _resting(13, 61), _resting(15, 57)], apply=True)
    heart = result["types"]["heart_rate"]
    assert (heart["removed"], heart["changed"], heart["added"], heart["unchanged"]) == (
        [],
        [],
        1,
        2,
    )


def test_a_failed_fetch_removes_nothing(cache):
    report = _rebuild([], apply=True, failing=True)
    assert report["types"]["heart_rate"]["status"] == "error"
    assert _stored(cache) == _BEFORE


def test_a_failed_fetch_keeps_what_it_had_already_written(cache):
    """A row Google sent before the failure is newer than the one set aside."""

    def handler(conn, start, end):
        db.save_heart_rate_row(conn, {"date": "2026-03-12", "resting_hr": 62, "provider": "google"})
        conn.commit()
        raise api.HealthAPIError("API error 500 for daily-resting-heart-rate list")

    with (
        patch.object(sync_tools.auth, "granted_scopes", return_value=None),
        patch.object(sync_tools.auth, "refresh_google_token"),
    ):
        sync_tools.rebuild(
            ["heart_rate"], "2026-03-10", "2026-03-16", apply=True, handlers={"heart_rate": handler}
        )
    assert _stored(cache) == {**_BEFORE, "2026-03-12": 62}


def _sync_answers(lists=None, rollups=None):
    """Patches answering each list and rollup type from the dicts given, else nothing."""

    def listed(data_type, start, end, view="list"):
        return (lists or {}).get(data_type, [])

    def rolled(data_type, start, end):
        return (rollups or {}).get(data_type, [])

    return (
        patch.object(google_sync.api, "list_google_data_points", side_effect=listed),
        patch.object(google_sync.api, "daily_roll_up", side_effect=rolled),
        patch.object(sync_tools.auth, "granted_scopes", return_value=None),
        patch.object(sync_tools.auth, "refresh_google_token"),
        patch.object(sync_tools.config, "OFFLINE_MODE", False),
    )


def _rebuild_with(types, apply=True, lists=None, rollups=None):
    first, second, third, fourth, fifth = _sync_answers(lists, rollups)
    with first, second, third, fourth, fifth:
        return sync_tools.rebuild(list(types), "2026-03-10", "2026-03-16", apply=apply)


def _rows(path, table):
    conn = db.get_db(path)
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        conn.close()


def _write(path, table, row):
    conn = db.get_db(path)
    db._upsert(conn, table, row)
    conn.commit()
    conn.close()


def _steps(day, count):
    return {
        "civilStartTime": {"date": {"year": 2026, "month": 3, "day": day}},
        "steps": {"countSum": str(count)},
    }


_IMPORTED_MINUTES = {"very_active_minutes": 30, "active_minutes": 45}


def test_an_imported_value_beside_googles_survives_applying(cache):
    _write(cache, "activity", {"date": "2026-03-12", "steps": 900, **_IMPORTED_MINUTES})
    _write(cache, "activity", {"date": "2026-03-12", "steps": 950, "provider": "google"})
    report = _rebuild_with(["activity"], rollups={"steps": [_steps(12, 960)]})
    (row,) = _rows(cache, "activity")
    assert (row["steps"], row["very_active_minutes"], row["active_minutes"]) == (960, 30, 45)
    assert report["types"]["activity"]["changed"] == [
        {"date": "2026-03-12", "columns": {"steps": {"before": 950, "after": 960}}}
    ]


def test_a_row_google_no_longer_holds_goes_and_is_listed_with_what_it_held(cache):
    _write(
        cache,
        "activity",
        {"date": "2026-03-12", "steps": 950, "provider": "google", **_IMPORTED_MINUTES},
    )
    report = _rebuild_with(["activity"], rollups={"steps": [_steps(13, 960)]})
    assert [r["date"] for r in _rows(cache, "activity")] == ["2026-03-13"]
    (removed,) = report["types"]["activity"]["removed"]
    assert (removed["steps"], removed["very_active_minutes"]) == (950, 30)


def test_a_workout_google_still_lists_is_kept_even_unreadable(cache):
    name = "users/me/dataTypes/exercise/dataPoints/1"
    _write(
        cache,
        "exercises",
        {"log_id": name, "date": "2026-03-12", "name": "Run", "provider": "google"},
    )
    walk = {
        "name": name.replace("/1", "/2"),
        "exercise": {"interval": {"startTime": "2026-03-13T08:00:00Z", "startUtcOffset": "0s"}},
    }
    report = _rebuild_with(["exercises"], lists={"exercise": [{"name": name}, walk]})
    assert name in [r["log_id"] for r in _rows(cache, "exercises")]
    assert report["types"]["exercises"]["removed"] == []


def test_a_removal_on_the_day_either_side_is_reported(cache):
    """The sweep covers the fetch's own dates, so the report must too."""
    _write(cache, "food_entries", {"date": "2026-03-09", "points": "[]", "provider": "google"})
    report = _rebuild_with(["food_entries"])
    assert [r["date"] for r in report["types"]["food_entries"]["removed"]] == ["2026-03-09"]


def test_a_workout_google_dropped_takes_its_route(cache):
    name = "users/me/dataTypes/exercise/dataPoints/1"
    _write(
        cache,
        "exercises",
        {"log_id": name, "date": "2026-03-12", "name": "Run", "provider": "google"},
    )
    _write(
        cache,
        "exercise_routes",
        {"log_id": name, "date": "2026-03-12", "tcx": "<x/>", "provider": "google"},
    )
    _rebuild_with(["exercises"])
    assert (_rows(cache, "exercises"), _rows(cache, "exercise_routes")) == ([], [])


def test_a_route_already_held_is_kept_and_not_fetched_again(cache):
    name = "users/me/dataTypes/exercise/dataPoints/1"
    _write(
        cache,
        "exercise_routes",
        {"log_id": name, "date": "2026-03-12", "tcx": "<x/>", "provider": "google"},
    )

    def gps_walk(identifier, day):
        return {
            "name": identifier,
            "exercise": {
                "exerciseMetadata": {"hasGps": True},
                "interval": {"startTime": f"2026-03-{day}T08:00:00Z", "startUtcOffset": "0s"},
            },
        }

    other = name.replace("/1", "/2")
    with patch.object(google_sync.api, "export_exercise_tcx", return_value="<y/>") as export:
        _rebuild_with(
            ["exercise_routes"], lists={"exercise": [gps_walk(name, 12), gps_walk(other, 13)]}
        )
    # Only the route not yet held is exported, and the held one stays.
    assert [call.args[0] for call in export.call_args_list] == [other]
    assert [r["log_id"] for r in _rows(cache, "exercise_routes")] == [name, other]


def test_the_record_of_googles_columns_is_not_reported_as_a_change(cache):
    _write(cache, "weight", {"date": "2026-03-12", "weight_kg": 70.0, "provider": "google"})
    point = {
        "name": "users/me/dataTypes/weight/dataPoints/1",
        "weight": {
            "sampleTime": {"civilTime": {"date": {"year": 2026, "month": 3, "day": 12}}},
            "weightGrams": "70000",
        },
    }
    report = _rebuild_with(["weight"], lists={"weight": [point]})
    weight = report["types"]["weight"]
    assert (weight["changed"], weight["unchanged"]) == ([], 1)
    (row,) = _rows(cache, "weight")
    assert json.loads(row["google_columns"]) == ["weight_kg"]


def _a_row_of(table):
    """A row of `table` dated inside the window, as this provider wrote it."""
    conn = db.get_db(":memory:")
    columns = [r[1] for r in conn.execute(f"PRAGMA table_info('{table}')")]
    conn.close()
    given = {
        "date": "2026-03-12",
        "datetime": "2026-03-12T08:00:00",
        "temp_celsius": 37.0,
        "provider": "google",
        # Every figure Google wrote, so nothing but Google's answer holds the row.
        "google_columns": '["weight_kg", "fat_pct"]',
        "bmi": None,
    }
    return {
        column: given.get(column, f"users/me/{table}/1" if column in db.key_columns(table) else 1)
        for column in columns
    }


@pytest.mark.parametrize(
    "dtype", [t for t in google_sync.GOOGLE_SYNC_HANDLERS if db.table_for(t) is not None]
)
def test_the_tables_left_to_their_sync_are_the_ones_it_removes_from(tmp_path, dtype):
    """Removal is left to the sync exactly where the sync removes; a route goes with its workout."""
    table = db.table_for(dtype)
    path = tmp_path / "cache.db"
    _write(path, table, _a_row_of(table))
    conn = db.get_db(path)
    first, second, third, fourth, fifth = _sync_answers()
    with first, second, third, fourth, fifth:
        google_sync.GOOGLE_SYNC_HANDLERS[dtype](conn, date(2026, 3, 10), date(2026, 3, 16))
    conn.close()
    removed_by_its_sync = _rows(path, table) == []
    assert removed_by_its_sync == (table in sync_tools._SWEPT_TABLES - {"exercise_routes"})


def test_a_compressed_column_is_named_not_sent(cache):
    conn = db.get_db(cache)
    db.save_food_entries(conn, {"date": "2026-03-12", "points": "[]", "provider": "google"})
    conn.commit()
    conn.close()
    with patch.object(google_sync.api, "daily_roll_up", return_value=[]):
        report = _rebuild([], apply=False, types=("food_entries",))
    (removed,) = report["types"]["food_entries"]["removed"]
    assert removed == {"date": "2026-03-12", "points": "(stored compressed)"}
    json.dumps(report)


@pytest.mark.parametrize(
    "since,until",
    [("2026-03-x", None), (None, None), ("2026-03-10", "2026-03-01")],
    ids=["bad-date", "no-since", "reversed"],
)
def test_dates_that_cannot_be_rebuilt_are_refused(cache, since, until):
    report = sync_tools.rebuild(["heart_rate"], since, until)
    assert report["types"]["heart_rate"]["status"] == "error"
    assert _stored(cache) == _BEFORE


def test_a_type_with_no_dates_is_refused(cache):
    report = _rebuild([], apply=True, types=("account",))
    assert report["types"]["account"]["status"] == "error"


class TestTheTool:
    async def _call(self, cache, **kwargs):
        from google_health_mcp.tools.sync_tools import health_rebuild

        with (
            patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
            patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
            patch.object(db, "DB_PATH", cache),
            patch.object(
                sync_tools, "rebuild", return_value={"applied": False, "types": {}}
            ) as run,
        ):
            client.exists.return_value = True
            tokens.exists.return_value = True
            body = json.loads(await health_rebuild(**kwargs))
        return body, run

    async def test_it_previews_unless_told_to_apply(self, cache):
        _, run = await self._call(cache, since="2026-03-10")
        assert run.call_args.kwargs["apply"] is False

    async def test_all_means_every_type_with_dates(self, cache):
        _, run = await self._call(cache, since="2026-03-10")
        types = run.call_args.args[0]
        assert "account" not in types
        assert set(types) == set(google_sync.GOOGLE_SYNC_HANDLERS) - {"account"}

    async def test_offline_it_refuses(self, cache):
        with patch.object(sync_tools.config, "OFFLINE_MODE", True):
            body, run = await self._call(cache, since="2026-03-10")
        assert body["offline_mode"] is True
        assert not run.called


def test_the_command_previews_unless_told_to_apply(cache, monkeypatch, capsys):
    from google_health_mcp import cli

    seen = {}

    def fake(types, since, until, apply=False):
        seen["apply"] = apply
        return {"applied": apply, "types": {"heart_rate": {"status": "ok"}}}

    monkeypatch.setattr(sync_tools, "rebuild", fake)
    monkeypatch.setattr(sync_tools.config, "OFFLINE_MODE", False)
    monkeypatch.setattr("sys.argv", ["google-health-mcp", "rebuild", "--since", "2026-03-10"])
    cli.main()
    assert seen["apply"] is False
    assert json.loads(capsys.readouterr().out)["applied"] is False
    monkeypatch.setattr(
        "sys.argv", ["google-health-mcp", "rebuild", "--since", "2026-03-10", "--apply"]
    )
    cli.main()
    assert seen["apply"] is True


def test_the_command_refuses_offline(monkeypatch):
    from google_health_mcp import cli

    monkeypatch.setattr(sync_tools.config, "OFFLINE_MODE", True)
    monkeypatch.setattr("sys.argv", ["google-health-mcp", "rebuild", "--since", "2026-03-10"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1


def test_the_window_is_the_dates_asked_for(cache):
    asked = []

    def answer(data_type, start, end, view="list"):
        asked.append((start, end))
        return []

    with (
        patch.object(google_sync.api, "list_google_data_points", side_effect=answer),
        patch.object(sync_tools.auth, "granted_scopes", return_value=None),
        patch.object(sync_tools.auth, "refresh_google_token"),
    ):
        sync_tools.rebuild(["heart_rate"], "2026-03-10", "2026-03-16")
        sync_tools.rebuild(["heart_rate"], "2026-03-10", "2999-01-01")
    # A day either side, and never past today.
    assert asked == [
        (date(2026, 3, 9), date(2026, 3, 18)),
        (date(2026, 3, 9), date.today() + timedelta(days=1)),
    ]
