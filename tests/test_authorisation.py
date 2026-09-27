"""A permission the grant lacks is named wherever the person will see it.

A grant never gains scopes on refresh, so a release that asks for a new one
leaves every existing install without it until the user re-consents. The
syncing host records which requested scopes the grant lacks; every tool
response, `doctor` and `sync` repeat it, including on offline hosts, which
hold no token and read only the shared database.
"""

import json
from unittest.mock import patch

import pytest

from google_health_mcp import auth, cli, config, db, doctor
from google_health_mcp.helpers import require_auth
from google_health_mcp.tools import sync_tools

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

    def test_a_token_that_recorded_nothing_records_unknown(self, db_path):
        _record(db_path, _SOME)
        self._sync(None)
        assert db.recorded_missing_scopes() is None

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
        for name in _SOME:
            assert name in body["authorisation"]
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
        assert "google-health-mcp auth" in finding.fix

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

    def test_an_offline_host_reads_the_record(self, monkeypatch, db_path):
        monkeypatch.setattr(config, "OFFLINE_MODE", True)
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
        assert all(name in err for name in _SOME)
        assert "google-health-mcp auth" in err
