"""Profile, settings, height and exercise routes: stored as Google returns them.

The profile, settings and irregular-rhythm enrolment are per-account records
rather than dated series, so each is kept whole, as the JSON Google sent. A
route is the TCX export of an exercise with GPS, kept as the text Google sent.
Nothing here computes a value from them.
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from google_health_mcp import api, config
from google_health_mcp import db as db_mod
from google_health_mcp.tools import google_sync

# Fictional throughout.
_PROFILE = {"name": "users/me/profile", "age": 99, "userConfiguredWalkingStrideLengthMm": 123}
_SETTINGS = {"name": "users/me/settings", "timeZone": "Etc/UTC", "weightUnit": "KILOGRAM"}
_IRN = {"name": "users/me/irnProfile", "enrollmentStatus": False}
_TCX = "<TrainingCenterDatabase><Activities/></TrainingCenterDatabase>"


class TestTheClient:
    @pytest.mark.parametrize(
        "call,path",
        [
            (api.get_profile, "users/me/profile"),
            (api.get_settings, "users/me/settings"),
            (api.get_irn_profile, "users/me/irnProfile"),
        ],
    )
    def test_each_account_record_is_read_from_its_own_path(self, call, path):
        with patch.object(api, "google_get", return_value={"k": 1}) as get:
            assert call() == {"k": 1}
        assert get.call_args.args[0] == path

    def test_a_route_is_asked_for_under_the_exercise_name(self):
        name = "users/me/dataTypes/exercise/dataPoints/123"
        with patch.object(api, "google_get", return_value={"tcxData": _TCX}) as get:
            assert api.export_exercise_tcx(name) == _TCX
        assert get.call_args.args[0] == f"{name}:exportExerciseTcx"

    @pytest.mark.parametrize("body", [{}, {"tcxData": 5}], ids=["absent", "not-text"])
    def test_a_route_response_without_its_text_is_refused(self, body):
        with patch.object(api, "google_get", return_value=body):
            with pytest.raises(api.HealthAPIError):
                api.export_exercise_tcx("users/me/dataTypes/exercise/dataPoints/1")


def _run(handler, tmp_db, **patches):
    with patch.multiple(google_sync.api, **patches):
        count = handler(tmp_db, date(2026, 3, 1), date(2026, 3, 31))
    tmp_db.commit()
    return count


class TestTheAccountRecords:
    def test_each_is_stored_as_google_sent_it(self, tmp_db):
        count = _run(
            google_sync.sync_account,
            tmp_db,
            get_profile=lambda: _PROFILE,
            get_settings=lambda: _SETTINGS,
            get_irn_profile=lambda: _IRN,
        )
        assert count == 3
        stored = db_mod.query_account(tmp_db)
        assert stored["profile"]["body"] == _PROFILE
        assert stored["settings"]["body"] == _SETTINGS
        assert stored["irn_profile"]["body"] == _IRN
        assert stored["profile"]["fetched_at"].endswith("+00:00")
        providers = {r[0] for r in tmp_db.execute("SELECT provider FROM account")}
        assert providers == {"google"}

    @pytest.mark.parametrize("body", [{}, [1, 2], "text"], ids=["empty", "list", "str"])
    def test_a_record_that_is_not_an_object_writes_nothing(self, tmp_db, body):
        count = _run(
            google_sync.sync_account,
            tmp_db,
            get_profile=lambda: body,
            get_settings=lambda: _SETTINGS,
            get_irn_profile=lambda: _IRN,
        )
        assert count == 2
        assert "profile" not in db_mod.query_account(tmp_db)

    def test_an_api_error_on_one_record_keeps_the_others_and_still_fails(self, tmp_db):
        def refused():
            raise api.HealthAPIError("API error 500 for settings", 500)

        with pytest.raises(api.HealthAPIError):
            _run(
                google_sync.sync_account,
                tmp_db,
                get_profile=lambda: _PROFILE,
                get_settings=refused,
                get_irn_profile=lambda: _IRN,
            )
        assert set(db_mod.query_account(tmp_db)) == {"profile", "irn_profile"}

    @pytest.mark.parametrize(
        "error",
        [
            api.HealthNetworkError("Network error. Check your connection."),
            api.HealthRateLimitError(60),
        ],
        ids=["no-answer", "rate-limited"],
    )
    def test_a_failure_the_next_record_would_share_stops_at_once(self, tmp_db, error):
        fetched = []

        def fails():
            fetched.append("profile")
            raise error

        with pytest.raises(type(error)):
            _run(
                google_sync.sync_account,
                tmp_db,
                get_profile=fails,
                get_settings=lambda: fetched.append("settings") or _SETTINGS,
                get_irn_profile=lambda: fetched.append("irn") or _IRN,
            )
        assert fetched == ["profile"]

    def test_a_record_whose_scope_is_trimmed_is_not_fetched(self, tmp_db, monkeypatch):
        trimmed = {k: v for k, v in config.GOOGLE_SCOPE_READERS.items() if k != "profile"}
        monkeypatch.setattr(config, "GOOGLE_SCOPE_READERS", trimmed)
        fetched = []
        _run(
            google_sync.sync_account,
            tmp_db,
            get_profile=lambda: fetched.append(1) or _PROFILE,
            get_settings=lambda: _SETTINGS,
            get_irn_profile=lambda: _IRN,
        )
        assert fetched == []

    def test_an_empty_record_writes_nothing(self, tmp_db):
        count = _run(
            google_sync.sync_account,
            tmp_db,
            get_profile=lambda: {},
            get_settings=lambda: _SETTINGS,
            get_irn_profile=lambda: _IRN,
        )
        assert count == 2
        assert "profile" not in db_mod.query_account(tmp_db)

    def test_a_later_read_replaces_the_stored_one(self, tmp_db):
        for age in (98, 99):
            _run(
                google_sync.sync_account,
                tmp_db,
                get_profile=lambda age=age: {**_PROFILE, "age": age},
                get_settings=lambda: _SETTINGS,
                get_irn_profile=lambda: _IRN,
            )
        assert db_mod.query_account(tmp_db)["profile"]["body"]["age"] == 99


def _height_point(mm, day=10):
    civil = {"date": {"year": 2023, "month": 1, "day": day}, "time": {}}
    return {"height": {"heightMillimeters": mm, "sampleTime": {"civilTime": civil}}}


class TestHeight:
    def test_the_whole_history_is_asked_for_whatever_the_window(self, tmp_db):
        """One reading, recorded once, years before any incremental window."""
        seen = {}

        def capture(data_type, start, end):
            seen[data_type] = (start, end)
            return []

        _run(google_sync.sync_height, tmp_db, list_google_data_points=capture)
        start, end = seen["height"]
        assert start <= date(2000, 1, 1)
        # Closed-open: the window's last day is included.
        assert end == date(2026, 4, 1)

    def test_a_reading_is_stored_in_googles_unit(self, tmp_db):
        _run(
            google_sync.sync_height,
            tmp_db,
            list_google_data_points=lambda *a: [_height_point("1234")],
        )
        (row,) = db_mod.query_height(tmp_db, "2000-01-01", "2030-12-31")
        assert row["height_mm"] == 1234
        assert row["date"] == "2023-01-10"
        assert row["provider"] == "google"

    def test_an_unreadable_reading_writes_nothing(self, tmp_db):
        count = _run(
            google_sync.sync_height,
            tmp_db,
            list_google_data_points=lambda *a: [_height_point("tall")],
        )
        assert count == 0
        assert db_mod.query_height(tmp_db, "2000-01-01", "2030-12-31") == []


def _exercise(identifier, has_gps, start="2026-03-10T08:00:00Z", offset="0s"):
    metadata = {} if has_gps is None else {"hasGps": has_gps}
    return {
        "name": identifier,
        "exercise": {
            "exerciseMetadata": metadata,
            "interval": {"startTime": start, "startUtcOffset": offset},
        },
    }


class TestExerciseRoutes:
    def test_only_an_exercise_with_gps_is_exported(self, tmp_db):
        exported = []

        def export(name):
            exported.append(name)
            return _TCX

        count = _run(
            google_sync.sync_exercise_routes,
            tmp_db,
            list_google_data_points=lambda *a: [
                _exercise("ex/1", True),
                _exercise("ex/2", None),
                _exercise("ex/3", False),
                _exercise("ex/4", "true"),
            ],
            export_exercise_tcx=export,
        )
        assert exported == ["ex/1"]
        assert count == 1
        route = db_mod.query_exercise_route(tmp_db, "ex/1")
        assert route["tcx"] == _TCX
        assert route["date"] == "2026-03-10"
        assert route["provider"] == "google"

    async def test_the_route_tool_returns_the_date(self, tmp_db):
        from google_health_mcp.tools.exercise_tools import health_get_exercise_route

        db_mod.save_exercise_route(
            tmp_db, {"log_id": "ex/1", "date": "2026-03-10", "tcx": _TCX, "provider": "google"}
        )
        tmp_db.commit()
        path = Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])
        body = await _call(health_get_exercise_route, path, "exercise_tools", log_id="ex/1")
        assert body["date"] == "2026-03-10"

    def test_a_route_is_dated_by_the_local_day_it_started(self, tmp_db):
        _run(
            google_sync.sync_exercise_routes,
            tmp_db,
            list_google_data_points=lambda *a: [
                _exercise("ex/1", True, start="2026-03-10T23:30:00Z", offset="3600s")
            ],
            export_exercise_tcx=lambda name: _TCX,
        )
        assert db_mod.query_exercise_route(tmp_db, "ex/1")["date"] == "2026-03-11"

    def test_a_day_another_provider_recorded_is_left_to_it(self, tmp_db):
        """The workout listed for that day carries the other provider's id, so
        a route stored under Google's could never be looked up."""
        db_mod.save_exercise(tmp_db, "imported-1", {"date": "2026-03-10", "name": "Run"})
        tmp_db.commit()
        exported = []
        count = _run(
            google_sync.sync_exercise_routes,
            tmp_db,
            list_google_data_points=lambda *a: [_exercise("ex/1", True)],
            export_exercise_tcx=lambda name: exported.append(name) or _TCX,
        )
        assert exported == []
        assert count == 0

    @pytest.mark.parametrize(
        "error",
        [
            api.HealthAPIError("API error 500 for exercise exportExerciseTcx"),
            api.HealthAPIError("API error 404 for exercise exportExerciseTcx"),
            api.GoogleGatewayTimeout("Google timed out serving the request."),
            api.HealthAPIError("Google returned an exercise export with no TCX text."),
        ],
        ids=["500", "404", "504", "empty-export"],
    )
    def test_one_workouts_failed_export_is_left_out_not_failed(self, tmp_db, error):
        """One workout whose export always fails must not fail the type on
        every run, which on an hourly timer is an alert every hour."""

        def export(name):
            if name == "ex/1":
                raise error
            return _TCX

        count = _run(
            google_sync.sync_exercise_routes,
            tmp_db,
            list_google_data_points=lambda *a: [_exercise("ex/1", True), _exercise("ex/2", True)],
            export_exercise_tcx=export,
        )
        assert count == 1
        assert db_mod.route_ids(tmp_db) == {"ex/2"}

    def test_a_name_the_export_refuses_is_left_out_through_the_real_client(self, tmp_db):
        """Through the real export, since the refusal happens inside it."""
        good = "users/me/dataTypes/exercise/dataPoints/2"
        with patch.object(api, "google_get", return_value={"tcxData": _TCX}):
            count = _run(
                google_sync.sync_exercise_routes,
                tmp_db,
                list_google_data_points=lambda *a: [
                    _exercise("users/me/dataTypes/exercise/dataPoints/a.b", True),
                    _exercise(good, True),
                ],
            )
        assert count == 1
        assert db_mod.route_ids(tmp_db) == {good}

    @pytest.mark.parametrize(
        "error",
        [
            api.HealthNetworkError("Network error. Check your connection."),
            api.HealthAuthError("refused"),
            api.HealthRateLimitError(60),
        ],
        ids=["no-answer", "refused", "rate-limited"],
    )
    def test_a_failure_the_next_export_would_share_stops_the_sync(self, tmp_db, error):
        """Each export behind it would wait out the same timeout or refusal."""
        exported = []

        def export(name):
            exported.append(name)
            raise error

        with pytest.raises(type(error)):
            _run(
                google_sync.sync_exercise_routes,
                tmp_db,
                list_google_data_points=lambda *a: [
                    _exercise("ex/1", True),
                    _exercise("ex/2", True),
                ],
                export_exercise_tcx=export,
            )
        assert exported == ["ex/1"]

    def test_a_route_already_held_is_not_fetched_again(self, tmp_db):
        """A recorded route does not change, and an export can be large."""
        db_mod.save_exercise_route(
            tmp_db, {"log_id": "ex/1", "date": "2026-03-10", "tcx": _TCX, "provider": "google"}
        )
        tmp_db.commit()
        exported = []
        _run(
            google_sync.sync_exercise_routes,
            tmp_db,
            list_google_data_points=lambda *a: [_exercise("ex/1", True)],
            export_exercise_tcx=lambda name: exported.append(name) or _TCX,
        )
        assert exported == []

    def test_an_unknown_route_reads_as_none(self, tmp_db):
        assert db_mod.query_exercise_route(tmp_db, "ex/none") is None


async def _call(tool, db_path, module, **kwargs):
    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch(f"google_health_mcp.tools.{module}.refresh_before_query"),
        patch.object(db_mod, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        return json.loads(await tool(**kwargs))


class TestTheTools:
    @pytest.fixture
    def db_path(self, tmp_db):
        return Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])

    async def test_an_empty_profile_cache_says_so(self, db_path):
        from google_health_mcp.tools.profile_tools import health_get_profile

        body = await _call(health_get_profile, db_path, "profile_tools")
        assert "message" in body
        assert "profile" not in body

    async def test_the_profile_tool_returns_all_three_records(self, tmp_db, db_path):
        from google_health_mcp.tools.profile_tools import health_get_profile

        for resource, body in (("profile", _PROFILE), ("settings", _SETTINGS)):
            db_mod.save_account(
                tmp_db,
                {"resource": resource, "body": json.dumps(body), "fetched_at": "x"},
            )
        tmp_db.commit()
        body = await _call(health_get_profile, db_path, "profile_tools")
        assert body["profile"] == _PROFILE
        assert body["settings"] == _SETTINGS
        assert body["irn_profile"] is None

    async def test_the_height_tool_returns_the_readings(self, tmp_db, db_path):
        from google_health_mcp.tools.weight_tools import health_get_height

        db_mod.save_height(
            tmp_db, {"datetime": "2023-01-10T00:00:00", "date": "2023-01-10", "height_mm": 1234}
        )
        tmp_db.commit()
        body = await _call(health_get_height, db_path, "weight_tools")
        assert body["height"][0]["height_mm"] == 1234

    async def test_the_route_stays_behind_its_flag(self, tmp_db, db_path):
        """Half a megabyte of XML is a useless answer unasked."""
        from google_health_mcp.tools.exercise_tools import health_get_exercise_route

        db_mod.save_exercise_route(
            tmp_db, {"log_id": "ex/1", "date": "2026-03-10", "tcx": _TCX, "provider": "google"}
        )
        tmp_db.commit()
        plain = await _call(health_get_exercise_route, db_path, "exercise_tools", log_id="ex/1")
        assert "tcx" not in plain
        assert plain["log_id"] == "ex/1"
        full = await _call(
            health_get_exercise_route, db_path, "exercise_tools", log_id="ex/1", include_tcx=True
        )
        assert full["tcx"] == _TCX

    async def test_an_exercise_with_no_route_says_so(self, tmp_db, db_path):
        from google_health_mcp.tools.exercise_tools import health_get_exercise_route

        body = await _call(health_get_exercise_route, db_path, "exercise_tools", log_id="ex/9")
        assert "cache" in body["message"]
        assert "since" in body["hint"]
