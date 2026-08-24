"""Shared fixtures for the google-health-mcp test suite."""

import urllib.request

import pytest

from google_health_mcp import config, db, helpers


@pytest.fixture(autouse=True)
def _no_real_credentials(tmp_path_factory, monkeypatch):
    """No test sees the developer's own credentials.

    Every query tool opens with `refresh_before_query`, which auto-syncs when
    the cache holds no `sync_log` row for the type - and a temp database never
    has one. Given a usable token that sync succeeded, writing the real
    account into the test's database and into the windows it asserts over,
    while on a machine with no credentials it failed and `auto_sync_if_stale`
    swallowed it. The same suite meant two different things.

    Pointing both credential files at an empty directory is what settles it:
    the refresh is refused before a request is built, so every host takes the
    branch CI takes. A test wanting credentials writes them here.

    `helpers` is patched as well as `config`, and it is the binding that
    matters most: `helpers` imports the two paths by value, so it keeps its
    own copies and `require_auth` - worn by every tool - would go on reading
    the real files. `auth` and `doctor` reach them through `config` at call
    time and need nothing here.
    """
    empty = tmp_path_factory.mktemp("credentials")
    for module in (config, helpers):
        monkeypatch.setattr(module, "GOOGLE_TOKENS_PATH", empty / "google_tokens.json")
        monkeypatch.setattr(module, "GOOGLE_CLIENT_PATH", empty / "google_client.json")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Refuse a real request, as a backstop to the fixture above.

    `pytest.fail` rather than `assert`, and the difference is load-bearing:
    `refresh_google_token` and `auto_sync_if_stale` both end in `except
    Exception`, so an `AssertionError` raised here is caught by the code under
    test and reported as an ordinary network failure - which is precisely the
    silence this fixture exists to break. `Failed` derives from
    `BaseException` and neither catch-all can absorb it.

    A test that means to exercise a request patches `urlopen` itself, which
    replaces this for the duration.
    """

    def refuse(*_args, **_kwargs):
        pytest.fail("this test reached the network; patch urllib.request.urlopen to serve it")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture(autouse=True)
def _isolate_from_ambient_env(monkeypatch):
    """Keep the developer's own environment out of every test.

    A shell that exports these - and one that runs a sync by hand usually
    does - otherwise changes what the suite asserts against.
    """
    for var in (
        "GOOGLE_HEALTH_MCP_OFFLINE",
        "GOOGLE_HEALTH_MCP_CONFIG_DIR",
        "GOOGLE_HEALTH_MCP_DB_PATH",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database carrying this version's schema."""
    db_path = tmp_path / "test_google_health.db"
    conn = db.get_db(db_path)
    yield conn
    conn.close()


@pytest.fixture
def populated_db(tmp_db):
    """Database pre-loaded with sample data across all tables."""
    # Heart rate - 5 days
    for i in range(5):
        db.save_heart_rate(
            tmp_db,
            f"2026-03-{10 + i:02d}",
            60 + i,
            [
                {
                    "name": "Out of Range",
                    "minutes": 1200,
                    "caloriesOut": 1000,
                    "min": 30,
                    "max": 100,
                },
                {"name": "Fat Burn", "minutes": 30, "caloriesOut": 200, "min": 100, "max": 140},
            ],
        )

    # Activity - 5 days
    for i in range(5):
        db.save_activity(
            tmp_db,
            {
                "date": f"2026-03-{10 + i:02d}",
                "steps": 8000 + i * 500,
                "calories_out": 2200 + i * 100,
                "active_minutes": 30 + i * 5,
                "very_active_minutes": 15 + i * 2,
                "fairly_active_minutes": 15 + i * 3,
                "lightly_active_minutes": 180 + i * 10,
                "sedentary_minutes": 600 - i * 20,
                "floors": 5 + i,
                "distance_km": 5.0 + i * 0.5,
            },
        )

    # Exercises - 3 sessions
    db.save_exercise(
        tmp_db,
        "log001",
        {
            "date": "2026-03-10",
            "name": "Walk",
            "duration_min": 45,
            "calories": 200,
            "avg_hr": 105,
            "steps": 5000,
            "distance_km": 3.5,
            "distance_unit": "Kilometer",
            "start_time": "2026-03-10T08:00:00",
            "source": "Tracker",
            "log_type": "auto_detected",
        },
    )
    db.save_exercise(
        tmp_db,
        "log002",
        {
            "date": "2026-03-11",
            "name": "Cycling",
            "duration_min": 30,
            "calories": 300,
            "avg_hr": 130,
            "steps": None,
            "distance_km": 8.0,
            "distance_unit": "Kilometer",
            "start_time": "2026-03-11T07:30:00",
            "source": "Tracker",
            "log_type": "auto_detected",
        },
    )
    db.save_exercise(
        tmp_db,
        "log003",
        {
            "date": "2026-03-12",
            "name": "Walk",
            "duration_min": 60,
            "calories": 250,
            "avg_hr": 110,
            "steps": 7000,
            "distance_km": 4.5,
            "distance_unit": "Kilometer",
            "start_time": "2026-03-12T12:00:00",
            "source": "Tracker",
            "log_type": "auto_detected",
        },
    )

    # Sleep - 5 nights
    for i in range(5):
        db.save_sleep(
            tmp_db,
            {
                "date": f"2026-03-{10 + i:02d}",
                "total_minutes": 420 + i * 10,
                "efficiency": 90 + i,
                "start_time": f"2026-03-{9 + i:02d}T23:00:00",
                "end_time": f"2026-03-{10 + i:02d}T06:00:00",
                "deep_minutes": 60 + i * 5,
                "light_minutes": 200 + i * 3,
                "rem_minutes": 100 + i * 2,
                "wake_minutes": 60 - i * 5,
            },
        )

    # Weight - 3 entries
    for i in range(3):
        db.save_weight(
            tmp_db,
            {
                "date": f"2026-03-{10 + i * 3:02d}",
                "weight_kg": 80.0 - i * 0.5,
                "bmi": 25.0 - i * 0.2,
                "fat_pct": 20.0 - i * 0.5,
            },
        )

    # SpO2 - 5 nights
    for i in range(5):
        db.save_spo2(
            tmp_db,
            {
                "date": f"2026-03-{10 + i:02d}",
                "avg": 96.0 + i * 0.2,
                "min": 93.0 + i * 0.3,
                "max": 99.0,
            },
        )

    # HRV - 5 nights
    for i in range(5):
        db.save_hrv(
            tmp_db,
            {
                "date": f"2026-03-{10 + i:02d}",
                "daily_rmssd": 35.0 + i * 2.0,
                "deep_rmssd": 40.0 + i * 2.5,
            },
        )

    # AZM - 5 days
    for i in range(5):
        db.save_azm(
            tmp_db,
            {
                "date": f"2026-03-{10 + i:02d}",
                "total_minutes": 30 + i * 5,
                "fat_burn_minutes": 20 + i * 2,
                "cardio_minutes": 10 + i,
                "peak_minutes": i,
            },
        )

    # Breathing rate - 5 nights
    for i in range(5):
        db.save_breathing_rate(
            tmp_db,
            {
                "date": f"2026-03-{10 + i:02d}",
                "breaths_per_min": 14.0 + i * 0.2,
            },
        )

    # Skin temperature - 5 nights
    for i in range(5):
        db.save_skin_temperature(
            tmp_db,
            {
                "date": f"2026-03-{10 + i:02d}",
                "nightly_relative": -0.2 + i * 0.1,
                "log_type": "dermal",
            },
        )

    # Core temperature - 4 manual readings across 3 days (two on 2026-03-11)
    for dt, temp in [
        ("2026-03-10T08:00:00", 36.6),
        ("2026-03-11T09:30:00", 37.8),
        ("2026-03-11T18:15:00", 38.4),
        ("2026-03-12T07:45:00", 37.1),
    ]:
        db.save_core_temperature(
            tmp_db,
            {"datetime": dt, "date": dt[:10], "temp_celsius": temp},
        )

    # Cardio fitness - 3 readings
    for i in range(3):
        db.save_cardio_fitness(
            tmp_db,
            {
                "date": f"2026-03-{10 + i * 2:02d}",
                "vo2_max_low": 38.0 + i,
                "vo2_max_high": 42.0 + i,
            },
        )

    # Food log - 4 days
    for i in range(4):
        db.save_food_log(
            tmp_db,
            {
                "date": f"2026-03-{10 + i:02d}",
                "calories_in": 2000 + i * 50,
                "water_ml": 1500.0 + i * 100,
            },
        )

    tmp_db.commit()
    return tmp_db


#: The calls a data type reaches a request through: the two client reads, and
#: the three helpers in `google_sync` that wrap them. Shared by the scope test
#: and the seam test, which read the same calls for different reasons.
#:
#: Named rather than matched on "any call taking a known path", which counted
#: things that fetch nothing - `point.get("electrocardiogram")` reads a
#: response field, and `refresh_before_query("sleep", ...)` names a cached
#: table that happens to spell a Google path the same way. With those
#: admitted, deleting the ECG fetch left the ECG scope still justified.
#:
#: Naming private helpers makes this rename-fragile, which is the trade: a
#: rename fails here with a message in the right file, where the loose version
#: was silent.
FETCHERS = {
    "list_google_data_points",
    "daily_roll_up",
    "_daily_rows",
    "_rollup_values",
    "_samples",
}

_DAY = {"year": 2026, "month": 3, "day": 15}
_CIVIL = {"date": _DAY, "time": {}}


def dated_but_empty(field: str) -> dict:
    """A point of the given type carrying an identity and a date, and no value.

    One point serves every read path and filter family: a rollup reads
    `civilStartTime`, a daily list point its payload's `date`, a sample its
    `sampleTime`, a session its `interval`, and the episode tables their
    point-level `name`. Carrying all of them means each handler reaches its
    write rather than skipping the point for want of an identity, which is
    what makes a negative assertion about what gets written mean anything.

    Unlike Google in one way worth knowing when reading a green result: a real
    rollup of a true-zero type carries an explicit 0, so `sync_activity` fills
    a row here rather than writing nothing.
    """
    return {
        "name": f"point/{field}",
        "civilStartTime": _CIVIL,
        field: {
            "date": _DAY,
            "sampleTime": {"civilTime": _CIVIL},
            "interval": {
                "startTime": "2026-03-14T23:00:00Z",
                "endTime": "2026-03-15T07:00:00Z",
                "civilStartTime": _CIVIL,
                "civilEndTime": _CIVIL,
            },
        },
    }
