"""Tests for MCP tool functions (the async wrappers)."""

import json
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from google_health_mcp.api import HealthAPIError, HealthOfflineError
from google_health_mcp.helpers import require_auth


class TestRequireAuth:
    """Test the auth decorator."""

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_missing_config(self, mock_tokens_path, mock_config_path):
        mock_config_path.exists.return_value = False
        mock_tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            return "should not reach"

        result = await tool_fn()
        parsed = json.loads(result)
        assert "error" in parsed
        # The remedy, not the wording: this is the whole of what the user can
        # act on, and it is the half a rephrasing is most likely to drop.
        assert "Run: google-health-mcp auth" in parsed["error"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_missing_tokens(self, mock_tokens_path, mock_config_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = False

        @require_auth
        async def tool_fn():
            return "should not reach"

        result = await tool_fn()
        parsed = json.loads(result)
        assert "error" in parsed

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_auth_present_passes_through(self, mock_tokens_path, mock_config_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            return "success"

        result = await tool_fn()
        assert result == "success"

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_preserves_function_name(self, mock_tokens_path, mock_config_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        @require_auth
        async def my_tool():
            return "ok"

        assert my_tool.__name__ == "my_tool"


class TestToolQueryFunctions:
    """Test tool query functions return correct structure from cache."""

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_activity_empty_cache(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.activity_tools import health_get_activity

            result = await health_get_activity(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "message" in parsed
        assert "No activity data" in parsed["message"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_activity_with_data(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        conn = db_mod.get_db(db_path)
        db_mod.save_activity(
            conn,
            {
                "date": "2026-03-12",
                "steps": 10000,
                "calories_out": 2500,
                "active_minutes": 45,
                "very_active_minutes": 20,
                "fairly_active_minutes": 25,
                "lightly_active_minutes": 200,
                "sedentary_minutes": 500,
                "floors": 10,
                "distance_km": 7.5,
            },
        )
        conn.commit()
        conn.close()

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.activity_tools import health_get_activity

            result = await health_get_activity(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "activity" in parsed
        assert parsed["count"] == 1
        assert parsed["activity"][0]["steps"] == 10000

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_sleep_empty_cache(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.sleep_tools import health_get_sleep

            result = await health_get_sleep(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "No sleep data" in parsed["message"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_heart_rate_empty_cache(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.heart_tools import health_get_heart_rate

            result = await health_get_heart_rate(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "No heart rate data" in parsed["message"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_trends_empty_cache(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.analysis_tools import health_trends

            result = await health_trends(
                data_type="activity",
                start_date="2026-03-01",
                end_date="2026-03-31",
            )

        parsed = json.loads(result)
        assert "message" in parsed

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_trends_invalid_type(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.analysis_tools import health_trends

            result = await health_trends(data_type="nonexistent")

        parsed = json.loads(result)
        assert "error" in parsed

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_exercises_empty_cache(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.exercise_tools import health_get_exercises

            result = await health_get_exercises(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "No exercise entries" in parsed["message"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_exercises_with_data(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        conn = db_mod.get_db(db_path)
        db_mod.save_exercise(
            conn,
            "log001",
            {
                "date": "2026-03-12",
                "name": "Cycling",
                "duration_min": 45,
                "calories": 350,
                "avg_hr": 130,
                "steps": None,
                "distance_km": 12.0,
                "distance_unit": "Kilometer",
                "start_time": "2026-03-12T07:30:00",
                "source": "Tracker",
                "log_type": "auto_detected",
            },
        )
        conn.commit()
        conn.close()

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.exercise_tools import health_get_exercises

            result = await health_get_exercises(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "exercises" in parsed
        assert parsed["count"] == 1
        assert parsed["exercises"][0]["name"] == "Cycling"

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_exercises_type_filter(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        conn = db_mod.get_db(db_path)
        db_mod.save_exercise(
            conn,
            "log001",
            {
                "date": "2026-03-12",
                "name": "Cycling",
                "duration_min": 45,
                "calories": 350,
                "avg_hr": 130,
                "steps": None,
                "distance_km": 12.0,
                "distance_unit": "Kilometer",
                "start_time": "2026-03-12T07:30:00",
                "source": "Tracker",
                "log_type": "auto",
            },
        )
        db_mod.save_exercise(
            conn,
            "log002",
            {
                "date": "2026-03-13",
                "name": "Walk",
                "duration_min": 30,
                "calories": 180,
                "avg_hr": 100,
                "steps": 4000,
                "distance_km": 2.5,
                "distance_unit": "Kilometer",
                "start_time": "2026-03-13T12:00:00",
                "source": "Tracker",
                "log_type": "auto",
            },
        )
        conn.commit()
        conn.close()

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.exercise_tools import health_get_exercises

            result = await health_get_exercises(
                start_date="2026-03-10", end_date="2026-03-15", exercise_type="cycl"
            )

        parsed = json.loads(result)
        assert parsed["count"] == 1
        assert parsed["exercises"][0]["name"] == "Cycling"

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_weight_empty_cache(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.weight_tools import health_get_weight

            result = await health_get_weight(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "No weight data" in parsed["message"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_weight_with_data(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        conn = db_mod.get_db(db_path)
        db_mod.save_weight(
            conn, {"date": "2026-03-12", "weight_kg": 79.5, "bmi": 24.5, "fat_pct": 19.0}
        )
        conn.commit()
        conn.close()

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.weight_tools import health_get_weight

            result = await health_get_weight(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "weight" in parsed
        assert parsed["count"] == 1
        assert parsed["weight"][0]["weight_kg"] == 79.5

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_spo2_empty_cache(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.spo2_tools import health_get_spo2

            result = await health_get_spo2(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "No SpO2 data" in parsed["message"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_spo2_with_data(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        conn = db_mod.get_db(db_path)
        db_mod.save_spo2(conn, {"date": "2026-03-12", "avg": 96.5, "min": 93.0, "max": 99.0})
        conn.commit()
        conn.close()

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.spo2_tools import health_get_spo2

            result = await health_get_spo2(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "spo2" in parsed
        assert parsed["count"] == 1
        assert parsed["spo2"][0]["avg"] == 96.5

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_hrv_empty_cache(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.hrv_tools import health_get_hrv

            result = await health_get_hrv(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "No HRV data" in parsed["message"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_hrv_with_data(self, mock_tokens_path, mock_config_path, tmp_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        conn = db_mod.get_db(db_path)
        db_mod.save_hrv(conn, {"date": "2026-03-12", "daily_rmssd": 38.0, "deep_rmssd": 44.0})
        conn.commit()
        conn.close()

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.hrv_tools import health_get_hrv

            result = await health_get_hrv(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert "hrv" in parsed
        assert parsed["count"] == 1
        assert parsed["hrv"][0]["daily_rmssd"] == 38.0


class TestOfflineMode:
    """Offline / cache-only mode (GOOGLE_HEALTH_MCP_OFFLINE)."""

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_require_auth_skips_credential_check(
        self, mock_tokens_path, mock_config_path, monkeypatch
    ):
        # No credential files, but offline mode lets the tool run anyway.
        mock_config_path.exists.return_value = False
        mock_tokens_path.exists.return_value = False
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        @require_auth
        async def tool_fn():
            return json.dumps({"ok": True})

        parsed = json.loads(await tool_fn())
        assert parsed["ok"] is True
        assert parsed["offline_mode"] is True

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_require_auth_still_gates_when_not_offline(
        self, mock_tokens_path, mock_config_path, monkeypatch
    ):
        # Regression guard: the offline branch must not weaken the normal gate.
        mock_config_path.exists.return_value = False
        mock_tokens_path.exists.return_value = True
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)

        @require_auth
        async def tool_fn():
            return "should not reach"

        parsed = json.loads(await tool_fn())
        assert "Run: google-health-mcp auth" in parsed["error"]

    async def test_require_auth_converts_offline_error(self, monkeypatch):
        from google_health_mcp.api import HealthOfflineError

        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        @require_auth
        async def tool_fn():
            raise HealthOfflineError("live disabled")

        parsed = json.loads(await tool_fn())
        assert parsed["offline_mode"] is True
        assert "live disabled" in parsed["error"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_cacheable_tool_serves_cache_offline(
        self, mock_tokens_path, mock_config_path, monkeypatch, tmp_path
    ):
        mock_config_path.exists.return_value = False
        mock_tokens_path.exists.return_value = False
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        conn = db_mod.get_db(db_path)
        db_mod.save_heart_rate(conn, "2026-03-12", 58, [])
        conn.commit()
        conn.close()

        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.heart_tools import health_get_heart_rate

            result = await health_get_heart_rate(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert parsed["offline_mode"] is True
        assert parsed["count"] == 1
        assert parsed["heart_rate"][0]["resting_hr"] == 58

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_cacheable_tool_live_true_offline_refused(
        self, mock_tokens_path, mock_config_path, monkeypatch, tmp_path
    ):
        mock_config_path.exists.return_value = False
        mock_tokens_path.exists.return_value = False
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.heart_tools import health_get_heart_rate

            result = await health_get_heart_rate(
                start_date="2026-03-10", end_date="2026-03-15", live=True
            )

        parsed = json.loads(result)
        assert parsed["offline_mode"] is True
        assert "GOOGLE_HEALTH_MCP_OFFLINE" in parsed["error"]

    async def test_live_only_tool_offline_refused(self, monkeypatch):
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        from google_health_mcp.tools.devices_tools import health_get_devices

        parsed = json.loads(await health_get_devices())
        assert parsed["offline_mode"] is True
        assert "GOOGLE_HEALTH_MCP_OFFLINE" in parsed["error"]

    async def test_health_sync_offline_refused(self, monkeypatch):
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        from google_health_mcp.tools.sync_tools import health_sync

        parsed = json.loads(await health_sync())
        assert parsed["offline_mode"] is True
        assert "GOOGLE_HEALTH_MCP_OFFLINE" in parsed["error"]

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_variant_live_hint_rewritten_offline(
        self, mock_tokens_path, mock_config_path, monkeypatch, tmp_path
    ):
        # cardio_fitness uses a non-canonical "Try live=True" hint; it must still
        # be rewritten offline so a cache-only host is never told to go live.
        mock_config_path.exists.return_value = False
        mock_tokens_path.exists.return_value = False
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        from google_health_mcp import db as db_mod

        db_path = tmp_path / "test.db"
        with patch.object(db_mod, "DB_PATH", db_path):
            from google_health_mcp.tools.cardio_fitness_tools import health_get_cardio_fitness

            result = await health_get_cardio_fitness(start_date="2026-03-10", end_date="2026-03-15")

        parsed = json.loads(result)
        assert parsed["offline_mode"] is True
        assert "live=True" not in parsed.get("hint", "")
        assert "host that owns the cache" in parsed.get("hint", "")


class TestWhichErrorsAreExplainedToTheModel:
    """`mcp` 2.1 keeps a `ToolError`'s text and replaces every other
    exception's with "Error executing tool <name>".

    So an error a caller could act on has to be converted, and an unplanned one
    has to be left alone.
    """

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_a_deliberate_error_keeps_its_message(self, mock_tokens_path, mock_config_path):
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            raise HealthAPIError("API error 503 for /sessions")

        with pytest.raises(ToolError) as excinfo:
            await tool_fn()
        assert "API error 503 for /sessions" in str(excinfo.value)

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_an_unplanned_error_is_left_to_be_masked(
        self, mock_tokens_path, mock_config_path
    ):
        # The text of an unplanned failure is what this package spends the rest
        # of its leak tests keeping off the wire. This is the measured case: a
        # failure to read the token file or the cache names an absolute path.
        # Converting everything would put it back.
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        @require_auth
        async def tool_fn():
            raise OSError("/home/someone/.config/google-health-mcp/google_tokens.json")

        with pytest.raises(OSError):
            await tool_fn()

    async def test_a_deliberate_error_keeps_its_message_offline_too(self, monkeypatch):
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        @require_auth
        async def tool_fn():
            raise HealthAPIError("API error 503 for /sessions")

        with pytest.raises(ToolError) as excinfo:
            await tool_fn()
        assert "API error 503 for /sessions" in str(excinfo.value)

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_an_offline_error_raised_while_online_is_converted(
        self, mock_tokens_path, mock_config_path, monkeypatch
    ):
        # Defensive rather than a live path: both raise sites are guarded by
        # the same OFFLINE_MODE the decorator reads, so a real process cannot
        # get here. Pinned anyway, because the clause sits above the
        # GoogleHealthError one and nothing else distinguishes this line from
        # its own deletion.
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)

        @require_auth
        async def tool_fn():
            raise HealthOfflineError("live API calls are disabled")

        with pytest.raises(ToolError) as excinfo:
            await tool_fn()
        assert "live API calls are disabled" in str(excinfo.value)

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_a_normal_mode_response_is_not_tagged_offline(
        self, mock_tokens_path, mock_config_path, monkeypatch
    ):
        # Annotating unconditionally is a one-token change that tells the model
        # a host which can sync must wait for one that does, and it leaves the
        # whole suite green. The assertion is the key's absence: a host that is
        # not offline must say nothing about offline mode either way.
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)

        @require_auth
        async def tool_fn():
            return json.dumps({"ok": True, "hint": "Try live=True to re-fetch this window."})

        parsed = json.loads(await tool_fn())
        assert "offline_mode" not in parsed
        assert parsed["hint"] == "Try live=True to re-fetch this window."

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_a_live_refresh_failure_keeps_its_status(
        self, mock_tokens_path, mock_config_path, monkeypatch, tmp_path
    ):
        # Every query tool reaches this on live=True, and the status word is the
        # whole of what the model can act on: `auth_error` says re-authorise,
        # where `rate_limited` says wait. Raised as a bare RuntimeError it was
        # masked, which is the one case a class-walking check cannot see.
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)

        from google_health_mcp import db as db_mod
        from google_health_mcp.tools import sync_tools

        monkeypatch.setattr(
            sync_tools, "run_sync", lambda *a, **k: {"heart_rate": {"status": "auth_error"}}
        )

        with patch.object(db_mod, "DB_PATH", tmp_path / "test.db"):
            from google_health_mcp.tools.heart_tools import health_get_heart_rate

            with pytest.raises(ToolError) as excinfo:
                await health_get_heart_rate(
                    start_date="2026-03-10", end_date="2026-03-15", live=True
                )

        assert "auth_error" in str(excinfo.value)

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_a_bad_date_still_names_the_formats(
        self, mock_tokens_path, mock_config_path, tmp_path
    ):
        # Through a real tool rather than a stand-in: parse_date runs inside the
        # tool body, so nothing converts it unless the decorator does.
        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True

        from google_health_mcp import db as db_mod

        with patch.object(db_mod, "DB_PATH", tmp_path / "test.db"):
            from google_health_mcp.tools.heart_tools import health_get_heart_rate

            with pytest.raises(ToolError) as excinfo:
                await health_get_heart_rate(start_date="not-a-date")

        assert "YYYY-MM-DD" in str(excinfo.value)
