"""Sleep: one row a night, from however many sessions the night was recorded as.

Every session sharing a date is aggregated into one row, because the table is
keyed by date and the last session written would otherwise silently replace a
fragmented night with its shortest piece.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync


def _session(
    start="2026-03-15T23:10:00Z",
    end="2026-03-16T06:40:00Z",
    offset="3600s",
    asleep="430",
    in_period="480",
    stages=(("DEEP", "70"), ("LIGHT", "260"), ("REM", "100"), ("AWAKE", "40")),
    nap=None,
):
    metadata = {"stagesStatus": "SUCCEEDED"}
    if nap is not None:
        metadata["nap"] = nap
    else:
        metadata["mainSleep"] = True
    return {
        "sleep": {
            "interval": {
                "startTime": start,
                "endTime": end,
                "startUtcOffset": offset,
                "endUtcOffset": offset,
            },
            "metadata": metadata,
            "summary": {
                "minutesAsleep": asleep,
                "minutesInSleepPeriod": in_period,
                "stagesSummary": [{"type": t, "minutes": m} for t, m in stages],
            },
        }
    }


@pytest.fixture
def sync_sleep(tmp_db):
    def run(points):
        with patch.object(google_sync.api, "list_google_data_points", return_value=points):
            count = google_sync.sync_sleep(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        return count

    return run


class TestTheNightItBelongsTo:
    def test_a_night_is_dated_by_when_it_ended_locally(self, tmp_db, sync_sleep):
        """A night starting on the 15th and ending on the 16th is the 16th.

        The response carries no civil end time, so the local date has to come
        from the UTC end plus its offset. Using the start would move every
        night that crosses midnight to the day before.
        """
        sync_sleep([_session()])
        rows = db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")
        assert [r["date"] for r in rows] == ["2026-03-16"]

    def test_the_offset_is_applied_rather_than_ignored(self, tmp_db, sync_sleep):
        """23:30 UTC with a +1h offset is 00:30 the next day, locally."""
        sync_sleep([_session(end="2026-03-15T23:30:00Z", offset="3600s")])
        assert db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]["date"] == "2026-03-16"

    def test_a_session_with_no_end_is_skipped(self, tmp_db, sync_sleep):
        assert sync_sleep([_session(end=None)]) == 0


class TestFragmentedNights:
    def test_sessions_sharing_a_night_are_summed(self, tmp_db, sync_sleep):
        """Otherwise the last one written replaces the night with its shortest piece."""
        sync_sleep(
            [
                _session(asleep="200", in_period="220", stages=(("DEEP", "40"), ("LIGHT", "160"))),
                _session(asleep="230", in_period="260", stages=(("DEEP", "30"), ("LIGHT", "200"))),
            ]
        )
        row = db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]
        assert row["total_minutes"] == 430
        assert row["deep_minutes"] == 70
        assert row["light_minutes"] == 360
        assert row["sessions"] == 2

    def test_the_span_covers_every_session(self, tmp_db, sync_sleep):
        sync_sleep(
            [
                _session(start="2026-03-15T23:10:00Z", end="2026-03-16T02:00:00Z"),
                _session(start="2026-03-16T03:00:00Z", end="2026-03-16T06:40:00Z"),
            ]
        )
        row = db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]
        assert row["start_time"] == "2026-03-15T23:10:00Z"
        assert row["end_time"] == "2026-03-16T06:40:00Z"

    def test_a_nap_counts_as_a_session_of_its_night(self, tmp_db, sync_sleep):
        """Every session sharing a date is one night's row, naps included."""
        sync_sleep(
            [
                _session(asleep="400"),
                _session(
                    start="2026-03-16T13:00:00Z",
                    end="2026-03-16T13:30:00Z",
                    asleep="30",
                    in_period="30",
                    stages=(("LIGHT", "30"),),
                    nap=True,
                ),
            ]
        )
        row = db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]
        assert row["total_minutes"] == 430
        assert row["sessions"] == 2

    def test_one_session_is_not_flagged_as_split(self, tmp_db, sync_sleep):
        sync_sleep([_session()])
        assert db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]["sessions"] == 1


class TestWhatHasNoSource:
    def test_efficiency_is_never_written(self, tmp_db, sync_sleep):
        """Google reports nothing corresponding to sleep efficiency.

        Computing a lookalike into a column that already holds a reported one
        would corrupt the series rather than extend it.
        """
        db.save_sleep(tmp_db, {"date": "2026-03-16", "efficiency": 91})
        tmp_db.commit()
        sync_sleep([_session()])
        row = db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]
        assert row["efficiency"] == 91
        assert row["total_minutes"] == 430

    def test_the_sleep_period_is_stored_as_its_own_column(self, tmp_db, sync_sleep):
        """minutesInSleepPeriod is not time in bed, so it gets a column of its own."""
        sync_sleep([_session(in_period="480")])
        assert db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]["sleep_period_minutes"] == 480

    def test_a_stage_the_night_lacks_stays_absent(self, tmp_db, sync_sleep):
        """A CLASSIC night has no stages, which is not the same as no deep sleep."""
        sync_sleep([_session(stages=())])
        row = db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]
        assert row["deep_minutes"] is None
        assert row["total_minutes"] == 430


class TestAnUnreadableOffset:
    def test_a_malformed_offset_skips_the_night(self, tmp_db, sync_sleep):
        """Defaulting to UTC would move a whole night into the wrong day."""
        assert sync_sleep([_session(offset="not-an-offset")]) == 0

    def test_a_negative_offset_moves_the_night_earlier(self, tmp_db, sync_sleep):
        sync_sleep([_session(end="2026-03-16T02:00:00Z", offset="-18000s")])
        assert db.query_sleep(tmp_db, "2026-03-01", "2026-04-01")[0]["date"] == "2026-03-15"
