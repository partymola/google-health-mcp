"""Each sleep session, stored whole as Google sent it.

The nightly `sleep` row sums a night's sessions and keeps the stage totals.
Everything else a session carries arrives only here: every stage segment,
short awakening and out-of-bed segment, and Google's own metadata and summary.
Nothing in a session is computed: the record is the point as it came.
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync

# Fictional throughout.
_SEGMENT = {
    "startTime": "2026-03-15T00:00:00Z",
    "startUtcOffset": "0s",
    "endTime": "2026-03-15T00:30:00Z",
    "endUtcOffset": "0s",
    "type": "LIGHT",
}


def _session(name="users/me/dataTypes/sleep/dataPoints/1", end="2026-03-15T07:00:00Z", offset="0s"):
    return {
        "name": name,
        "dataSource": {"platform": "FITBIT", "recordingMethod": "PASSIVELY_MEASURED"},
        "sleep": {
            "interval": {
                "startTime": "2026-03-14T23:00:00Z",
                "startUtcOffset": offset,
                "endTime": end,
                "endUtcOffset": offset,
            },
            "type": "STAGES",
            "metadata": {"mainSleep": True, "processed": True, "stagesStatus": "SUCCEEDED"},
            "summary": {"minutesAsleep": "400", "minutesAwake": "20"},
            "stages": [_SEGMENT, {**_SEGMENT, "type": "DEEP"}],
            "shortAwakenings": [{**_SEGMENT, "type": "AWAKE"}],
            "outOfBedSegments": [],
        },
    }


def _sync(tmp_db, points):
    with patch.object(
        google_sync.api,
        "list_google_data_points",
        side_effect=lambda t, *_: points if t == "sleep" else [],
    ):
        count = google_sync.sync_sleep_sessions(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
    tmp_db.commit()
    return count


class TestEachSessionIsStoredWhole:
    def test_the_record_is_the_point_as_google_sent_it(self, tmp_db):
        point = _session()
        assert _sync(tmp_db, [point]) == 1
        (row,) = db.query_sleep_sessions(tmp_db, "2026-03-15", "2026-03-15")
        assert row["session_id"] == point["name"]
        assert row["start_time"] == "2026-03-14T23:00:00Z"
        assert row["end_time"] == "2026-03-15T07:00:00Z"
        assert row["record"] == point
        assert row["provider"] == "google"

    def test_a_session_is_dated_by_its_local_end(self, tmp_db):
        """The same rule the nightly row follows, so the two agree on a night."""
        _sync(tmp_db, [_session(end="2026-03-15T23:30:00Z", offset="3600s")])
        (row,) = db.query_sleep_sessions(tmp_db, "2026-03-16", "2026-03-16")
        assert row["date"] == "2026-03-16"

    def test_two_sessions_on_one_night_are_two_rows(self, tmp_db):
        """The nightly row sums them; here each stays itself."""
        _sync(
            tmp_db,
            [_session(), _session(name="users/me/dataTypes/sleep/dataPoints/2")],
        )
        assert len(db.query_sleep_sessions(tmp_db, "2026-03-15", "2026-03-15")) == 2

    def test_a_re_sync_replaces_the_record(self, tmp_db):
        """Google revises a session after the night; the latest is what it holds."""
        _sync(tmp_db, [_session()])
        revised = _session()
        revised["sleep"]["summary"]["minutesAsleep"] = "410"
        _sync(tmp_db, [revised])
        (row,) = db.query_sleep_sessions(tmp_db, "2026-03-15", "2026-03-15")
        assert row["record"]["sleep"]["summary"]["minutesAsleep"] == "410"

    @pytest.mark.parametrize(
        "point",
        [
            {**_session(), "name": None},
            {**_session(), "name": 7},
            {"name": "users/me/dataTypes/sleep/dataPoints/1", "sleep": "not an object"},
            {**_session(), "sleep": {**_session()["sleep"], "interval": {}}},
        ],
        ids=["no-name", "numeric-name", "no-session", "no-end"],
    )
    def test_a_session_that_cannot_be_keyed_or_dated_is_skipped(self, tmp_db, point):
        assert _sync(tmp_db, [point]) == 0
        assert db.query_sleep_sessions(tmp_db, "2026-03-01", "2026-04-01") == []

    def test_an_unreadable_stored_record_reads_as_absent(self, tmp_db):
        db.save_sleep_session(
            tmp_db,
            {
                "session_id": "users/me/dataTypes/sleep/dataPoints/1",
                "date": "2026-03-15",
                "record": "{not json",
            },
        )
        tmp_db.commit()
        (row,) = db.query_sleep_sessions(tmp_db, "2026-03-15", "2026-03-15")
        assert row["record"] is None


async def _call(db_path, **kwargs):
    from google_health_mcp.tools.sleep_tools import health_get_sleep_sessions

    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch("google_health_mcp.tools.sleep_tools.refresh_before_query") as refresh,
        patch.object(db, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        body = json.loads(await health_get_sleep_sessions(start_date="2026-03-15", **kwargs))
    assert {c.args[0] for c in refresh.call_args_list} == {"sleep_sessions"}
    return body


class TestTheTool:
    @pytest.fixture
    def db_path(self, tmp_db):
        _sync(tmp_db, [_session()])
        return Path(tmp_db.execute("PRAGMA database_list").fetchone()[2])

    async def test_the_segments_stay_behind_their_flag(self, db_path):
        """Dozens of segments a night is a large answer to an ordinary question."""
        body = await _call(db_path)
        (session,) = body["sleep_sessions"]
        assert "stages" not in session["record"]["sleep"]
        assert "shortAwakenings" not in session["record"]["sleep"]
        assert session["segment_counts"] == {
            "stages": 2,
            "shortAwakenings": 1,
            "outOfBedSegments": 0,
        }
        assert session["record"]["sleep"]["metadata"]["mainSleep"] is True

    async def test_the_segments_come_back_when_asked_for(self, db_path):
        body = await _call(db_path, include_stages=True)
        (session,) = body["sleep_sessions"]
        assert session["record"]["sleep"]["stages"][1]["type"] == "DEEP"
        assert session["record"]["sleep"]["outOfBedSegments"] == []

    async def test_an_unreadable_record_is_returned_rather_than_failing_the_window(self, tmp_db):
        db.save_sleep_session(
            tmp_db, {"session_id": "users/me/dataTypes/sleep/dataPoints/9", "date": "2026-03-15"}
        )
        _sync(tmp_db, [_session()])
        body = await _call(Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]))
        assert body["count"] == 2

    async def test_an_empty_window_says_so(self, tmp_path):
        body = await _call(tmp_path / "empty.db")
        assert body["message"] == "No sleep sessions found for this period."
