"""ECG and irregular-rhythm notifications, stored whole.

A reading is thousands of voltages plus the numbers that make them readable -
the sampling frequency and the scaling factor - so it is stored whole. Without
either of those the samples are an array of meaningless integers.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync


def _ecg(
    name="users/me/dataTypes/electrocardiogram/dataPoints/fake-1",
    start="2026-03-15T08:00:00Z",
    offset="3600s",
    samples=(975, 1224, 1382),
    hz=250,
    scaling=10922,
    bpm="71",
    classification="NORMAL_SINUS_RHYTHM",
    device="Fictional Watch",
):
    reading = {
        "interval": {"startTime": start, "startUtcOffset": offset},
        "leadNumber": 1,
        "resultClassification": classification,
    }
    if samples is not None:
        reading["waveformSamples"] = list(samples)
    if hz is not None:
        reading["samplingFrequencyHertz"] = hz
    if scaling is not None:
        reading["millivoltsScalingFactor"] = scaling
    if bpm is not None:
        reading["beatsPerMinuteAvg"] = bpm
    if device is not None:
        reading["medicalDeviceInfo"] = {"deviceModel": device}
    point = {"electrocardiogram": reading}
    if name is not None:
        point["name"] = name
    return point


@pytest.fixture
def sync(tmp_db):
    def run(handler, points):
        with patch.object(google_sync.api, "list_google_data_points", return_value=points):
            count = handler(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
        tmp_db.commit()
        return count

    return run


class TestTheReading:
    def test_a_reading_is_stored_whole(self, tmp_db, sync):
        sync(google_sync.sync_ecg, [_ecg()])
        row = db.query_ecg(tmp_db, "2026-03-01", "2026-04-01", include_waveform=True)[0]
        assert row["classification"] == "NORMAL_SINUS_RHYTHM"
        assert row["avg_bpm"] == 71
        assert row["sampling_hz"] == 250
        assert row["scaling_factor"] == 10922
        assert row["waveform"] == [975, 1224, 1382]

    def test_the_duration_comes_from_the_samples(self, tmp_db, sync):
        """The interval carries only a start, so the length is not in it.

        7500 samples at 250 Hz is thirty seconds. Storing it means nothing
        downstream has to know where it came from.
        """
        sync(google_sync.sync_ecg, [_ecg(samples=range(7500), hz=250)])
        assert db.query_ecg(tmp_db, "2026-03-01", "2026-04-01")[0]["duration_sec"] == 30.0

    def test_without_a_frequency_there_is_no_duration(self, tmp_db, sync):
        """Dividing by an absent rate would invent a number."""
        sync(google_sync.sync_ecg, [_ecg(hz=None)])
        assert db.query_ecg(tmp_db, "2026-03-01", "2026-04-01")[0]["duration_sec"] is None

    def test_the_classification_is_kept_as_given(self, tmp_db, sync):
        """Four of the eight values distinguish kinds of inconclusive.

        Reducing them to normal-or-not would throw away why a reading could
        not be classified, which is the part a person acts on.
        """
        sync(google_sync.sync_ecg, [_ecg(classification="INCONCLUSIVE_HIGH_HEART_RATE")])
        assert db.query_ecg(tmp_db, "2026-03-01", "2026-04-01")[0]["classification"] == (
            "INCONCLUSIVE_HIGH_HEART_RATE"
        )

    def test_a_reading_with_no_identifier_is_skipped(self, tmp_db, sync):
        assert sync(google_sync.sync_ecg, [_ecg(name=None)]) == 0

    def test_the_device_comes_from_the_medical_device_block(self, tmp_db, sync):
        """ECG points carry an empty dataSource, unlike every other type."""
        sync(google_sync.sync_ecg, [_ecg(device="Fictional Watch")])
        assert db.query_ecg(tmp_db, "2026-03-01", "2026-04-01")[0]["device_model"] == (
            "Fictional Watch"
        )

    def test_re_syncing_corrects_rather_than_duplicating(self, tmp_db, sync):
        sync(google_sync.sync_ecg, [_ecg(bpm="71")])
        sync(google_sync.sync_ecg, [_ecg(bpm="73")])
        rows = db.query_ecg(tmp_db, "2026-03-01", "2026-04-01")
        assert len(rows) == 1
        assert rows[0]["avg_bpm"] == 73

    def test_the_date_is_local(self, tmp_db, sync):
        sync(google_sync.sync_ecg, [_ecg(start="2026-03-15T23:30:00Z", offset="3600s")])
        assert db.query_ecg(tmp_db, "2026-03-01", "2026-04-01")[0]["date"] == "2026-03-16"


class TestIrregularRhythm:
    def test_an_alert_is_stored(self, tmp_db, sync):
        points = [
            {
                "name": "users/me/dataTypes/irregular-rhythm-notification/dataPoints/fake",
                "irregularRhythmNotification": {
                    "interval": {
                        "startTime": "2026-03-15T02:00:00Z",
                        "endTime": "2026-03-15T03:00:00Z",
                        "startUtcOffset": "3600s",
                    },
                    "alertWindows": [{"startTime": "2026-03-15T02:10:00Z"}],
                },
            }
        ]
        sync(google_sync.sync_irn, points)
        row = db.query_irn(tmp_db, "2026-03-01", "2026-04-01")[0]
        assert row["date"] == "2026-03-15"
        assert row["end_time"] == "2026-03-15T03:00:00Z"
        assert row["alert_windows"] == [{"startTime": "2026-03-15T02:10:00Z"}]

    def test_an_alert_with_no_identifier_is_skipped(self, tmp_db, sync):
        assert (
            sync(
                google_sync.sync_irn,
                [
                    {
                        "irregularRhythmNotification": {
                            "interval": {"startTime": "2026-03-15T02:00:00Z"}
                        }
                    }
                ],
            )
            == 0
        )
