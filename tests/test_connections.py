"""Every query tool closes its cache connection when the read fails.

A connection closed only after the read returns stays open, when the read
raises, until the traceback is freed, rather than closing where it failed.
"""

import sqlite3
from unittest.mock import patch

import pytest

from google_health_mcp import db, helpers
from google_health_mcp.tools import (
    activity_tools,
    azm_tools,
    breathing_rate_tools,
    cardio_fitness_tools,
    exercise_tools,
    food_tools,
    heart_tools,
    hrv_tools,
    profile_tools,
    rhythm_tools,
    sleep_tools,
    spo2_tools,
    temperature_tools,
    weight_tools,
)

#: (module, tool, the db query it reads through): every query tool taking the
#: `live` flag, which test_every_tool_with_a_live_flag_is_listed holds complete.
#: health_trends is pinned in tests/test_analysis.py.
_TOOLS = [
    (activity_tools, "health_get_activity", "query_activity"),
    (azm_tools, "health_get_azm", "query_azm"),
    (breathing_rate_tools, "health_get_breathing_rate", "query_breathing_rate"),
    (cardio_fitness_tools, "health_get_cardio_fitness", "query_cardio_fitness"),
    (exercise_tools, "health_get_exercises", "query_exercises"),
    (food_tools, "health_get_food_log", "query_food_log"),
    (heart_tools, "health_get_heart_rate", "query_heart_rate"),
    (hrv_tools, "health_get_hrv", "query_hrv"),
    (rhythm_tools, "health_get_ecg", "query_ecg"),
    (rhythm_tools, "health_get_irregular_rhythm", "query_irn"),
    (sleep_tools, "health_get_sleep", "query_sleep"),
    (sleep_tools, "health_get_sleep_sessions", "query_sleep_sessions"),
    (spo2_tools, "health_get_spo2", "query_spo2"),
    (temperature_tools, "health_get_skin_temperature", "query_skin_temperature"),
    (temperature_tools, "health_get_core_temperature", "query_core_temperature"),
    (weight_tools, "health_get_weight", "query_weight"),
    (weight_tools, "health_get_weight_readings", "query_weight_readings"),
    (weight_tools, "health_get_height", "query_height"),
    (profile_tools, "health_get_profile", "query_account"),
]


def _tracked_connections():
    """A get_db replacement, and the connections it has opened and closed."""
    opened, closed = [], []

    class _Recording(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def get_db(*args, **kwargs):
        conn = sqlite3.connect(":memory:", factory=_Recording, check_same_thread=False)
        opened.append(conn)
        return conn

    return get_db, opened, closed


async def _tools_with_a_live_flag():
    from google_health_mcp import cli  # noqa: F401  (registers every tool)
    from google_health_mcp.mcp_instance import mcp

    tools = await mcp.list_tools()
    return {t.name for t in tools if "live" in t.input_schema.get("properties", {})}


async def test_every_tool_with_a_live_flag_is_listed():
    live = await _tools_with_a_live_flag()
    assert live, "no tool takes live, so this proves nothing"
    assert live == {name for _m, name, _q in _TOOLS}


@pytest.mark.parametrize(("module", "name", "query"), _TOOLS, ids=[name for _m, name, _q in _TOOLS])
async def test_a_failing_cache_read_closes_its_connection(module, name, query):
    get_db, opened, closed = _tracked_connections()
    with (
        patch.object(helpers, "GOOGLE_CLIENT_PATH") as client,
        patch.object(helpers, "GOOGLE_TOKENS_PATH") as tokens,
        patch.object(module, "refresh_before_query"),
        patch.object(db, "get_db", get_db),
        patch.object(db, query, side_effect=RuntimeError("the read failed")),
        pytest.raises(RuntimeError),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        await getattr(module, name)()
    assert opened, "no connection was opened, so this proves nothing"
    assert closed == opened
