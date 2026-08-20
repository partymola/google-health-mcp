"""The sync loop: the window it asks for, and what it does when a handler fails.

Every test here drives `run_sync` through a stub handler rather than through a
provider's parser. The loop's job is to choose a window, dispatch, and record
what happened - none of which is about where the data comes from, and a test
that mocks a transport can only reach it through whichever provider is current.
"""

import sqlite3
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools.sync_tools import (
    INCREMENTAL_OVERLAP_DAYS,
    auto_sync_if_stale,
    run_sync,
)


def recording_handler(records: int = 0):
    """A handler that stores the window it was given and reports a count."""
    windows = []

    def handler(conn, start_date, end_date):
        windows.append((start_date, end_date))
        return records

    handler.windows = windows
    return handler


def failing_handler(exception):
    def handler(conn, start_date, end_date):
        raise exception

    return handler


class TestRunSyncSince:
    def test_invalid_since_returns_error(self):
        # Bad date is rejected before any DB/API work.
        result = run_sync(["sleep"], since="garbage")
        assert result["sleep"]["status"] == "error"
        assert "YYYY-MM-DD" in result["sleep"]["message"]

    def test_since_overrides_cursor(self, tmp_db, monkeypatch):
        # run_sync opens its own connection; point it at the tmp db.
        monkeypatch.setattr(db, "get_db", lambda *a, **k: tmp_db)
        # Seed a recent cursor: without --since this would resume near today.
        db.log_sync(tmp_db, "core_temperature", "ok", 0, last_date_attempted="2026-06-28")
        handler = recording_handler()

        result = run_sync(
            ["core_temperature"], since="2020-01-01", handlers={"core_temperature": handler}
        )

        assert result["core_temperature"]["status"] == "ok"
        # The window starts at the --since date, not at the cached cursor.
        assert handler.windows[0][0] == date(2020, 1, 1)
        assert result["core_temperature"]["range"].startswith("2020-01-01 to ")


class TestRunSyncUntil:
    def test_invalid_until_returns_error(self):
        result = run_sync(["sleep"], since="2026-03-01", until="garbage")
        assert result["sleep"]["status"] == "error"
        assert "YYYY-MM-DD" in result["sleep"]["message"]

    def test_until_without_since_is_rejected(self):
        result = run_sync(["sleep"], until="2026-03-10")
        assert result["sleep"]["status"] == "error"
        assert "since" in result["sleep"]["message"]

    def test_until_before_since_is_rejected(self):
        result = run_sync(["sleep"], since="2026-03-10", until="2026-03-01")
        assert result["sleep"]["status"] == "error"
        assert "before" in result["sleep"]["message"]

    def test_since_until_fetches_exact_window(self, tmp_db, monkeypatch):
        """A mid-cache hole is re-fetched as [since, until], not [since, today]."""
        monkeypatch.setattr(db, "get_db", lambda *a, **k: tmp_db)
        handler = recording_handler()

        result = run_sync(
            ["core_temperature"],
            since="2026-03-05",
            until="2026-03-09",
            handlers={"core_temperature": handler},
        )

        assert result["core_temperature"]["status"] == "ok"
        assert result["core_temperature"]["range"] == "2026-03-05 to 2026-03-09"
        assert handler.windows == [(date(2026, 3, 5), date(2026, 3, 9))]

    def test_backfill_does_not_regress_cursor(self, tmp_path, monkeypatch):
        """Repairing an old window must not pull the incremental cursor backwards."""
        # run_sync closes its connection, so give it fresh ones to a shared file
        # and verify through another (the tmp_db fixture's conn would be closed).
        db_path = tmp_path / "test.db"
        real_get_db = db.get_db
        seed = real_get_db(db_path)
        db.log_sync(seed, "core_temperature", "ok", 0, last_date_attempted="2026-06-28")
        seed.commit()
        seed.close()
        monkeypatch.setattr(db, "get_db", lambda *a, **k: real_get_db(db_path))

        run_sync(
            ["core_temperature"],
            since="2026-03-05",
            until="2026-03-09",
            handlers={"core_temperature": recording_handler()},
        )

        verify = real_get_db(db_path)
        assert db.get_last_attempted_date(verify, "core_temperature") == "2026-06-28"
        verify.close()

    def test_future_until_capped_at_today(self, tmp_path, monkeypatch):
        """A future end date is capped so the cursor never advances past today."""
        db_path = tmp_path / "test.db"
        real_get_db = db.get_db
        real_get_db(db_path).close()  # create schema
        monkeypatch.setattr(db, "get_db", lambda *a, **k: real_get_db(db_path))
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        future = (date.today() + timedelta(days=30)).isoformat()

        result = run_sync(
            ["core_temperature"],
            since=yesterday,
            until=future,
            handlers={"core_temperature": recording_handler()},
        )

        assert result["core_temperature"]["status"] == "ok"
        assert result["core_temperature"]["range"].endswith(f"to {date.today().isoformat()}")
        verify = real_get_db(db_path)
        assert db.get_last_attempted_date(verify, "core_temperature") == date.today().isoformat()
        verify.close()


class TestRunSync:
    """The orchestrator: dispatch, status, and what each failure is recorded as."""

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    def test_successful_sync(self, mock_get_db, tmp_db):
        """run_sync calls the handler, logs the result, and returns ok status."""
        mock_get_db.return_value = tmp_db

        results = run_sync(["heart_rate"], days=7, handlers={"heart_rate": recording_handler(1)})
        assert results["heart_rate"]["status"] == "ok"
        assert results["heart_rate"]["records"] == 1
        assert "range" in results["heart_rate"]

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    def test_unknown_type(self, mock_get_db, tmp_db):
        mock_get_db.return_value = tmp_db
        results = run_sync(["invalid_type"], days=7)
        assert results["invalid_type"]["status"] == "error"

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    def test_a_caller_that_names_no_handlers_gets_the_provider_map(
        self, mock_get_db, tmp_db, monkeypatch
    ):
        """auto_sync_if_stale calls run_sync with no handlers, and swallows what
        comes back - so a default of nothing would leave every hourly sync
        reporting "Unknown type" to no one while every table quietly ages.

        The entry is replaced inside the real map rather than passed in, so a
        default that stopped being that map reports an unknown type instead.
        """
        from google_health_mcp.tools import sync_tools

        mock_get_db.return_value = tmp_db
        handler = recording_handler(3)
        monkeypatch.setitem(sync_tools.GOOGLE_SYNC_HANDLERS, "hrv", handler)

        results = run_sync(["hrv"], days=7)

        assert results["hrv"]["status"] == "ok"
        assert results["hrv"]["records"] == 3
        assert len(handler.windows) == 1

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    def test_auth_error_handled(self, mock_get_db, tmp_db):
        """auth_error is told apart from error: doctor grades the two differently,
        and a dead token is the one that will not clear itself."""
        from google_health_mcp.api import HealthAuthError

        mock_get_db.return_value = tmp_db
        results = run_sync(
            ["heart_rate"],
            days=7,
            handlers={"heart_rate": failing_handler(HealthAuthError("expired"))},
        )
        assert results["heart_rate"]["status"] == "auth_error"

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    def test_rate_limit_handled(self, mock_get_db, tmp_db):
        from google_health_mcp.api import HealthRateLimitError

        mock_get_db.return_value = tmp_db
        results = run_sync(
            ["heart_rate"],
            days=7,
            handlers={"heart_rate": failing_handler(HealthRateLimitError(300))},
        )
        assert results["heart_rate"]["status"] == "rate_limited"

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    def test_offline_error_propagates_and_closes(self, mock_get_db, tmp_db):
        """HealthOfflineError must escape run_sync (so callers surface one clean
        offline message instead of per-type error rows), and the DB connection
        must still be closed on the way out."""
        from google_health_mcp import api

        mock_get_db.return_value = tmp_db

        with pytest.raises(api.HealthOfflineError):
            run_sync(
                ["heart_rate"],
                days=7,
                handlers={"heart_rate": failing_handler(api.HealthOfflineError("offline"))},
            )

        # connection was closed despite the exception escaping
        with pytest.raises(sqlite3.ProgrammingError):
            tmp_db.execute("SELECT 1")

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    def test_records_last_date_attempted(self, mock_get_db, tmp_path):
        """Successful sync stores its end-date in sync_log.last_date_attempted."""
        from google_health_mcp import db as db_mod
        from google_health_mcp.db import SCHEMA, _migrate

        db_path = tmp_path / "test.db"
        # db.get_db is patched, so seed a real database directly and hand that
        # connection to run_sync; reopen afterwards to query.
        seed = sqlite3.connect(str(db_path))
        seed.row_factory = sqlite3.Row
        seed.executescript(SCHEMA)
        _migrate(seed)
        mock_get_db.return_value = seed

        run_sync(["heart_rate"], days=7, handlers={"heart_rate": recording_handler()})

        verify = sqlite3.connect(str(db_path))
        verify.row_factory = sqlite3.Row
        last = db_mod.get_last_attempted_date(verify, "heart_rate")
        verify.close()
        assert last == date.today().isoformat()

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    def test_uses_attempted_date_to_skip_empty_days(self, mock_get_db, tmp_db):
        """For sparse types, sync starts from last attempted date, not last data row."""
        from google_health_mcp import db as db_mod

        mock_get_db.return_value = tmp_db

        # Simulate yesterday's run: data table has one old row, sync_log
        # records that we attempted up to yesterday and found no new logs.
        db_mod.save_food_log(tmp_db, {"date": "2026-01-01", "calories_in": 1800, "water_ml": 1000})
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        db_mod.log_sync(tmp_db, "food_log", "ok", 0, last_date_attempted=yesterday)
        tmp_db.commit()

        handler = recording_handler()
        run_sync(["food_log"], handlers={"food_log": handler})

        start = handler.windows[0][0]
        assert start > date(2026, 1, 1), "resumed from the stale data row, not from the cursor"
        assert start == date.fromisoformat(yesterday) - timedelta(days=INCREMENTAL_OVERLAP_DAYS)


class TestWhatAllMeans:
    """ "all" is what the hourly sync asks for, and it names no type itself.

    Resolved to nothing it syncs nothing, reports an empty result, and leaves
    every table quietly ageing - so the expansion is asserted against the
    handler map rather than against a list written out a second time.
    """

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_all_expands_to_every_type_the_provider_can_fetch(
        self, mock_tokens_path, mock_config_path, monkeypatch
    ):
        from google_health_mcp.tools import sync_tools
        from google_health_mcp.tools.google_sync import GOOGLE_SYNC_HANDLERS

        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
        asked = []
        monkeypatch.setattr(
            sync_tools, "run_sync", lambda types, *a, **k: asked.extend(types) or {}
        )

        await sync_tools.health_sync(data_types="all")

        assert set(asked) == set(GOOGLE_SYNC_HANDLERS)

    @patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH")
    @patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH")
    async def test_a_named_type_is_passed_through_untouched(
        self, mock_tokens_path, mock_config_path, monkeypatch
    ):
        from google_health_mcp.tools import sync_tools

        mock_config_path.exists.return_value = True
        mock_tokens_path.exists.return_value = True
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
        asked = []
        monkeypatch.setattr(
            sync_tools, "run_sync", lambda types, *a, **k: asked.extend(types) or {}
        )

        await sync_tools.health_sync(data_types="sleep, hrv")

        assert asked == ["sleep", "hrv"]


class TestAutoSyncOffline:
    """auto_sync_if_stale respects offline / cache-only mode."""

    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    @patch("google_health_mcp.tools.sync_tools.run_sync")
    def test_noop_when_offline(self, mock_run_sync, mock_get_db, monkeypatch):
        # Offline mode: no sync attempt, and not even a DB touch for staleness.
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)
        auto_sync_if_stale("heart_rate")
        mock_run_sync.assert_not_called()
        mock_get_db.assert_not_called()

    @patch("google_health_mcp.tools.sync_tools.db.get_last_sync_time")
    @patch("google_health_mcp.tools.sync_tools.db.get_db")
    @patch("google_health_mcp.tools.sync_tools.run_sync")
    def test_runs_when_not_offline(self, mock_run_sync, mock_get_db, mock_last_sync, monkeypatch):
        # Regression guard: the offline early-return must be gated on the flag,
        # not always on. A never-synced (stale) type still triggers a sync.
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
        mock_last_sync.return_value = None
        auto_sync_if_stale("heart_rate")
        mock_run_sync.assert_called_once()


def test_an_unnamed_failure_still_leaves_a_sync_log_row(tmp_path, monkeypatch):
    """Pins the catch-all: sync_log is the only record a run leaves."""
    from google_health_mcp import db as db_module
    from google_health_mcp.tools import sync_tools

    db_path = tmp_path / "google_health.db"
    conn = db_module.get_db(db_path)
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
    monkeypatch.setattr(sync_tools.db, "get_db", lambda *a, **k: conn)

    results = sync_tools.run_sync(
        ["sleep"],
        days=1,
        handlers={"sleep": failing_handler(AttributeError("'list' object has no get"))},
    )

    assert results["sleep"]["status"] == "error"
    reopened = sqlite3.connect(db_path)
    rows = reopened.execute("SELECT status, notes FROM sync_log").fetchall()
    reopened.close()
    assert rows and rows[0][0] == "error"
    assert rows[0][1] == "unexpected AttributeError"


def test_the_unnamed_failure_message_carries_no_response_content(tmp_path, monkeypatch):
    """sync_log stores this, and these paths carry API responses."""
    from google_health_mcp import db as db_module
    from google_health_mcp.tools import sync_tools

    db_path = tmp_path / "google_health.db"
    conn = db_module.get_db(db_path)
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
    monkeypatch.setattr(sync_tools.db, "get_db", lambda *a, **k: conn)

    results = sync_tools.run_sync(
        ["sleep"], days=1, handlers={"sleep": failing_handler(KeyError("/etc/secret/path"))}
    )
    assert "/etc/secret/path" not in results["sleep"]["message"]
    reopened = sqlite3.connect(db_path)
    notes = reopened.execute("SELECT notes FROM sync_log").fetchone()[0]
    reopened.close()
    assert notes == "unexpected KeyError"


def test_a_database_that_cannot_record_the_failure_does_not_escape(tmp_path, monkeypatch):
    """The row is best-effort; losing it must not also lose the connection."""
    from google_health_mcp import db as db_module
    from google_health_mcp.tools import sync_tools

    real = db_module.get_db(tmp_path / "google_health.db")
    closed = []

    class _WatchedConn:
        def __getattr__(self, name):
            return getattr(real, name)

        def close(self):
            closed.append(True)
            real.close()

    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
    monkeypatch.setattr(sync_tools.db, "get_db", lambda *a, **k: _WatchedConn())
    monkeypatch.setattr(
        sync_tools.db,
        "log_sync",
        MagicMock(side_effect=sqlite3.OperationalError("attempt to write a readonly database")),
    )

    results = sync_tools.run_sync(
        ["sleep"], days=1, handlers={"sleep": failing_handler(AttributeError("boom"))}
    )

    assert results["sleep"]["status"] == "error"
    assert closed == [True]


def test_auto_sync_logs_a_type_rather_than_a_traceback(tmp_path, monkeypatch, caplog):
    """A traceback carries the database path, and this line is on by request.

    The Data Safety Rules have no debug carve-out, so exc_info is not an
    option here.
    """
    import logging

    from google_health_mcp import db as db_module
    from google_health_mcp.tools import sync_tools

    conn = db_module.get_db(tmp_path / "google_health.db")
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
    monkeypatch.setattr(sync_tools.db, "get_db", lambda *a, **k: conn)
    monkeypatch.setattr(
        sync_tools.db,
        "get_last_sync_time",
        MagicMock(side_effect=FileNotFoundError(2, "No such file", "/home/private/cache.db")),
    )

    with caplog.at_level(logging.DEBUG, logger="google_health_mcp.tools.sync_tools"):
        sync_tools.auto_sync_if_stale("sleep")

    assert caplog.records
    for record in caplog.records:
        assert record.exc_info is None
        assert "/home/private" not in record.getMessage()
    assert any("FileNotFoundError" in r.getMessage() for r in caplog.records)


def test_auto_sync_closes_its_connection_even_when_the_lookup_fails(tmp_path, monkeypatch):
    """Its own read is outside run_sync, so nothing else closes for it."""
    from google_health_mcp import db as db_module
    from google_health_mcp.tools import sync_tools

    real = db_module.get_db(tmp_path / "google_health.db")
    closed = []

    class _WatchedConn:
        def __getattr__(self, name):
            return getattr(real, name)

        def close(self):
            closed.append(True)
            real.close()

    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
    monkeypatch.setattr(sync_tools.db, "get_db", lambda *a, **k: _WatchedConn())
    monkeypatch.setattr(
        sync_tools.db,
        "get_last_sync_time",
        MagicMock(side_effect=sqlite3.DatabaseError("malformed database image")),
    )
    monkeypatch.setattr(sync_tools, "run_sync", MagicMock())

    sync_tools.auto_sync_if_stale("sleep")

    assert closed == [True]


class TestTheIncrementalWindowLooksBack:
    """Google publishes a day's summary after the day has ended.

    A sync running late in the local day therefore sees nothing for
    yesterday, records that it attempted through today, and - resuming from
    that cursor - never asks for yesterday again. Measured on a real cache:
    resting heart rate, breathing rate, skin temperature and SpO2 all had no
    19 Aug 2026 row while Google held one for each, and nothing anywhere
    said so. `sync_log` was full of `ok`, so `doctor` reported a clean log.
    """

    def test_a_day_the_cursor_has_passed_is_asked_for_again(self, tmp_db, monkeypatch):
        monkeypatch.setattr(db, "get_db", lambda *a, **k: tmp_db)
        today = date.today()
        # The shape the defect leaves behind: attempted through today, and no
        # row for yesterday because Google had not published it yet.
        db.log_sync(tmp_db, "core_temperature", "ok", 0, last_date_attempted=today.isoformat())
        handler = recording_handler()

        run_sync(["core_temperature"], handlers={"core_temperature": handler})

        start = handler.windows[0][0]
        assert start < today, "the window starts at the cursor, so yesterday is never re-asked"
        assert start <= today - timedelta(days=2), (
            "a day published late needs more than one day of overlap to be caught"
        )

    def test_the_look_back_is_bounded(self, tmp_db, monkeypatch):
        """The cursor exists so a dry type is not re-queried from its last
        reading forever. An unbounded look-back would give that back."""
        monkeypatch.setattr(db, "get_db", lambda *a, **k: tmp_db)
        today = date.today()
        db.log_sync(tmp_db, "core_temperature", "ok", 0, last_date_attempted=today.isoformat())
        handler = recording_handler()

        run_sync(["core_temperature"], handlers={"core_temperature": handler})

        assert handler.windows[0][0] >= today - timedelta(days=14)

    def test_a_cursor_at_the_start_of_time_does_not_overflow(self, tmp_db, monkeypatch):
        """Only a corrupt sync_log produces one, and a diagnostic value must
        not turn into an error the caller reads as a failed sync: subtracting
        the overlap from a date within it of `date.min` raises OverflowError,
        which `run_sync`'s catch-all reports as `Unexpected error during sync`."""
        monkeypatch.setattr(db, "get_db", lambda *a, **k: tmp_db)
        db.log_sync(tmp_db, "hrv", "ok", 0, last_date_attempted="0001-01-02")
        handler = recording_handler()

        results = run_sync(["hrv"], handlers={"hrv": handler})

        assert results["hrv"]["status"] == "ok", results["hrv"]
        assert handler.windows[0][0] == date.min
