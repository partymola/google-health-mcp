"""An exercise_type matching no cached name is refused, not answered empty.

`health_get_exercises(exercise_type="swimmin")` came back as "No exercise
entries found for this period", which says the workouts are not there rather
than that the filter is one the cache cannot use. A model reads the first as
an answer about the person and stops.

Google names the workouts, so the accepted values are whatever the cache
holds and no schema can carry them. The fold therefore happens in Python
rather than in SQL: `LOWER` covers ASCII alone, so a name outside it would
pass the match here and match nothing in the query.

The refusal says the cache rather than the person, and keeps the `live=True`
hint, because an absent name can be a window that was never synced.

The calls go through `mcp.call_tool`, the layer a model reaches, and the
refusal arrives as a `ToolError` because `require_auth` converts what this
package raises deliberately.
"""

import asyncio
import json
import sqlite3

import pytest

from google_health_mcp import config, db, helpers
from google_health_mcp.mcp_instance import mcp
from google_health_mcp.tools import exercise_tools

# Seeded one per day from 2026-03-10. "LÄUFEN" is stored in upper case on
# purpose: SQLite's LOWER leaves the umlaut alone, so it is asking for that
# name in lower case that tells a Python fold from a SQL one. A pair that
# differs only in ASCII letters passes either way.
LOGGED = ("Cycling", "Trail Run", "LÄUFEN")
WINDOW = ("2026-03-10", "2026-03-14")
FIRST_DAY_ONLY = ("2026-03-10", "2026-03-10")


class _Exists:
    """A credential path `require_auth` is satisfied by."""

    def exists(self):
        return True


def seeded_db(names=LOGGED):
    """A cache holding one workout per name, on fixed past dates.

    `check_same_thread=False` because the tool body runs in a worker thread
    via anyio while this connection is made on the test's.
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA)
    for i, name in enumerate(names):
        db.save_exercise(
            conn,
            f"log{i}",
            {
                "date": f"2026-03-{10 + i:02d}",
                "name": name,
                "duration_min": 30,
                "start_time": f"2026-03-{10 + i:02d}T08:00:00",
                "provider": "google",
            },
        )
    conn.commit()
    return conn


def call(args, names=LOGGED):
    """Call the registered tool, returning (raised_message, parsed_result)."""
    conn = seeded_db(names)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(helpers, "GOOGLE_CLIENT_PATH", _Exists())
            mp.setattr(helpers, "GOOGLE_TOKENS_PATH", _Exists())
            mp.setattr(exercise_tools.db, "get_db", lambda *a, **k: conn)
            mp.setattr(exercise_tools, "refresh_before_query", lambda *a, **k: None)
            try:
                result = asyncio.run(mcp.call_tool("health_get_exercises", args))
            except Exception as e:
                # The message is what is under test, so every type is caught.
                return str(e), None
    finally:
        conn.close()
    text = "".join(c.text for c in result.content if getattr(c, "text", None))
    return None, json.loads(text)


def window(**extra):
    return {"start_date": WINDOW[0], "end_date": WINDOW[1], **extra}


def test_the_fixture_holds_the_names_these_tests_use():
    """Every assertion below is vacuous against a cache without them.

    The names are written out rather than read from `LOGGED`, which seeds the
    cache: comparing the fixture with itself passes however it is narrowed.
    """
    _, data = call(window())
    assert {e["name"] for e in data["exercises"]} == {"Cycling", "Trail Run", "LÄUFEN"}


@pytest.mark.parametrize("value", ["swimmin", "rowing", "not-a-workout"])
def test_a_type_matching_no_cached_name_is_refused(value):
    raised, data = call(window(exercise_type=value))
    assert raised is not None, f"exercise_type={value!r} was answered rather than refused: {data}"
    assert value in raised


def test_the_refusal_names_what_the_cache_holds():
    """A refusal the model cannot act on leaves it guessing at the vocabulary.

    The names are written out rather than read from `LOGGED`, which seeds the
    cache: iterating that compares the fixture with itself.
    """
    raised, _ = call(window(exercise_type="swimmin"))
    for name in ("Cycling", "Trail Run", "LÄUFEN"):
        assert name in raised, f"the refusal did not name {name!r}: {raised!r}"


def test_the_refusal_is_about_the_cache_and_keeps_the_way_out():
    """An absent name can be an unsynced window rather than a workout never done.

    `auto_sync_if_stale` swallows its failures, so the cache is not evidence
    about the person. A refusal saying "you have not logged that" makes the
    same false claim the empty answer did, and dropping the hint removes the
    only route to the answer, so both halves are asserted.
    """
    raised, _ = call(window(exercise_type="swimmin"))
    assert "cache" in raised.casefold(), raised
    assert "logged" not in raised.casefold(), raised
    assert "live=True" in raised, raised


def test_a_filter_value_reaches_the_refusal_unaltered():
    """The hint is appended rather than substituted into a finished message.

    A rewrite over the whole string fires inside the echoed filter value too,
    so a caller sending the hint's own wording got it replaced mid-quote.
    """
    raised, _ = call(window(exercise_type=helpers.LIVE_HINT))
    assert raised.count(helpers.LIVE_HINT) == 2, raised


@pytest.mark.parametrize("value", ["cycling", "CYCLING", "cycl"])
def test_a_substring_in_another_case_still_answers(value):
    raised, data = call(window(exercise_type=value))
    assert raised is None, raised
    assert [e["name"] for e in data["exercises"]] == ["Cycling"]


def test_a_name_outside_ascii_answers_in_another_case():
    """The match and the query must fold the same way, which SQL cannot do.

    `LOWER` in SQLite folds ASCII alone, so a query written that way over the
    caller's own spelling accepts this filter and then returns nothing, which
    is the empty result the refusal exists to replace.
    """
    raised, data = call(window(exercise_type="läufen"))
    assert raised is None, raised
    assert [e["name"] for e in data["exercises"]] == ["LÄUFEN"]


def test_a_filter_matching_several_names_answers_for_all_of_them():
    """A substring is not one name, and narrowing to the first would drop rows."""
    raised, data = call(window(exercise_type="n"))
    assert raised is None, raised
    assert sorted(e["name"] for e in data["exercises"]) == sorted(LOGGED)


def test_a_cached_name_outside_the_window_is_an_empty_answer_not_a_refusal():
    """The vocabulary is the whole cache; the window is a separate question.

    Refusing here would tell the model the workout does not exist when it
    simply falls outside the dates it asked about. The window holds another
    workout on purpose: with an empty one the vocabulary is empty too, and a
    version reading only the window would refuse nothing and look correct.
    """
    raised, data = call(
        {
            "start_date": FIRST_DAY_ONLY[0],
            "end_date": FIRST_DAY_ONLY[1],
            "exercise_type": "läufen",
        }
    )
    assert raised is None, raised
    assert "message" in data


def test_the_refusal_does_not_offer_a_live_fetch_offline(monkeypatch):
    """Offline mode cannot honour `live=True`, and a refusal never passes the
    response path that rewrites that hint.

    Found by calling the registered tool on a reader host, where offline mode
    is on: the empty answer explained itself and the refusal told the model to
    do something the process refuses.
    """
    monkeypatch.setattr(config, "OFFLINE_MODE", True)

    raised, _ = call(window(exercise_type="swimmin"))
    assert "live=True" not in raised, raised
    assert "GOOGLE_HEALTH_MCP_OFFLINE" in raised, raised
    # The rest of the message has to survive the swap. Asserting only that the
    # hint changed passes against a version that answers with the hint alone,
    # which drops the filter value and every name the caller needs.
    assert "swimmin" in raised, raised
    assert "Cycling" in raised, raised


def test_a_workout_with_no_name_does_not_break_the_filter():
    """A row can reach the cache unnamed, and one is enough to mask every call.

    `google_sync` writes `displayName or exerciseType`, and both can be
    absent. Without the guard in `exercise_names` the fold raises
    `AttributeError`, which is not a `GoogleHealthError`, so every filtered
    call comes back as `Error executing tool health_get_exercises`.
    """
    # Built by the same call as the one under test, because an omitted column
    # and an explicit None are different instructions to the upsert: asserting
    # on a hand-built row would be a claim about a database nothing exercises.
    conn = seeded_db(LOGGED + (None,))
    rows = db.query_exercises(conn, WINDOW[0], WINDOW[1])
    conn.close()
    assert any(r["name"] is None for r in rows), "the fixture holds no unnamed workout"

    raised, data = call(window(exercise_type="cycl"), names=LOGGED + (None,))
    assert raised is None, raised
    assert [e["name"] for e in data["exercises"]] == ["Cycling"]


def test_an_empty_filter_narrows_nothing():
    """Every name contains the empty string, so this is not a refusal.

    Pinned because the obvious tidy-up, `if not exercise_type`, would turn it
    into either a silent no-op or a refusal without anyone deciding.
    """
    raised, data = call(window(exercise_type=""))
    assert raised is None, raised
    assert data["count"] == 3


@pytest.mark.parametrize("value", ["%", "Trail_Run"])
def test_a_wildcard_is_an_ordinary_character(value):
    """The old SQL LIKE read `%` and `_` as wildcards; the new match does not."""
    raised, _ = call(window(exercise_type=value))
    assert raised is not None, f"{value!r} was read as a pattern rather than as text"


def test_two_workouts_sharing_a_name_are_both_returned():
    """Names are how rows are selected, so de-duplicating by name loses one.

    A fixture of unique names cannot see that: comparing the set of names
    returned passes with either count.
    """
    raised, data = call(window(exercise_type="cycl"), names=("Cycling", "Cycling"))
    assert raised is None, raised
    assert data["count"] == 2


def test_an_empty_cache_is_not_a_refusal():
    """With an empty cache there is no vocabulary and nothing to name.

    Refusing would report a filter problem when the real one is that nothing
    has been synced yet, which the existing message already says.
    """
    raised, data = call(window(exercise_type="swimmin"), names=())
    assert raised is None, raised
    assert "message" in data
