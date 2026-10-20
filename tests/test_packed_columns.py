"""The two columns stored compressed: a route's TCX and a sleep session's record.

Each runs to kilobytes of text a row, and a route to hundreds of them, so together
they were most of a cache's size. They are stored zlib-compressed, as BLOBs in
columns declared TEXT, and every reader returns exactly the text it was given.
A row an earlier release stored as text reads the same, and the syncing host
packs such rows and gives the space back.
"""

import sqlite3
import zlib
from pathlib import Path

import pytest

from google_health_mcp import db
from google_health_mcp.tools import sync_tools

# Fictional throughout. Indented and non-ASCII, so a round trip that strips
# whitespace or mangles an encoding cannot pass.
_TCX = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<TrainingCenterDatabase>\n"
    "  <Activities>\n"
    '    <Activity Sport="Biking"><Notes>Café loop</Notes></Activity>\n'
    "  </Activities>\n"
    "</TrainingCenterDatabase>\n"
)
_RECORD = '{"name": "users/me/dataTypes/sleep/dataPoints/1", "sleep": {"type": "STAGES"}}'
_ROUTE_ID = "users/me/dataTypes/exercise/dataPoints/1"
_SESSION_ID = "users/me/dataTypes/sleep/dataPoints/1"


def _stored_type(conn, table, column):
    return conn.execute(f"SELECT typeof({column}) FROM {table}").fetchone()[0]


def _save_both(conn, tcx=_TCX, record=_RECORD):
    db.save_exercise_route(
        conn, {"log_id": _ROUTE_ID, "date": "2026-03-10", "tcx": tcx, "provider": "google"}
    )
    db.save_sleep_session(
        conn,
        {
            "session_id": _SESSION_ID,
            "date": "2026-03-15",
            "start_time": "2026-03-14T23:00:00Z",
            "end_time": "2026-03-15T07:00:00Z",
            "record": record,
            "provider": "google",
        },
    )
    conn.commit()


def _insert_as_text(conn, tcx=_TCX, record=_RECORD):
    """Rows as a release before packing wrote them."""
    conn.execute(
        "INSERT INTO exercise_routes (log_id, date, tcx, provider) VALUES (?, ?, ?, 'google')",
        (_ROUTE_ID, "2026-03-10", tcx),
    )
    conn.execute(
        "INSERT INTO sleep_sessions (session_id, date, start_time, end_time, record, provider) "
        "VALUES (?, ?, ?, ?, ?, 'google')",
        (_SESSION_ID, "2026-03-15", "2026-03-14T23:00:00Z", "2026-03-15T07:00:00Z", record),
    )
    conn.commit()


def _read_both(conn):
    route = db.query_exercise_route(conn, _ROUTE_ID)
    (session,) = db.query_sleep_sessions(conn, "2026-03-15", "2026-03-15")
    return route, session


class TestWhatIsStored:
    def test_both_columns_are_stored_compressed(self, tmp_db):
        _save_both(tmp_db)
        assert _stored_type(tmp_db, "exercise_routes", "tcx") == "blob"
        assert _stored_type(tmp_db, "sleep_sessions", "record") == "blob"

    def test_a_route_reads_back_exactly_as_it_was_given(self, tmp_db):
        _save_both(tmp_db)
        route, _ = _read_both(tmp_db)
        assert route["tcx"] == _TCX

    def test_a_session_record_reads_back_as_the_point_it_was(self, tmp_db):
        _save_both(tmp_db)
        _, session = _read_both(tmp_db)
        assert session["record"] == {
            "name": _SESSION_ID,
            "sleep": {"type": "STAGES"},
        }

    def test_a_withdrawn_value_is_stored_as_null(self, tmp_db):
        """None still means "withdraw this", not a compressed empty string."""
        _save_both(tmp_db)
        db.save_exercise_route(tmp_db, {"log_id": _ROUTE_ID, "date": "2026-03-10", "tcx": None})
        tmp_db.commit()
        assert _stored_type(tmp_db, "exercise_routes", "tcx") == "null"

    def test_a_row_an_earlier_release_stored_as_text_reads_the_same(self, tmp_db):
        _insert_as_text(tmp_db)
        route, session = _read_both(tmp_db)
        assert route["tcx"] == _TCX
        assert session["record"]["sleep"] == {"type": "STAGES"}

    @pytest.mark.parametrize(
        "stored", [b"not zlib", zlib.compress(b"\xff\xfe")], ids=["not-zlib", "not-utf8"]
    )
    def test_a_packed_value_that_will_not_inflate_reads_as_absent(self, tmp_db, stored):
        """Unknown, not an error: one bad row must not cost the window."""
        _insert_as_text(tmp_db)
        tmp_db.execute("UPDATE exercise_routes SET tcx = ?", (stored,))
        tmp_db.execute("UPDATE sleep_sessions SET record = ?", (stored,))
        tmp_db.commit()
        route, session = _read_both(tmp_db)
        assert route["tcx"] is None
        assert session["record"] is None


def _file_db(tmp_path) -> tuple[sqlite3.Connection, Path]:
    path = tmp_path / "google_health.db"
    return db.get_db(path), path


class TestCompaction:
    def test_rows_stored_as_text_are_packed_and_read_the_same(self, tmp_db):
        _insert_as_text(tmp_db)
        before = _read_both(tmp_db)

        assert db.compact(tmp_db) == 2

        assert _stored_type(tmp_db, "exercise_routes", "tcx") == "blob"
        assert _stored_type(tmp_db, "sleep_sessions", "record") == "blob"
        assert _read_both(tmp_db) == before

    def test_nothing_else_in_the_row_changes(self, tmp_db):
        _insert_as_text(tmp_db)
        columns = "session_id, date, start_time, end_time, provider"
        before = tmp_db.execute(f"SELECT {columns} FROM sleep_sessions").fetchall()
        db.compact(tmp_db)
        assert tmp_db.execute(f"SELECT {columns} FROM sleep_sessions").fetchall() == before

    def test_a_second_run_finds_nothing_to_pack(self, tmp_db):
        _insert_as_text(tmp_db)
        db.compact(tmp_db)
        assert db.compact(tmp_db) == 0

    def test_a_row_already_packed_is_left_alone(self, tmp_db):
        _save_both(tmp_db)
        assert db.compact(tmp_db) == 0

    def test_every_row_keeps_its_own_text_and_date(self, tmp_db):
        """The state an upgrade meets: many rows, some already packed, an
        empty one and a NULL one."""
        routes = {f"ex/{i}": (f"2026-03-1{i}", f"{_TCX}<!-- {i} -->") for i in range(3)}
        for log_id, (day, tcx) in routes.items():
            tmp_db.execute(
                "INSERT INTO exercise_routes (log_id, date, tcx) VALUES (?, ?, ?)",
                (log_id, day, tcx),
            )
        tmp_db.execute(
            "INSERT INTO exercise_routes (log_id, date, tcx) VALUES "
            "('ex/empty', '2026-03-20', ''), ('ex/none', '2026-03-21', NULL)"
        )
        db.save_exercise_route(tmp_db, {"log_id": "ex/packed", "date": "2026-03-22", "tcx": _TCX})
        for night in (15, 16):
            tmp_db.execute(
                "INSERT INTO sleep_sessions (session_id, date, record) VALUES (?, ?, ?)",
                (f"s/{night}", f"2026-03-{night}", f'{{"night": {night}}}'),
            )
        tmp_db.commit()

        assert db.compact(tmp_db) == 6

        for log_id, (day, tcx) in routes.items():
            route = db.query_exercise_route(tmp_db, log_id)
            assert (route["date"], route["tcx"]) == (day, tcx)
        assert db.query_exercise_route(tmp_db, "ex/empty")["tcx"] == ""
        assert db.query_exercise_route(tmp_db, "ex/none")["tcx"] is None
        assert db.query_exercise_route(tmp_db, "ex/packed")["tcx"] == _TCX
        sessions = db.query_sleep_sessions(tmp_db, "2026-03-15", "2026-03-16")
        assert [(s["date"], s["record"]) for s in sessions] == [
            ("2026-03-15", {"night": 15}),
            ("2026-03-16", {"night": 16}),
        ]
        assert db.compact(tmp_db) == 0

    def test_the_space_the_text_took_is_given_back(self, tmp_path):
        conn, path = _file_db(tmp_path)
        # Repetitive like a real export, so it compresses the way one does.
        _insert_as_text(conn, tcx=_TCX * 20_000)
        large = path.stat().st_size

        db.compact(conn)
        conn.close()

        assert path.stat().st_size < large / 4

    def test_free_space_an_earlier_run_could_not_reclaim_is_given_back(self, tmp_path):
        """A VACUUM refused once, because another connection held the file, is
        retried by the next run rather than left behind for good."""
        conn, path = _file_db(tmp_path)
        _insert_as_text(conn, tcx=_TCX * 20_000)
        conn.execute("DELETE FROM exercise_routes")
        conn.execute("DELETE FROM sleep_sessions")
        conn.commit()
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] > 0

        assert db.compact(conn) == 0

        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        conn.close()

    @staticmethod
    def _freed(tmp_path, kept, dropped):
        """A cache with `dropped` of `kept + dropped` samples' worth of free pages."""
        conn, _ = _file_db(tmp_path)
        for reading, samples in (("kept", kept), ("dropped", dropped)):
            db.save_ecg(
                conn, {"reading_id": reading, "date": "2026-03-10", "waveform": "0," * samples}
            )
        conn.commit()
        conn.execute("DELETE FROM ecg WHERE reading_id = 'dropped'")
        conn.commit()
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        assert free > 0
        return conn, free

    def test_a_little_free_space_does_not_rewrite_the_file(self, tmp_path):
        """Rewriting the whole cache on every sync is a full upload to every
        host that shares it, for a few pages."""
        conn, free = self._freed(tmp_path, kept=400_000, dropped=100_000)
        db.compact(conn)
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == free
        conn.close()

    def test_a_third_of_the_file_free_is_given_back(self, tmp_path):
        conn, _ = self._freed(tmp_path, kept=300_000, dropped=150_000)
        db.compact(conn)
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        conn.close()

    def test_packing_anything_gives_the_space_back(self, tmp_path):
        """However small a share of the file the packed rows were."""
        conn, _ = self._freed(tmp_path, kept=400_000, dropped=50_000)
        _insert_as_text(conn)
        assert db.compact(conn) == 2
        assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0
        conn.close()


def _stub_handler(conn, start, end):
    return 0


class TestTheSyncCompacts:
    @pytest.fixture
    def legacy(self, tmp_path, monkeypatch):
        path = tmp_path / "google_health.db"
        seed = db.get_db(path)
        _insert_as_text(seed)
        seed.close()
        real_get_db = db.get_db
        monkeypatch.setattr(sync_tools.db, "get_db", lambda *a, **k: real_get_db(path))
        return path

    def _types(self, path):
        conn = sqlite3.connect(path)
        try:
            return (
                _stored_type(conn, "exercise_routes", "tcx"),
                _stored_type(conn, "sleep_sessions", "record"),
            )
        finally:
            conn.close()

    def test_a_sync_packs_what_an_earlier_release_left(self, legacy, monkeypatch):
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
        results = sync_tools.run_sync(["hrv"], days=1, handlers={"hrv": _stub_handler})
        assert results["hrv"]["status"] == "ok"
        assert self._types(legacy) == ("blob", "blob")

    def test_a_cache_only_host_never_rewrites_the_cache(self, legacy, monkeypatch):
        """Only the syncing host writes the shared file. The cache is padded
        with free pages so a VACUUM would rewrite it too."""
        conn = sqlite3.connect(legacy)
        conn.execute(
            "INSERT INTO ecg (reading_id, date, waveform) VALUES ('x', '2026-03-10', ?)",
            ("0," * 500_000,),
        )
        conn.commit()
        conn.execute("DELETE FROM ecg")
        conn.commit()
        conn.close()
        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

        sync_tools.run_sync(["hrv"], days=1, handlers={"hrv": _stub_handler})

        assert self._types(legacy) == ("text", "text")
        conn = sqlite3.connect(legacy)
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        conn.close()
        assert free > 0, "the cache was vacuumed"

    def test_a_compaction_that_fails_does_not_fail_the_sync(self, legacy, monkeypatch, caplog):
        import logging

        monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)

        def locked(conn):
            raise sqlite3.OperationalError("database is locked: /home/private/cache.db")

        monkeypatch.setattr(sync_tools.db, "compact", locked)
        with caplog.at_level(logging.INFO, logger="google_health_mcp.tools.sync_tools"):
            results = sync_tools.run_sync(["hrv"], days=1, handlers={"hrv": _stub_handler})

        assert results["hrv"]["status"] == "ok"
        assert any("OperationalError" in r.getMessage() for r in caplog.records)
        assert all("/home/private" not in r.getMessage() for r in caplog.records)
