"""ECG and irregular-rhythm readings, and the waveform that must not travel by default.

A 30-second lead-I trace is thousands of voltages. Storing it is the point -
nothing else in this package holds one - but handing it to a model unasked is
a useless answer and an expensive one, so the tools return the summary and the
samples only on request.
"""

import json
from unittest.mock import patch

import pytest

from google_health_mcp import db as db_mod
from google_health_mcp.config import CACHED_DATA_TYPES
from google_health_mcp.tools.rhythm_tools import health_get_ecg, health_get_irregular_rhythm

# Fictional: a flat trace nobody could mistake for a real reading.
_WAVEFORM = [0, 12, -12, 40, -40, 0]


@pytest.fixture
def rhythm_db(tmp_db):
    """One ECG reading with a trace, one without, and one alert."""
    db_mod.save_ecg(
        tmp_db,
        {
            "reading_id": "users/me/dataPoints/ecg-one",
            "date": "2026-03-10",
            "start_time": "2026-03-10T08:00:00Z",
            "avg_bpm": 62,
            "classification": "NORMAL_SINUS_RHYTHM",
            "lead_number": 1,
            "sampling_hz": 250,
            "scaling_factor": 1000,
            "duration_sec": 30.0,
            "waveform": json.dumps(_WAVEFORM),
            "device_model": "Fictional Watch 3",
            "provider": "google",
        },
    )
    db_mod.save_ecg(
        tmp_db,
        {
            "reading_id": "users/me/dataPoints/ecg-two",
            "date": "2026-03-11",
            "start_time": "2026-03-11T09:00:00Z",
            "avg_bpm": 71,
            "classification": "INCONCLUSIVE_LOW_HEART_RATE",
            "lead_number": 1,
            "sampling_hz": 250,
            "scaling_factor": 1000,
            "duration_sec": None,
            "waveform": None,
            "device_model": "Fictional Watch 3",
            "provider": "google",
        },
    )
    db_mod.save_irn(
        tmp_db,
        {
            "alert_id": "users/me/dataPoints/irn-one",
            "date": "2026-03-12",
            "start_time": "2026-03-12T01:00:00Z",
            "end_time": "2026-03-12T05:30:00Z",
            "alert_windows": json.dumps(
                [{"startTime": "2026-03-12T01:00:00Z", "endTime": "2026-03-12T01:20:00Z"}]
            ),
            "device_model": "Fictional Watch 3",
            "provider": "google",
        },
    )
    tmp_db.commit()
    return tmp_db


async def _call(tool, db_path, **kwargs):
    """Run a tool against a prepared database, with the credential gate satisfied."""
    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch("google_health_mcp.tools.rhythm_tools.refresh_before_query"),
        patch.object(db_mod, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        return json.loads(await tool(**kwargs))


class TestTheWaveformStaysBehindItsFlag:
    async def test_no_reading_in_a_window_carries_its_samples(self, rhythm_db, tmp_path):
        """Across every reading, not the first: a response holds a window of them."""
        db_mod.save_ecg(
            rhythm_db,
            {
                "reading_id": "users/me/dataPoints/ecg-three",
                "date": "2026-03-10",
                "start_time": "2026-03-10T18:00:00Z",
                "waveform": json.dumps(_WAVEFORM),
            },
        )
        rhythm_db.commit()

        parsed = await _call(
            health_get_ecg,
            tmp_path / "test_google_health.db",
            start_date="2026-03-10",
            end_date="2026-03-10",
        )
        assert parsed["count"] == 2
        assert all("waveform" not in r for r in parsed["ecg"])
        assert all(r["waveform_samples"] == len(_WAVEFORM) for r in parsed["ecg"])
        assert parsed["ecg"][0]["classification"] == "NORMAL_SINUS_RHYTHM"
        assert parsed["ecg"][0]["avg_bpm"] == 62

    async def test_the_sample_count_says_a_trace_is_there_to_ask_for(self, rhythm_db, tmp_path):
        parsed = await _call(
            health_get_ecg,
            tmp_path / "test_google_health.db",
            start_date="2026-03-10",
            end_date="2026-03-10",
        )
        assert parsed["ecg"][0]["waveform_samples"] == len(_WAVEFORM)

    async def test_asking_for_it_returns_every_reading_s_samples(self, rhythm_db, tmp_path):
        """Across the window, not the first reading: both halves of the flag."""
        db_mod.save_ecg(
            rhythm_db,
            {
                "reading_id": "users/me/dataPoints/ecg-four",
                "date": "2026-03-10",
                "start_time": "2026-03-10T20:00:00Z",
                "waveform": json.dumps(_WAVEFORM),
            },
        )
        rhythm_db.commit()

        parsed = await _call(
            health_get_ecg,
            tmp_path / "test_google_health.db",
            start_date="2026-03-10",
            end_date="2026-03-10",
            include_waveform=True,
        )
        assert parsed["count"] == 2
        assert all(r["waveform"] == _WAVEFORM for r in parsed["ecg"])

    async def test_a_reading_with_no_trace_reports_absence_rather_than_zero(
        self, rhythm_db, tmp_path
    ):
        """Nothing stored is not a trace of length nought, and a count of 0 would say it was."""
        parsed = await _call(
            health_get_ecg,
            tmp_path / "test_google_health.db",
            start_date="2026-03-11",
            end_date="2026-03-11",
            include_waveform=True,
        )
        reading = parsed["ecg"][0]
        assert reading["waveform_samples"] is None
        assert reading["waveform"] is None


class TestAnUnreadableStoredValue:
    """One bad row must not cost the caller the rest of the window."""

    async def test_a_trace_that_will_not_parse_counts_as_unknown(self, rhythm_db, tmp_path):
        rhythm_db.execute("UPDATE ecg SET waveform = ? WHERE date = ?", ("[1, 2,", "2026-03-10"))
        rhythm_db.commit()

        parsed = await _call(
            health_get_ecg,
            tmp_path / "test_google_health.db",
            start_date="2026-03-10",
            end_date="2026-03-11",
            include_waveform=True,
        )
        assert parsed["count"] == 2
        assert parsed["ecg"][0]["waveform_samples"] is None
        assert parsed["ecg"][0]["waveform"] is None
        assert parsed["ecg"][0]["classification"] == "NORMAL_SINUS_RHYTHM"

    async def test_alert_windows_that_will_not_parse_read_as_absent(self, rhythm_db, tmp_path):
        rhythm_db.execute("UPDATE irn SET alert_windows = ?", ("{not json",))
        rhythm_db.commit()

        parsed = await _call(
            health_get_irregular_rhythm,
            tmp_path / "test_google_health.db",
            start_date="2026-03-12",
            end_date="2026-03-12",
        )
        assert parsed["irregular_rhythm"][0]["alert_windows"] is None
        assert parsed["irregular_rhythm"][0]["start_time"] == "2026-03-12T01:00:00Z"


class TestTheRhythmTools:
    async def test_ecg_reads_the_window_it_was_given(self, rhythm_db, tmp_path):
        parsed = await _call(
            health_get_ecg,
            tmp_path / "test_google_health.db",
            start_date="2026-03-10",
            end_date="2026-03-11",
        )
        assert parsed["count"] == 2
        assert [r["date"] for r in parsed["ecg"]] == ["2026-03-10", "2026-03-11"]

    async def test_an_empty_window_says_so(self, rhythm_db, tmp_path):
        parsed = await _call(
            health_get_ecg,
            tmp_path / "test_google_health.db",
            start_date="2026-02-01",
            end_date="2026-02-28",
        )
        assert "No ECG" in parsed["message"]

    async def test_an_alert_carries_its_windows_decoded(self, rhythm_db, tmp_path):
        parsed = await _call(
            health_get_irregular_rhythm,
            tmp_path / "test_google_health.db",
            start_date="2026-03-12",
            end_date="2026-03-12",
        )
        alert = parsed["irregular_rhythm"][0]
        assert alert["alert_windows"] == [
            {"startTime": "2026-03-12T01:00:00Z", "endTime": "2026-03-12T01:20:00Z"}
        ]
        assert alert["end_time"] == "2026-03-12T05:30:00Z"

    async def test_an_alert_with_no_windows_stays_absent(self, rhythm_db, tmp_path):
        db_mod.save_irn(
            rhythm_db,
            {
                "alert_id": "users/me/dataPoints/irn-two",
                "date": "2026-03-13",
                "start_time": "2026-03-13T02:00:00Z",
                "end_time": None,
                "alert_windows": None,
                "device_model": "Fictional Watch 3",
                "provider": "google",
            },
        )
        rhythm_db.commit()
        parsed = await _call(
            health_get_irregular_rhythm,
            tmp_path / "test_google_health.db",
            start_date="2026-03-13",
            end_date="2026-03-13",
        )
        assert parsed["irregular_rhythm"][0]["alert_windows"] is None

    async def test_an_empty_alert_window_says_so(self, rhythm_db, tmp_path):
        parsed = await _call(
            health_get_irregular_rhythm,
            tmp_path / "test_google_health.db",
            start_date="2026-02-01",
            end_date="2026-02-28",
        )
        assert "No irregular rhythm" in parsed["message"]


class TestTheLiveFlagReachesTheSync:
    """Both tools sit behind the one refresh path, and `live` is what it acts on."""

    @pytest.mark.parametrize(
        "tool,data_type",
        [(health_get_ecg, "ecg"), (health_get_irregular_rhythm, "irn")],
    )
    async def test_the_window_and_the_flag_are_passed_through(
        self, rhythm_db, tmp_path, tool, data_type
    ):
        seen = []
        with (
            patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
            patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
            patch(
                "google_health_mcp.tools.rhythm_tools.refresh_before_query",
                # **k as well as *a: the same call written with keywords is
                # the same call, and a TypeError here would read as a defect.
                lambda *a, **k: seen.append(a + tuple(k.values())),
            ),
            patch.object(db_mod, "DB_PATH", tmp_path / "test_google_health.db"),
        ):
            client.exists.return_value = True
            tokens.exists.return_value = True
            await tool(start_date="2026-03-10", end_date="2026-03-12", live=True)

        assert len(seen) == 1
        dtype, start, end, live = seen[0]
        assert dtype == data_type
        # Against the cached types, not a literal: a type nothing syncs is
        # swallowed by auto-sync and serves a cache that never refreshes.
        assert dtype in CACHED_DATA_TYPES
        assert (start.isoformat(), end.isoformat()) == ("2026-03-10", "2026-03-12")
        assert live is True
