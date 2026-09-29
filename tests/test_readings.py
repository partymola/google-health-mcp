"""Every weigh-in and body-fat reading, stored whole as Google sent it.

The `weight` table keeps one row a day; a scale records every step onto it,
and each is its own point. Nothing here is converted: the record is the point
as it came, grams and all.
"""

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync

# Fictional throughout.
_CIVIL = {"date": {"year": 2026, "month": 3, "day": 15}, "time": {"hours": 7, "minutes": 5}}


def _weigh_in(name="users/me/dataTypes/weight/dataPoints/1", grams=12345.0, civil=_CIVIL):
    return {
        "name": name,
        "dataSource": {"platform": "HEALTH_CONNECT", "recordingMethod": "ACTIVELY_MEASURED"},
        "weight": {
            "weightGrams": grams,
            "sampleTime": {
                "physicalTime": "2026-03-15T07:05:00Z",
                "utcOffset": "0s",
                "civilTime": civil,
            },
        },
    }


def _body_fat(name="users/me/dataTypes/body-fat/dataPoints/1", percentage=12.5):
    return {
        "name": name,
        "bodyFat": {
            "percentage": percentage,
            "sampleTime": {
                "physicalTime": "2026-03-15T07:05:00Z",
                "utcOffset": "0s",
                "civilTime": _CIVIL,
            },
        },
    }


def _sync(tmp_db, handler, points):
    """Answer only the data type the points belong to, so a handler fetching
    the other one finds nothing."""
    data_type = "body-fat" if any("bodyFat" in p for p in points) else "weight"
    with patch.object(
        google_sync.api,
        "list_google_data_points",
        side_effect=lambda t, *_: points if t == data_type else [],
    ):
        count = handler(tmp_db, date(2026, 3, 1), date(2026, 4, 1))
    tmp_db.commit()
    return count


class TestEveryReadingIsStoredWhole:
    @pytest.mark.parametrize(
        "handler,point,query",
        [
            (google_sync.sync_weight_readings, _weigh_in(), db.query_weight_readings),
            (google_sync.sync_body_fat_readings, _body_fat(), db.query_body_fat_readings),
        ],
        ids=["weight", "body-fat"],
    )
    def test_the_record_is_the_point_as_google_sent_it(self, tmp_db, handler, point, query):
        assert _sync(tmp_db, handler, [point]) == 1
        (row,) = query(tmp_db, "2026-03-15", "2026-03-15")
        assert row["reading_id"] == point["name"]
        assert row["datetime"] == "2026-03-15T07:05:00"
        assert row["date"] == "2026-03-15"
        assert row["record"] == point
        assert row["provider"] == "google"

    def test_two_weigh_ins_on_one_day_are_two_rows(self, tmp_db):
        """What the daily row cannot hold, and the reason this table exists."""
        _sync(
            tmp_db,
            google_sync.sync_weight_readings,
            [
                # Named to sort before the morning reading, so an order by id
                # and an order by time disagree.
                _weigh_in(
                    name="users/me/dataTypes/weight/dataPoints/0",
                    grams=12000.0,
                    civil={**_CIVIL, "time": {"hours": 19, "minutes": 5}},
                ),
                _weigh_in(),
            ],
        )
        rows = db.query_weight_readings(tmp_db, "2026-03-15", "2026-03-15")
        assert [r["record"]["weight"]["weightGrams"] for r in rows] == [12345.0, 12000.0]

    def test_the_daily_row_is_left_alone(self, tmp_db):
        """The two tables answer different questions and neither writes the other."""
        _sync(tmp_db, google_sync.sync_weight_readings, [_weigh_in()])
        assert db.query_weight(tmp_db, "2026-03-15", "2026-03-15") == []

    def test_the_daily_sync_leaves_the_readings_alone(self, tmp_db):
        _sync(tmp_db, google_sync.sync_weight, [_weigh_in()])
        assert db.query_weight(tmp_db, "2026-03-15", "2026-03-15") != []
        assert db.query_weight_readings(tmp_db, "2026-03-15", "2026-03-15") == []

    @pytest.mark.parametrize(
        "point",
        [
            {**_weigh_in(), "name": None},
            {**_weigh_in(), "name": ""},
            {**_weigh_in(), "name": 3},
            {"name": "users/me/dataTypes/weight/dataPoints/1", "weight": "not an object"},
            _weigh_in(civil={}),
        ],
        ids=["no-name", "empty-name", "numeric-name", "no-reading", "no-time"],
    )
    def test_a_reading_that_cannot_be_keyed_or_dated_is_skipped(self, tmp_db, point):
        assert _sync(tmp_db, google_sync.sync_weight_readings, [point]) == 0
        assert db.query_weight_readings(tmp_db, "2026-03-01", "2026-04-01") == []


@pytest.mark.parametrize(
    "save,prefix",
    [(db.save_weight_reading, "weight"), (db.save_body_fat_reading, "body-fat")],
    ids=["weight", "body-fat"],
)
def test_a_weigher_who_stops_is_not_a_stopped_series(tmp_db, save, prefix):
    """The same weigh-ins as `weight`, which doctor already leaves to the person."""
    from datetime import timedelta

    from google_health_mcp import doctor

    start = date(2026, 3, 1)
    for i in range(30):
        day = (start + timedelta(days=i)).isoformat()
        save(tmp_db, {"reading_id": f"{prefix}/{i}", "date": day, "record": "{}"})
    for i in range(50):
        day = (start + timedelta(days=i)).isoformat()
        db.save_sleep(tmp_db, {"date": day, "total_minutes": 400})
    tmp_db.commit()
    findings = doctor._check_stopped_series(tmp_db, doctor._newest_per_type(tmp_db))
    assert not [f for f in findings if f.check == doctor.STOPPED_SERIES]


async def _call(db_path):
    from google_health_mcp.tools.weight_tools import health_get_weight_readings

    with (
        patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
        patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        patch("google_health_mcp.tools.weight_tools.refresh_before_query") as refresh,
        patch.object(db, "DB_PATH", db_path),
    ):
        client.exists.return_value = True
        tokens.exists.return_value = True
        body = json.loads(await health_get_weight_readings(start_date="2026-03-15"))
    return body, {c.args[0] for c in refresh.call_args_list}


class TestTheTool:
    async def test_both_kinds_of_reading_come_back(self, tmp_db):
        _sync(tmp_db, google_sync.sync_weight_readings, [_weigh_in()])
        _sync(tmp_db, google_sync.sync_body_fat_readings, [_body_fat()])
        body, refreshed = await _call(Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]))
        assert body["weight_readings"][0]["record"]["weight"]["weightGrams"] == 12345.0
        assert body["body_fat_readings"][0]["record"]["bodyFat"]["percentage"] == 12.5
        assert body["count"] == 2
        assert refreshed == {"weight_readings", "body_fat_readings"}

    async def test_body_fat_alone_is_still_an_answer(self, tmp_db):
        _sync(tmp_db, google_sync.sync_body_fat_readings, [_body_fat()])
        body, _ = await _call(Path(tmp_db.execute("PRAGMA database_list").fetchone()[2]))
        assert "message" not in body
        assert body["weight_readings"] == []
        assert len(body["body_fat_readings"]) == 1
        assert body["count"] == 1

    async def test_an_empty_window_says_so(self, tmp_path):
        body, _ = await _call(tmp_path / "empty.db")
        assert body["message"] == "No weight or body-fat readings found for this period."
