"""A permission the grant lacks is named wherever the person will see it.

A grant never gains scopes on refresh, so a release that asks for a new one
leaves every existing install without it until the user re-consents. The
syncing host records which requested scopes the grant lacks; every tool
response, `doctor` and `sync` repeat it, including on offline hosts, which
hold no token and read only the shared database.
"""

import json
import os
import sys
from unittest.mock import patch

import pytest

from google_health_mcp import auth, cli, config, db, doctor
from google_health_mcp.helpers import missing_scopes_note, require_auth
from google_health_mcp.tools import google_sync, sync_tools

_SOME = list(config.GOOGLE_SCOPE_READERS)[:2]


def _granted(*names):
    return " ".join(f"{config.GOOGLE_SCOPE_PREFIX}{n}.readonly" for n in names)


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "cache.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    monkeypatch.setattr(config, "DB_PATH", path)
    return path


def _record(path, missing):
    conn = db.get_db(path)
    try:
        db.record_missing_scopes(conn, missing)
    finally:
        conn.close()


class TestTheRecord:
    def test_what_is_recorded_is_what_is_read_back(self, db_path):
        _record(db_path, _SOME)
        assert db.recorded_missing_scopes() == _SOME

    def test_a_later_record_replaces_the_earlier(self, db_path):
        _record(db_path, _SOME)
        _record(db_path, [])
        assert db.recorded_missing_scopes() == []

    def test_unknown_is_recorded_as_unknown(self, db_path):
        _record(db_path, _SOME)
        _record(db_path, None)
        assert db.recorded_missing_scopes() is None

    def test_reading_never_creates_a_database(self, db_path):
        """Every tool response reads this, including on hosts that must not write."""
        assert db.recorded_missing_scopes() is None
        assert not db_path.exists()

    def test_a_database_from_before_the_table_reads_as_unknown(self, db_path):
        import sqlite3

        sqlite3.connect(db_path).close()
        assert db.recorded_missing_scopes() is None

    def test_a_database_with_the_table_and_no_record_reads_as_unknown(self, db_path):
        """Upgraded and not yet synced: the window this record exists for."""
        db.get_db(db_path).close()
        assert db.recorded_missing_scopes() is None

    def test_a_file_that_is_not_a_database_reads_as_unknown(self, db_path):
        db_path.write_bytes(b"not a database" * 100)
        assert db.recorded_missing_scopes() is None

    @pytest.mark.skipif(
        sys.platform == "win32" or getattr(os, "geteuid", lambda: 1)() == 0,
        reason="directory search permission is POSIX, and root ignores it",
    )
    def test_a_database_that_cannot_be_reached_reads_as_unknown(self, tmp_path):
        """Every tool response asks, so this must never raise."""
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0)
        try:
            assert db.recorded_missing_scopes(locked / "cache.db") is None
        finally:
            locked.chmod(0o700)

    @pytest.mark.parametrize("stored", ["{not json", '{"a": 1}', "[1, 2]"])
    def test_an_unreadable_record_reads_as_unknown(self, db_path, stored):
        conn = db.get_db(db_path)
        conn.execute(
            "INSERT INTO authorisation (id, missing_scopes, checked_at) VALUES (1, ?, 'x')",
            (stored,),
        )
        conn.commit()
        conn.close()
        assert db.recorded_missing_scopes() is None


class TestWhatTheTokenSaysWasGranted:
    def test_the_stored_scope_is_returned(self, tmp_path, monkeypatch):
        path = tmp_path / "google_tokens.json"
        path.write_text(json.dumps({"access_token": "a", "scope": "x y"}))
        monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", path)
        assert auth.granted_scopes() == "x y"

    @pytest.mark.parametrize(
        "contents", [None, "{not json", "[]", '{"access_token": "a"}', '{"scope": 7}']
    )
    def test_anything_else_is_unknown_and_never_raises(self, tmp_path, monkeypatch, contents):
        path = tmp_path / "google_tokens.json"
        if contents is not None:
            path.write_text(contents)
        monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", path)
        assert auth.granted_scopes() is None


class TestTheSyncRecordsIt:
    def _sync(self, granted, handlers=None):
        with patch.object(sync_tools.auth, "granted_scopes", return_value=granted):
            return sync_tools.run_sync(
                ["t"], handlers=handlers or {"t": lambda conn, start, end: 0}
            )

    def test_the_missing_scopes_are_recorded(self, db_path):
        self._sync(_granted(*list(config.GOOGLE_SCOPE_READERS)[2:]))
        assert db.recorded_missing_scopes() == _SOME

    def test_a_full_grant_records_nothing_missing(self, db_path):
        self._sync(_granted(*config.GOOGLE_SCOPE_READERS))
        assert db.recorded_missing_scopes() == []

    def test_a_token_that_records_nothing_leaves_what_was_known(self, db_path):
        """A token file caught mid-rewrite reads as unknown, and that must not
        erase a shortfall every host is reporting."""
        _record(db_path, _SOME)
        self._sync(None)
        assert db.recorded_missing_scopes() == _SOME

    def test_it_is_recorded_even_when_a_type_fails(self, db_path):
        """A missing scope is exactly what makes types fail."""

        def refused(conn, start, end):
            raise sync_tools.api.HealthAuthError("refused")

        self._sync(_granted(), handlers={"t": refused})
        assert db.recorded_missing_scopes() == list(config.GOOGLE_SCOPE_READERS)

    def test_failing_to_record_it_does_not_fail_the_sync(self, db_path, monkeypatch):
        def broken(*a, **k):
            raise RuntimeError("disk")

        monkeypatch.setattr(db, "record_missing_scopes", broken)
        assert self._sync(_granted())["t"]["status"] == "ok"


class TestAMissingScopeSkipsRatherThanFails:
    """A type read under a scope the grant lacks is skipped, not failed.

    Every install upgrading into a release that adds a scope lacks it until
    the person consents again. Failing those types would exit `sync` 1 and
    grade doctor's sync log a failure every run, for a shortfall the note
    already reports as a warning.
    """

    def _sync(self, granted, types, handlers):
        with patch.object(sync_tools.auth, "granted_scopes", return_value=granted):
            return sync_tools.run_sync(types, handlers=handlers)

    def test_the_type_is_skipped_and_its_handler_never_runs(self, db_path):
        ran = []
        held = [s for s in config.GOOGLE_SCOPE_READERS if s != "location"]
        result = self._sync(
            _granted(*held),
            ["exercise_routes"],
            {"exercise_routes": lambda conn, start, end: ran.append(1) or 0},
        )
        assert ran == []
        assert result["exercise_routes"]["status"] == "skipped"
        assert "location" in result["exercise_routes"]["message"]
        conn = db.get_db(db_path)
        row = conn.execute(
            "SELECT status, notes FROM sync_log WHERE data_type = 'exercise_routes'"
        ).fetchone()
        conn.close()
        assert tuple(row) == (
            "skipped",
            "permission not granted (location); run google-health-mcp auth",
        )

    def test_a_token_that_has_not_recorded_its_scopes_is_refreshed_first(self, db_path):
        """Every token from before this release records none until its next
        refresh; without one the first sync after upgrading fetches what the
        grant lacks and fails on the 403."""
        held = [s for s in config.GOOGLE_SCOPE_READERS if s != "location"]
        granted = [None]
        forced = []

        def refresh(force=False):
            forced.append(force)
            granted[0] = _granted(*held)
            return "token"

        ran = []
        with (
            patch.object(sync_tools.auth, "granted_scopes", side_effect=lambda: granted[0]),
            patch.object(sync_tools.auth, "refresh_google_token", side_effect=refresh),
        ):
            result = sync_tools.run_sync(
                ["exercise_routes"],
                handlers={"exercise_routes": lambda c, s, e: ran.append(1) or 0},
            )
        assert forced == [True]
        assert ran == []
        assert result["exercise_routes"]["status"] == "skipped"

    @pytest.mark.parametrize("failure", [auth.TokenRefused, auth.RefreshNetworkError])
    def test_a_refresh_that_fails_leaves_the_sync_to_run_as_before(self, db_path, failure):
        ran = []
        with (
            patch.object(sync_tools.auth, "granted_scopes", return_value=None),
            patch.object(sync_tools.auth, "refresh_google_token", side_effect=failure("x")),
        ):
            result = sync_tools.run_sync(
                ["exercise_routes"],
                handlers={"exercise_routes": lambda c, s, e: ran.append(1) or 0},
            )
        assert ran == [1]
        assert result["exercise_routes"]["status"] == "ok"

    def test_an_offline_run_never_refreshes(self, db_path, monkeypatch):
        """Offline mode makes no network call."""
        monkeypatch.setattr(config, "OFFLINE_MODE", True)
        with (
            patch.object(sync_tools.auth, "granted_scopes", return_value=None),
            patch.object(sync_tools.auth, "refresh_google_token") as refresh,
        ):
            sync_tools.run_sync(["t"], handlers={"t": lambda c, s, e: 0})
        refresh.assert_not_called()

    def test_a_type_that_failed_today_is_still_retried(self, db_path, monkeypatch):
        """Only a sync or a skip counts toward the once-a-day gate."""
        monkeypatch.setattr(config, "OFFLINE_MODE", False)
        conn = db.get_db(db_path)
        db.log_sync(conn, "sleep", "error", notes="API error 500 for sleep list")
        conn.close()
        with patch.object(sync_tools, "run_sync") as run:
            sync_tools.auto_sync_if_stale("sleep")
        run.assert_called_once()

    def test_a_skip_for_both_reasons_names_both(self, db_path, monkeypatch):
        trimmed = {
            k: v for k, v in config.GOOGLE_SCOPE_READERS.items() if k != "activity_and_fitness"
        }
        monkeypatch.setattr(config, "GOOGLE_SCOPE_READERS", trimmed)
        held = [s for s in trimmed if s != "location"]
        result = self._sync(
            _granted(*held), ["exercise_routes"], {"exercise_routes": lambda c, s, e: 0}
        )
        message = result["exercise_routes"]["message"]
        assert "not granted (location)" in message
        assert "not requested by this install (activity_and_fitness)" in message
        conn = db.get_db(db_path)
        (notes,) = conn.execute(
            "SELECT notes FROM sync_log WHERE data_type = 'exercise_routes'"
        ).fetchone()
        conn.close()
        assert notes == message

    def test_a_token_that_records_its_scopes_is_not_refreshed_for_them(self, db_path):
        with (
            patch.object(
                sync_tools.auth,
                "granted_scopes",
                return_value=_granted(*config.GOOGLE_SCOPE_READERS),
            ),
            patch.object(sync_tools.auth, "refresh_google_token") as refresh,
        ):
            sync_tools.run_sync(["t"], handlers={"t": lambda c, s, e: 0})
        refresh.assert_not_called()

    def test_a_skipped_type_is_not_synced_again_on_every_query(self, db_path, monkeypatch):
        monkeypatch.setattr(config, "OFFLINE_MODE", False)
        held = [s for s in config.GOOGLE_SCOPE_READERS if s != "location"]
        monkeypatch.setattr(
            sync_tools, "GOOGLE_SYNC_HANDLERS", {"exercise_routes": lambda c, s, e: 0}
        )
        with patch.object(sync_tools.auth, "granted_scopes", return_value=_granted(*held)):
            for _ in range(3):
                sync_tools.auto_sync_if_stale("exercise_routes")
        conn = db.get_db(db_path)
        (rows,) = conn.execute(
            "SELECT COUNT(*) FROM sync_log WHERE data_type = 'exercise_routes'"
        ).fetchone()
        conn.close()
        assert rows == 1

    def test_a_skip_is_not_a_failure_to_the_cli(self, db_path, monkeypatch, capsys):
        monkeypatch.setattr(config, "OFFLINE_MODE", False)
        held = [s for s in config.GOOGLE_SCOPE_READERS if s != "location"]
        monkeypatch.setattr(
            sync_tools, "GOOGLE_SYNC_HANDLERS", {"exercise_routes": lambda c, s, e: 0}
        )
        with (
            patch.object(sync_tools.auth, "granted_scopes", return_value=_granted(*held)),
            patch("sys.argv", ["google-health-mcp", "sync", "--types", "exercise_routes"]),
        ):
            cli.main()
        assert "skipped" in capsys.readouterr().out

    def test_a_skip_is_not_a_failure_to_doctor(self, db_path):
        held = [s for s in config.GOOGLE_SCOPE_READERS if s != "location"]
        self._sync(_granted(*held), ["exercise_routes"], {"exercise_routes": lambda c, s, e: 0})
        (finding,) = doctor.check_sync_health()
        assert finding.severity == doctor.OK

    def test_a_scope_trimmed_from_the_request_skips_its_types(self, db_path, monkeypatch):
        """The README's way to ask for fewer permissions; without this each
        run would fetch the type anyway and fail on the 403."""
        trimmed = {k: v for k, v in config.GOOGLE_SCOPE_READERS.items() if k != "location"}
        monkeypatch.setattr(config, "GOOGLE_SCOPE_READERS", trimmed)
        ran = []
        result = self._sync(
            None, ["exercise_routes"], {"exercise_routes": lambda c, s, e: ran.append(1) or 0}
        )
        assert ran == []
        assert result["exercise_routes"]["status"] == "skipped"
        assert "not requested" in result["exercise_routes"]["message"]
        assert "auth" not in result["exercise_routes"]["message"]

    @pytest.mark.parametrize("granted", [None, "full"], ids=["unknown", "held"])
    def test_the_type_runs_when_the_scope_is_held_or_unknown(self, db_path, granted):
        """Unknown is not missing: skipping on it would stop every sync from a
        token written before scopes were recorded."""
        granted = _granted(*config.GOOGLE_SCOPE_READERS) if granted == "full" else None
        ran = []
        self._sync(
            granted, ["exercise_routes"], {"exercise_routes": lambda c, s, e: ran.append(1) or 0}
        )
        assert ran == [1]

    def test_the_account_records_skip_one_at_a_time(self, db_path, tmp_db):
        """Settings are held on every older grant; losing them with profile
        because the three were fetched together would be the failure again."""
        held = [s for s in config.GOOGLE_SCOPE_READERS if s != "profile"]
        profile = []
        with (
            patch.object(google_sync.auth, "granted_scopes", return_value=_granted(*held)),
            patch.multiple(
                google_sync.api,
                get_profile=lambda: profile.append(1) or {"age": 1},
                get_settings=lambda: {"timeZone": "Etc/UTC"},
                get_irn_profile=lambda: {"enrollmentStatus": False},
            ),
        ):
            count = google_sync.sync_account(tmp_db, None, None)
        assert profile == []
        assert count == 2
        assert set(db.query_account(tmp_db)) == {"settings", "irn_profile"}

    def test_a_refused_record_keeps_the_others_and_still_fails(self, db_path, tmp_db):
        """Where the grant is not recorded the refusal is the first sign; the
        records that were answered are stored, and the type still fails."""

        def refused():
            raise google_sync.api.HealthAuthError("refused")

        with (
            patch.object(google_sync.auth, "granted_scopes", return_value=None),
            patch.multiple(
                google_sync.api,
                get_profile=refused,
                get_settings=lambda: {"timeZone": "Etc/UTC"},
                get_irn_profile=lambda: {"enrollmentStatus": False},
            ),
            pytest.raises(google_sync.api.HealthAuthError),
        ):
            google_sync.sync_account(tmp_db, None, None)
        assert set(db.query_account(tmp_db)) == {"settings", "irn_profile"}


class TestEveryResponseCarriesIt:
    async def _call(self, result='{"data": 1}'):
        @require_auth
        async def tool():
            return result

        with (
            patch("google_health_mcp.helpers.GOOGLE_CLIENT_PATH") as client,
            patch("google_health_mcp.helpers.GOOGLE_TOKENS_PATH") as tokens,
        ):
            client.exists.return_value = True
            tokens.exists.return_value = True
            return json.loads(await tool())

    async def test_a_missing_scope_is_named_with_the_fix(self, db_path):
        _record(db_path, _SOME)
        body = await self._call()
        assert body["data"] == 1
        assert body["authorisation"] == missing_scopes_note(_SOME)
        held = set(config.GOOGLE_SCOPE_READERS) - set(_SOME)
        assert not any(name in body["authorisation"] for name in held)
        assert "google-health-mcp auth" in body["authorisation"]

    @pytest.mark.parametrize("missing", [[], None], ids=["none-missing", "unknown"])
    async def test_nothing_is_added_otherwise(self, db_path, missing):
        _record(db_path, missing)
        assert "authorisation" not in await self._call()

    async def test_an_offline_host_repeats_it(self, db_path, monkeypatch):
        """Offline hosts hold no token; the shared database is how they know."""
        monkeypatch.setattr(config, "OFFLINE_MODE", True)
        _record(db_path, _SOME)
        body = await self._call()
        assert body["offline_mode"] is True
        assert "authorisation" in body

    async def test_a_response_that_is_not_an_object_passes_through(self, db_path):
        _record(db_path, _SOME)
        assert await self._call("[1, 2]") == [1, 2]


class TestDoctorReportsIt:
    def _findings(self):
        return [f for f in doctor.check_authorisation() if f.check == doctor.MISSING_SCOPES]

    def _token(self, tmp_path, monkeypatch, scope):
        path = tmp_path / "google_tokens.json"
        body = {"access_token": "a", "refresh_token": "r"}
        if scope is not None:
            body["scope"] = scope
        path.write_text(json.dumps(body))
        monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", path)

    def test_a_grant_lacking_a_scope_is_a_warning_naming_it(self, tmp_path, monkeypatch, db_path):
        self._token(tmp_path, monkeypatch, _granted(*list(config.GOOGLE_SCOPE_READERS)[2:]))
        (finding,) = self._findings()
        assert finding.severity == doctor.WARN
        assert all(name in finding.detail for name in _SOME)
        held = set(config.GOOGLE_SCOPE_READERS) - set(_SOME)
        assert not any(name in finding.detail for name in held)
        assert "google-health-mcp auth" in finding.fix

    def test_the_slug_is_an_interface(self):
        """Documented for monitors; renaming it silences them without an error."""
        assert doctor.MISSING_SCOPES == "missing-scopes"

    @pytest.mark.parametrize("contents", ["[]", "{not json", '{"scope": 7}'])
    def test_an_unreadable_token_is_unknown_rather_than_a_crash(
        self, tmp_path, monkeypatch, db_path, contents
    ):
        path = tmp_path / "google_tokens.json"
        path.write_text(contents)
        monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", path)
        (finding,) = self._findings()
        assert finding.severity == doctor.OK
        assert "not recorded" in finding.detail

    def test_a_full_grant_is_ok(self, tmp_path, monkeypatch, db_path):
        self._token(tmp_path, monkeypatch, _granted(*config.GOOGLE_SCOPE_READERS))
        (finding,) = self._findings()
        assert finding.severity == doctor.OK

    def test_the_token_is_preferred_to_the_record(self, tmp_path, monkeypatch, db_path):
        """The record is only as fresh as the last sync; the token is current."""
        _record(db_path, _SOME)
        self._token(tmp_path, monkeypatch, _granted(*config.GOOGLE_SCOPE_READERS))
        (finding,) = self._findings()
        assert finding.severity == doctor.OK

    def test_an_offline_host_reads_the_record(self, tmp_path, monkeypatch, db_path):
        """Even with a token file present, which an offline host does not own."""
        monkeypatch.setattr(config, "OFFLINE_MODE", True)
        self._token(tmp_path, monkeypatch, _granted(*config.GOOGLE_SCOPE_READERS))
        _record(db_path, _SOME)
        (finding,) = self._findings()
        assert finding.severity == doctor.WARN
        assert "syncing host" in finding.fix

    def test_unknown_says_so_rather_than_passing(self, tmp_path, monkeypatch, db_path):
        self._token(tmp_path, monkeypatch, None)
        (finding,) = self._findings()
        assert finding.severity == doctor.OK
        assert "not recorded" in finding.detail

    def test_it_is_one_of_the_checks_doctor_runs(self, tmp_path, monkeypatch, db_path):
        self._token(tmp_path, monkeypatch, _granted())
        assert any(f.check == doctor.MISSING_SCOPES for f in doctor.run_checks())


class TestSyncPrintsIt:
    def test_the_command_names_it_on_stderr(self, db_path, monkeypatch, capsys):
        monkeypatch.setattr(config, "OFFLINE_MODE", False)

        def fake_run_sync(types, days, since=None, until=None, handlers=None):
            _record(db_path, _SOME)
            return {"sleep": {"status": "ok", "records": 0, "range": ""}}

        monkeypatch.setattr(cli.sync_tools, "run_sync", fake_run_sync)
        with patch("sys.argv", ["google-health-mcp", "sync", "--types", "sleep"]):
            cli.main()
        err = capsys.readouterr().err
        assert missing_scopes_note(_SOME) in err

    def test_nothing_is_printed_when_nothing_is_missing(self, db_path, monkeypatch, capsys):
        monkeypatch.setattr(config, "OFFLINE_MODE", False)

        def fake_run_sync(types, days, since=None, until=None, handlers=None):
            _record(db_path, [])
            return {"sleep": {"status": "ok", "records": 0, "range": ""}}

        monkeypatch.setattr(cli.sync_tools, "run_sync", fake_run_sync)
        with patch("sys.argv", ["google-health-mcp", "sync", "--types", "sleep"]):
            cli.main()
        assert capsys.readouterr().err == ""
