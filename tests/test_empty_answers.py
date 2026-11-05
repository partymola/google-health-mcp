"""What a query tool says when the window holds nothing.

A live call re-fetches the window before reading the cache, so an empty answer
after one means Google has nothing there. Offering live=True again sends the
model to repeat the call it just made. Each tool's own note, about what the
data needs before any exists, is said either way.
"""

import json
from unittest.mock import patch

import pytest

from google_health_mcp import helpers
from google_health_mcp.tools import (
    activity_tools,
    azm_tools,
    breathing_rate_tools,
    cardio_fitness_tools,
    exercise_tools,
    heart_tools,
    hrv_tools,
    profile_tools,
    rhythm_tools,
    sleep_tools,
    spo2_tools,
    temperature_tools,
    weight_tools,
)

#: (module, tool, message, note) for every tool whose empty answer can offer
#: a re-fetch. food_log is not here: its note never offered one.
_TOOLS = [
    (activity_tools, "health_get_activity", "No activity data found for this period.", None),
    (azm_tools, "health_get_azm", "No AZM data found for this period.", None),
    (
        breathing_rate_tools,
        "health_get_breathing_rate",
        "No breathing rate data found for this period.",
        "Requires sleep tracking.",
    ),
    (
        cardio_fitness_tools,
        "health_get_cardio_fitness",
        "No cardio fitness data found for this period.",
        "Requires age/sex profile and recent walking/running data.",
    ),
    (exercise_tools, "health_get_exercises", "No exercise entries found for this period.", None),
    (heart_tools, "health_get_heart_rate", "No heart rate data found for this period.", None),
    (hrv_tools, "health_get_hrv", "No HRV data found for this period.", None),
    (
        rhythm_tools,
        "health_get_ecg",
        "No ECG readings found for this period.",
        "ECG readings are taken by hand on a supported watch.",
    ),
    (
        rhythm_tools,
        "health_get_irregular_rhythm",
        "No irregular rhythm notifications found for this period.",
        "These are raised by the watch and are rare.",
    ),
    (sleep_tools, "health_get_sleep", "No sleep data found for this period.", None),
    (sleep_tools, "health_get_sleep_sessions", "No sleep sessions found for this period.", None),
    (spo2_tools, "health_get_spo2", "No SpO2 data found for this period.", None),
    (
        temperature_tools,
        "health_get_skin_temperature",
        "No skin temperature data found for this period.",
        "Requires sleep tracking and baseline.",
    ),
    (
        temperature_tools,
        "health_get_core_temperature",
        "No core temperature data found for this period.",
        "Core temperature is only present when readings are logged manually in the app.",
    ),
    (weight_tools, "health_get_weight", "No weight data found for this period.", None),
    (
        weight_tools,
        "health_get_weight_readings",
        "No weight or body-fat readings found for this period.",
        None,
    ),
    (weight_tools, "health_get_height", "No height reading found for this period.", None),
    (
        profile_tools,
        "health_get_profile",
        "No profile or settings have been fetched yet.",
        None,
    ),
]

_IDS = [name for _module, name, _message, _note in _TOOLS]

#: Tools that take `live` and are deliberately not in the list above.
_NOT_LISTED = {"health_get_food_log"}


async def test_every_tool_with_a_live_flag_is_listed_or_excused():
    from google_health_mcp import cli  # noqa: F401  (registers every tool)
    from google_health_mcp.mcp_instance import mcp

    tools = await mcp.list_tools()
    live = {t.name for t in tools if "live" in t.input_schema.get("properties", {})}
    assert live, "no tool takes live, so this proves nothing"
    assert live == set(_IDS) | _NOT_LISTED


async def _empty_answer(module, name, live):
    """The tool's answer over an empty cache, with the refresh stubbed out."""
    with (
        patch.object(helpers, "GOOGLE_CLIENT_PATH") as client,
        patch.object(helpers, "GOOGLE_TOKENS_PATH") as tokens,
        patch.object(module, "refresh_before_query") as refresh,
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        said = json.loads(await getattr(module, name)(live=live))
    assert refresh.called, "the tool never reached its refresh, so this proves nothing"
    return said


@pytest.mark.parametrize(("module", "name", "message", "note"), _TOOLS, ids=_IDS)
async def test_an_empty_live_answer_does_not_offer_the_call_just_made(module, name, message, note):
    said = await _empty_answer(module, name, live=True)
    assert said == ({"message": message, "hint": note} if note else {"message": message})


@pytest.mark.parametrize(("module", "name", "message", "note"), _TOOLS, ids=_IDS)
async def test_an_empty_cache_answer_offers_a_live_call(module, name, message, note):
    said = await _empty_answer(module, name, live=False)
    hint = f"{note} {helpers.LIVE_HINT}" if note else helpers.LIVE_HINT
    assert said == {"message": message, "hint": hint}


@pytest.mark.parametrize(
    ("live", "note", "expected"),
    [
        (False, None, {"message": "m", "hint": helpers.LIVE_HINT}),
        (False, "n.", {"message": "m", "hint": f"n. {helpers.LIVE_HINT}"}),
        (True, None, {"message": "m"}),
        (True, "n.", {"message": "m", "hint": "n."}),
    ],
)
def test_the_helper_says_the_note_always_and_the_hint_only_after_a_cache_read(live, note, expected):
    assert json.loads(helpers.empty_answer("m", live, note)) == expected
