"""Tests for the doctor preflight diagnostic.

The properties pinned here are the ones that make a diagnostic trustworthy:
it must not change the thing it inspects, must not leak credentials into
output a user will paste into a bug report, and must survive every broken
setup it exists to describe.
"""

import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import date, timedelta
from importlib.metadata import PackageNotFoundError, version

import pytest

from google_health_mcp import db, doctor

# Sentinels chosen so a leak is unambiguous in any output format.
FAKE_ACCESS_TOKEN = "ACCESS-TOKEN-MUST-NOT-APPEAR-A1B2C3"
FAKE_REFRESH_TOKEN = "REFRESH-TOKEN-MUST-NOT-APPEAR-D4E5F6"
FAKE_CLIENT_SECRET = "CLIENT-SECRET-MUST-NOT-APPEAR-G7H8I9"


@pytest.fixture
def setup_paths(tmp_path, monkeypatch):
    """Point config and DB at a tmp dir without creating anything in it."""
    config_dir = tmp_path / "config"
    db_path = tmp_path / "data" / "google_health.db"
    monkeypatch.setattr("google_health_mcp.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr(
        "google_health_mcp.config.GOOGLE_CLIENT_PATH", config_dir / "google_client.json"
    )
    monkeypatch.setattr(
        "google_health_mcp.config.GOOGLE_TOKENS_PATH", config_dir / "google_tokens.json"
    )
    monkeypatch.setattr("google_health_mcp.config.DB_PATH", db_path)
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
    return config_dir, db_path


def _write_credentials(config_dir, expires_at=None, tokens=None):
    """A healthy Google install: the downloaded Desktop-client file and a token.

    The client file keeps Google's own `installed` wrapper, which is the shape
    auth.py reads and the shape a user drops in unedited.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "google_client.json").write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "123456-fictional.apps.googleusercontent.com",
                    "client_secret": FAKE_CLIENT_SECRET,
                }
            }
        )
    )
    stored = {
        "access_token": FAKE_ACCESS_TOKEN,
        "refresh_token": FAKE_REFRESH_TOKEN,
        "expires_at": expires_at if expires_at is not None else time.time() + 3600,
    }
    stored.update(tokens or {})
    (config_dir / "google_tokens.json").write_text(json.dumps(stored))


class _FakeCursor:
    """Minimal stand-in for a sqlite3 cursor holding one row."""

    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


# --- Invariant 1: doctor never mutates what it inspects ---


def test_does_not_create_the_database(setup_paths):
    """The wrong-path check is only meaningful if doctor cannot manufacture a DB."""
    _config_dir, db_path = setup_paths

    doctor.run_checks()

    assert not db_path.exists()
    assert not db_path.parent.exists()


def test_does_not_create_the_config_directory(setup_paths):
    config_dir, _db_path = setup_paths

    doctor.run_checks()

    assert not config_dir.exists()


def test_does_not_modify_an_existing_database(setup_paths, tmp_path):
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    conn.close()
    before = db_path.read_bytes()

    doctor.run_checks()

    assert db_path.read_bytes() == before


# --- Invariant 2: doctor never prints secret values ---


@pytest.mark.parametrize("as_json", [False, True])
def test_never_prints_token_or_secret_values(setup_paths, capsys, as_json):
    """Both output formats, driven through the real credential files.

    The JSON payload is a second surface onto the same findings, so it inherits
    this rule rather than getting a pin of its own - a test that builds its own
    findings never puts a secret anywhere and would pass over code that dumped
    the whole token file.
    """
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)

    doctor.run_doctor(as_json=as_json)

    out = capsys.readouterr()
    combined = out.out + out.err
    assert FAKE_ACCESS_TOKEN not in combined
    assert FAKE_REFRESH_TOKEN not in combined
    assert FAKE_CLIENT_SECRET not in combined


def test_findings_carry_no_secret_values(setup_paths):
    """Belt and braces: the structured findings are what a caller might log."""
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)

    blob = " ".join(f"{f.name} {f.detail} {f.fix or ''}" for f in doctor.run_checks())

    assert FAKE_ACCESS_TOKEN not in blob
    assert FAKE_REFRESH_TOKEN not in blob
    assert FAKE_CLIENT_SECRET not in blob


# --- Invariant 3: doctor survives every broken setup ---


def test_survives_completely_missing_setup(setup_paths):
    findings = doctor.run_checks()

    assert any(f.severity == doctor.FAIL for f in findings)


def test_survives_unparseable_config_and_token_files(setup_paths):
    config_dir, _db_path = setup_paths
    config_dir.mkdir(parents=True)
    (config_dir / "google_client.json").write_text("{not json")
    # Malformed but secret-bearing: the error path must not echo file content.
    (config_dir / "google_tokens.json").write_text(
        '{"access_token": "' + FAKE_ACCESS_TOKEN + '" NOT JSON'
    )

    findings = doctor.run_checks()

    assert any(
        "unreadable" in f.detail.lower() or "malformed" in f.detail.lower() for f in findings
    )
    assert all(FAKE_ACCESS_TOKEN not in f"{f.detail} {f.fix or ''}" for f in findings)


def test_survives_credential_files_that_are_not_utf8(setup_paths):
    config_dir, _db_path = setup_paths
    config_dir.mkdir(parents=True)
    (config_dir / "google_client.json").write_bytes(b"\xff\xfe not text")
    (config_dir / "google_tokens.json").write_bytes(b"\xff\xfe not text")

    findings = doctor.run_checks()

    assert any(f.severity == doctor.FAIL for f in findings)


def test_survives_an_expires_at_in_milliseconds(setup_paths):
    """Seconds-vs-milliseconds is a routine OAuth slip; it must not crash."""
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir, expires_at=time.time() * 1000)

    findings = doctor.run_checks()

    assert any(f.name == "access token" and f.severity == doctor.WARN for f in findings)


def test_one_failing_check_does_not_abort_the_report(setup_paths, monkeypatch):
    def explode():
        raise RuntimeError(FAKE_ACCESS_TOKEN)

    monkeypatch.setattr(doctor, "check_database", explode)

    findings = doctor.run_checks()

    assert any(f.severity == doctor.FAIL and "could not run" in f.detail for f in findings)
    # The other checks still ran.
    assert any(f.name == "config path" for f in findings)
    # An exception message can be built from file content, so only the type is
    # reported - never str(e).
    assert all(FAKE_ACCESS_TOKEN not in f"{f.detail} {f.fix or ''}" for f in findings)


def test_survives_a_corrupt_database(setup_paths):
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    db_path.write_text("this is not a sqlite database")

    findings = doctor.run_checks()

    assert any(f.severity == doctor.FAIL and "database" in f.name.lower() for f in findings)


def test_reports_a_failed_integrity_check(setup_paths, monkeypatch):
    """A file that opens cleanly but reports corruption must still fail.

    Page-level damage makes `PRAGMA integrity_check` raise (covered above), so
    the branch that reads its *result* is reached only for the corruption
    classes SQLite describes rather than rejects. Driving the pragma directly
    is what pins that contract: anything other than "ok" is a failure.
    """
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    conn.close()

    class ReportsCorruption:
        """Delegates to the real connection, but says the database is damaged."""

        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, *args):
            if "integrity_check" in sql:
                return _FakeCursor(("row 42 missing from index idx_sleep_date",))
            return self._conn.execute(sql, *args)

    real_open = doctor._open_db_readonly

    @contextmanager
    def open_reporting_corruption(path):
        with real_open(path) as conn:
            yield ReportsCorruption(conn)

    monkeypatch.setattr(doctor, "_open_db_readonly", open_reporting_corruption)

    findings = doctor.run_checks()

    assert any(f.severity == doctor.FAIL and "database" in f.name.lower() for f in findings)


# --- Individual checks ---


def test_flags_offline_env_var_that_parses_as_off(setup_paths, monkeypatch):
    """`GOOGLE_HEALTH_MCP_OFFLINE=ture` is falsy and silently disables offline mode."""
    monkeypatch.setenv("GOOGLE_HEALTH_MCP_OFFLINE", "ture")

    findings = doctor.run_checks()

    assert any(
        "GOOGLE_HEALTH_MCP_OFFLINE" in f.detail and f.severity == doctor.WARN for f in findings
    )


def test_does_not_flag_a_valid_offline_value(setup_paths, monkeypatch):
    monkeypatch.setenv("GOOGLE_HEALTH_MCP_OFFLINE", "1")
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)

    findings = doctor.run_checks()

    assert not any("parses as OFF" in f.detail for f in findings)


def test_detects_a_missing_schema_column(setup_paths):
    """_migrate covers two columns; any other drift must still be reported."""
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    conn.execute("DROP TABLE spo2")
    conn.execute("CREATE TABLE spo2 (date TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    assert any("spo2" in f.detail and f.severity == doctor.FAIL for f in findings)


def test_reports_silent_sync_failure_from_sync_log(setup_paths):
    """The mode behind a silent outage: cache served, auth failing, no error."""
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.log_sync(conn, "sleep", "auth_error", 0, notes="auth: token refresh failed")
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    # Assert on the named check, not on a substring: "auth" also appears in the
    # headless-browser warning, which fires on any CI runner and would let this
    # pass with the sync-log check deleted entirely.
    assert any(f.name == "sync log" and f.severity == doctor.FAIL for f in findings)


def test_healthy_setup_reports_no_failures(setup_paths):
    config_dir, db_path = setup_paths
    _write_credentials(config_dir)
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.log_sync(conn, "sleep", "ok", 1)
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    assert not [f for f in findings if f.severity == doctor.FAIL]


def test_exit_code_is_zero_when_healthy(setup_paths, capsys):
    config_dir, db_path = setup_paths
    _write_credentials(config_dir)
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.log_sync(conn, "sleep", "ok", 1)
    conn.commit()
    conn.close()

    assert doctor.run_doctor() == 0


def test_exit_code_is_nonzero_when_broken(setup_paths, capsys):
    assert doctor.run_doctor() != 0


def test_a_client_id_that_is_not_googles_is_named_as_such(setup_paths):
    """Every Google client id ends .apps.googleusercontent.com, so one that does
    not cannot complete a token exchange - and the report has to say which half
    of the file is wrong, not just that setup failed."""
    config_dir, _db_path = setup_paths
    config_dir.mkdir(parents=True)
    (config_dir / "google_client.json").write_text(
        json.dumps({"installed": {"client_id": "23ABCD"}})
    )

    app = [f for f in doctor.run_checks() if f.name == "app config"]

    assert app and app[0].severity == doctor.FAIL
    assert "client_id" in app[0].detail


def test_a_configured_google_install_is_not_told_it_is_unconfigured(setup_paths):
    """The credential checks read the files this build actually authorises with."""
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)

    findings = doctor.run_checks()

    assert not [
        f for f in findings if f.name in ("app config", "credentials") and f.severity == doctor.FAIL
    ]


def test_a_web_client_file_is_refused_by_name(setup_paths):
    """A Web client fails at consent with redirect_uri_mismatch, far from the cause."""
    config_dir, _db_path = setup_paths
    config_dir.mkdir(parents=True)
    (config_dir / "google_client.json").write_text(
        json.dumps({"web": {"client_id": "123-abc.apps.googleusercontent.com"}})
    )

    findings = doctor.run_checks()
    app = [f for f in findings if f.name == "app config"]

    assert app and app[0].severity == doctor.FAIL
    assert "Desktop" in f"{app[0].detail} {app[0].fix or ''}"


def test_a_hand_written_client_file_is_named_as_the_wrong_shape(setup_paths):
    """A bare client id is what a hand-written file looks like, and what every
    other OAuth client's JSON looks like - the reader is holding something that
    does contain a client id, so "not a client file" alone reads as wrong."""
    config_dir, _db_path = setup_paths
    config_dir.mkdir(parents=True)
    (config_dir / "google_client.json").write_text(
        json.dumps({"client_id": "123-abc.apps.googleusercontent.com"})
    )

    app = [f for f in doctor.run_checks() if f.name == "app config"]

    assert app and app[0].severity == doctor.FAIL
    assert "installed" in app[0].detail


def test_a_short_lived_refresh_token_is_reported_before_it_dies(setup_paths):
    """The one failure that arrives a week later with nothing to point at.

    Google grants a 7-day refresh token while an app's publishing status is
    Testing, and the console's Audience page can read "In production" while
    the token server disagrees.
    """
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir, tokens={"refresh_token_expires_at": time.time() + 6 * 86400})

    findings = [f for f in doctor.run_checks() if f.name == "refresh token"]
    reported = [f for f in findings if f.severity == doctor.FAIL]

    assert len(reported) == 1
    assert "Publish" in f"{reported[0].detail} {reported[0].fix or ''}"
    # The date, not the interval: what the user has to act before.
    assert time.strftime("%Y-%m-%d", time.localtime(time.time() + 6 * 86400)) in reported[0].detail


def test_an_expiry_already_past_is_not_described_as_coming(setup_paths):
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir, tokens={"refresh_token_expires_at": time.time() - 86400})

    reported = [
        f for f in doctor.run_checks() if f.name == "refresh token" and f.severity == doctor.FAIL
    ]

    assert reported and "past it" in reported[0].detail


def test_a_token_with_no_recorded_expiry_is_not_reported(setup_paths):
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)

    assert not [
        f for f in doctor.run_checks() if f.name == "refresh token" and f.severity != doctor.OK
    ]


@pytest.mark.parametrize("value", [None, "later", True], ids=["null", "str", "bool"])
def test_an_unusable_expiry_is_not_read_as_one(setup_paths, value):
    """A value that is not a timestamp says nothing, and must not assert anything."""
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir, tokens={"refresh_token_expires_at": value})

    assert not [
        f for f in doctor.run_checks() if f.name == "refresh token" and f.severity == doctor.FAIL
    ]


def test_the_two_refresh_token_checks_do_not_contradict_each_other(setup_paths):
    """They share a finding name, so a verdict-shaped OK line reads as an answer.

    A short-lived token used yesterday is both facts at once: the expiry is
    coming, and the file is being rewritten.
    """
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir, tokens={"refresh_token_expires_at": time.time() + 86400})

    findings = [f for f in doctor.run_checks() if f.name == "refresh token"]
    passing = [f for f in findings if f.severity == doctor.OK]

    assert len(findings) == 2
    assert passing and "rewritten" in passing[0].detail
    assert "expiry" not in passing[0].detail


def test_a_client_id_that_cannot_authenticate_is_a_failure(setup_paths):
    """A warning here exits 0 and prints "No blocking problems" over a dead setup.

    Everything else is written healthy, so the grade under test is the only
    thing that can fail the run.
    """
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)
    (config_dir / "google_client.json").write_text(
        json.dumps({"installed": {"client_id": "23ABCD", "client_secret": FAKE_CLIENT_SECRET}})
    )

    app = [f for f in doctor.run_checks() if f.name == "app config"]

    assert app and app[0].severity == doctor.FAIL
    assert doctor.run_doctor() != 0


def test_a_client_file_with_no_secret_is_reported(setup_paths):
    """Every token exchange is refused as an unknown client, far from the cause."""
    config_dir, _db_path = setup_paths
    config_dir.mkdir(parents=True)
    (config_dir / "google_client.json").write_text(
        json.dumps({"installed": {"client_id": "123-abc.apps.googleusercontent.com"}})
    )

    app = [f for f in doctor.run_checks() if f.name == "app config"]

    assert app and app[0].severity == doctor.FAIL
    assert "client_secret" in app[0].detail


def test_a_missing_refresh_token_is_not_reassured_about(setup_paths):
    """ "No 7-day expiry marker" beside "missing: refresh_token" reads as health."""
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)
    (config_dir / "google_tokens.json").write_text(
        json.dumps({"access_token": FAKE_ACCESS_TOKEN, "expires_at": time.time() + 3600})
    )

    findings = doctor.run_checks()

    assert not [f for f in findings if f.name == "refresh token"]
    assert any(f.name == "credentials" and f.severity == doctor.FAIL for f in findings)


def test_reports_an_expired_access_token_without_calling_the_network(setup_paths, monkeypatch):
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir, expires_at=time.time() - 60)

    def fail_on_network(*_args, **_kwargs):
        raise AssertionError("doctor made a network call")

    monkeypatch.setattr("urllib.request.urlopen", fail_on_network)

    findings = doctor.run_checks()

    assert any("expired" in f.detail.lower() for f in findings)


def test_reports_database_freshness(setup_paths):
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.save_sleep(conn, {"date": "2026-03-10", "total_minutes": 420, "efficiency": 90})
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    assert any("2026-03-10" in f.detail for f in findings)


def test_opens_the_database_read_only(setup_paths):
    """A read-only open is what guarantees the no-mutation property above."""
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    conn.close()

    with doctor._open_db_readonly(db_path) as ro:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE should_not_work (x INTEGER)")


def test_read_only_open_survives_a_path_needing_uri_escaping(tmp_path):
    """The path goes into a URI, so `#` would otherwise start a fragment.

    Unescaped, `?mode=ro` lands inside that fragment and is ignored, handing
    back a writable connection to a truncated path - which also creates a
    stray file. Both invariants of this module depend on the escaping.
    """
    awkward = tmp_path / "weird #dir"
    awkward.mkdir()
    db_path = awkward / "google_health.db"
    db.get_db(db_path).close()

    with doctor._open_db_readonly(db_path) as ro:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("CREATE TABLE should_not_work (x INTEGER)")
        assert ro.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] == 0

    assert not (tmp_path / "weird ").exists()


def test_creates_nothing_under_a_path_needing_uri_escaping(tmp_path, monkeypatch):
    db_path = tmp_path / "weird #dir" / "google_health.db"
    monkeypatch.setattr("google_health_mcp.config.CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(
        "google_health_mcp.config.GOOGLE_CLIENT_PATH", tmp_path / "config" / "google_client.json"
    )
    monkeypatch.setattr(
        "google_health_mcp.config.GOOGLE_TOKENS_PATH", tmp_path / "config" / "google_tokens.json"
    )
    monkeypatch.setattr("google_health_mcp.config.DB_PATH", db_path)
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)

    doctor.run_checks()

    assert list(tmp_path.iterdir()) == []


def test_a_column_a_migration_would_restore_is_not_reported(setup_paths, monkeypatch):
    """Reporting a self-healing column attaches the "re-create and re-import"
    remedy, sending a user to destroy a cache that repairs itself on next open.

    There are no migrations yet - a first release has no older database to
    carry forward - so the excusing has to be driven rather than waited for.
    """
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    conn.execute("ALTER TABLE sleep DROP COLUMN sessions")
    conn.commit()
    conn.close()
    monkeypatch.setattr(doctor, "_SELF_HEALING_COLUMNS", {("sleep", "sessions")})

    findings = doctor.run_checks()

    assert not [f for f in findings if f.name == "schema" and f.severity == doctor.FAIL]


def test_a_column_no_migration_restores_is_reported(setup_paths):
    """The other half: an unrepairable gap does break its sync, and must say so."""
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    conn.execute("ALTER TABLE sleep DROP COLUMN sessions")
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    assert [f for f in findings if f.name == "schema" and f.severity == doctor.FAIL]


def test_offline_host_with_a_read_only_cache_is_healthy(setup_paths, monkeypatch):
    """One host syncs and the rest read: the documented multi-host layout."""
    _config_dir, db_path = setup_paths
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.save_sleep(conn, {"date": date.today().isoformat(), "total_minutes": 420, "efficiency": 90})
    db.log_sync(conn, "sleep", "ok", 1)
    conn.commit()
    conn.close()
    db_path.chmod(0o444)

    findings = doctor.run_checks()

    assert not [f for f in findings if f.severity != doctor.OK]


def test_offline_host_does_not_blame_itself_for_a_peers_sync_failure(setup_paths, monkeypatch):
    _config_dir, db_path = setup_paths
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.log_sync(conn, "sleep", "auth_error", 0, notes="auth: token refresh failed")
    conn.commit()
    conn.close()

    findings = doctor.run_checks()
    sync_log = [f for f in findings if f.name == "sync log"]

    assert sync_log and sync_log[0].severity == doctor.WARN
    assert "syncing host" in sync_log[0].detail


def test_a_type_that_recovered_is_not_still_reported_as_failing(setup_paths):
    _config_dir, db_path = setup_paths
    _write_credentials(_config_dir)
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.log_sync(conn, "sleep", "auth_error", 0, notes="auth: token refresh failed")
    db.log_sync(conn, "sleep", "ok", 5)
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    assert not [f for f in findings if f.name == "sync log" and f.severity == doctor.FAIL]


def test_warns_when_the_cache_has_stopped_updating(setup_paths):
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    stale = (date.today() - timedelta(days=30)).isoformat()
    db.save_sleep(conn, {"date": stale, "total_minutes": 420, "efficiency": 90})
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    assert any(f.name == "cache" and f.severity == doctor.WARN for f in findings)


def test_a_current_cache_is_not_called_stale(setup_paths):
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.save_sleep(conn, {"date": date.today().isoformat(), "total_minutes": 420, "efficiency": 90})
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    assert any(f.name == "cache" and f.severity == doctor.OK for f in findings)


@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="root bypasses file permission bits",
)
def test_reports_an_unwritable_token_file(setup_paths):
    """Every refresh writes the new access token, so an unwritable file fails
    each one after the network call has already succeeded."""
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)
    (config_dir / "google_tokens.json").chmod(0o444)

    findings = doctor.run_checks()

    assert any(f.name == "credentials" and f.severity == doctor.FAIL for f in findings)


@pytest.mark.skipif(
    sys.platform != "win32" and os.geteuid() == 0,
    reason="root bypasses file permission bits",
)
def test_a_read_only_config_directory_is_not_a_problem(setup_paths):
    """auth.py rewrites the token file in place, so only the file must be writable."""
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)
    config_dir.chmod(0o555)
    try:
        findings = doctor.run_checks()
    finally:
        config_dir.chmod(0o755)

    assert not [f for f in findings if f.name == "credentials" and f.severity == doctor.FAIL]


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_recognised_off_values_are_not_reported_as_typos(setup_paths, monkeypatch, value):
    monkeypatch.setenv("GOOGLE_HEALTH_MCP_OFFLINE", value)

    findings = doctor.run_checks()

    assert not [f for f in findings if f.name == "offline mode" and f.severity != doctor.OK]


def test_reports_a_rate_limited_sync(setup_paths):
    _config_dir, db_path = setup_paths
    _write_credentials(_config_dir)
    db_path.parent.mkdir(parents=True)
    conn = db.get_db(db_path)
    db.log_sync(conn, "sleep", "partial", 0, notes="rate limited")
    conn.commit()
    conn.close()

    findings = doctor.run_checks()

    assert any(f.name == "sync log" and f.severity == doctor.WARN for f in findings)


def test_absent_tables_are_reported_without_claiming_the_schema_matches(setup_paths):
    """An empty file has no tables at all; "matches this version" would be false."""
    _config_dir, db_path = setup_paths
    db_path.parent.mkdir(parents=True)
    db_path.touch()

    findings = doctor.run_checks()
    schema = [f for f in findings if f.name == "schema"]

    assert schema and all(f.severity != doctor.OK for f in schema)


def test_read_only_open_handles_a_non_utf8_path(tmp_path):
    """Such a name reaches Python as surrogate escapes, which `quote` refuses.

    Encoding the path through os.fsencode is what keeps this working; passing
    the str straight to `quote` raises UnicodeEncodeError.
    """
    awkward = tmp_path / os.fsdecode(b"raw\xff name")
    try:
        awkward.mkdir()
    except (UnicodeEncodeError, OSError):
        pytest.skip("filesystem rejects non-UTF-8 names")
    db_path = awkward / "google_health.db"
    db.get_db(db_path).close()

    with doctor._open_db_readonly(db_path) as ro:
        assert ro.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] == 0


def test_read_only_open_handles_a_path_with_a_percent_sign(tmp_path):
    """Guards the double-encoding class that the escaping fix could introduce."""
    awkward = tmp_path / "with%25pct"
    awkward.mkdir()
    db_path = awkward / "google_health.db"
    db.get_db(db_path).close()

    with doctor._open_db_readonly(db_path) as ro:
        assert ro.execute("SELECT COUNT(*) FROM sleep").fetchone()[0] == 0


def test_warns_when_the_token_file_is_older_than_the_refresh_lifetime(setup_paths):
    config_dir, _db_path = setup_paths
    _write_credentials(config_dir)
    tokens = config_dir / "google_tokens.json"
    ancient = time.time() - timedelta(days=200).total_seconds()
    os.utime(tokens, (ancient, ancient))

    findings = doctor.run_checks()

    assert any(f.name == "refresh token" and f.severity == doctor.WARN for f in findings)


# os.access ignores permission bits under CAP_DAC_OVERRIDE.
# os.geteuid and os.mkfifo are POSIX-only, and the marker below is evaluated at
# import: on Windows an attribute error here would take the whole module out
# rather than skipping a test.
_POSIX = sys.platform != "win32"
skip_non_posix = pytest.mark.skipif(not _POSIX, reason="POSIX-only file semantics")
skip_as_root = pytest.mark.skipif(
    _POSIX and os.geteuid() == 0, reason="root bypasses permission bits"
)


def _findings_named(findings, name):
    return [f for f in findings if f.name == name]


def _seed_cache(db_path, when):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.get_db(db_path)
    conn.execute("INSERT OR REPLACE INTO activity (date, steps) VALUES (?, ?)", (when, 5000))
    conn.commit()
    conn.close()


@skip_as_root
@skip_non_posix
def test_an_unreadable_database_is_not_called_corrupt(setup_paths):
    """The corruption remedy is to delete the cache - never say it on a guess."""
    _, db_path = setup_paths
    _seed_cache(db_path, date.today().isoformat())
    os.chmod(db_path, 0o000)
    try:
        findings = doctor.check_database()
        detail = " ".join(f.detail for f in _findings_named(findings, "database"))
        fixes = " ".join(f.fix or "" for f in _findings_named(findings, "database"))
        assert any(f.severity == doctor.FAIL for f in _findings_named(findings, "database"))
        assert "not readable" in detail
        assert "delete" not in fixes.replace("do not delete", "")
    finally:
        os.chmod(db_path, 0o600)


def test_a_database_that_will_not_open_is_not_called_corrupt(setup_paths, monkeypatch):
    """A sync holding the write lock must not be reported as damage."""
    _, db_path = setup_paths
    _seed_cache(db_path, date.today().isoformat())

    def refuse(path):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(doctor, "_open_db_readonly", refuse)
    findings = doctor.check_database()
    detail = " ".join(f.detail for f in _findings_named(findings, "database"))
    fixes = " ".join(f.fix or "" for f in _findings_named(findings, "database"))
    assert any(f.severity == doctor.FAIL for f in _findings_named(findings, "database"))
    assert "not implicated" in detail
    assert "delete" not in fixes and "backup" not in fixes


@skip_as_root
@skip_non_posix
def test_a_read_only_database_directory_is_reported(setup_paths):
    """SQLite writes its journal beside the database, so the directory counts."""
    _, db_path = setup_paths
    _seed_cache(db_path, date.today().isoformat())
    os.chmod(db_path.parent, 0o500)
    try:
        findings = doctor.check_database()
        assert any(f.severity == doctor.WARN for f in _findings_named(findings, "database"))
        assert "sync will fail" in " ".join(f.detail for f in _findings_named(findings, "database"))
    finally:
        os.chmod(db_path.parent, 0o700)


@skip_non_posix
def test_a_fifo_at_the_database_path_does_not_hang(setup_paths):
    _, db_path = setup_paths
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(db_path)
    try:
        findings = doctor.check_database()
        assert any(f.severity == doctor.FAIL for f in _findings_named(findings, "database"))
        assert "not a regular file" in " ".join(
            f.detail for f in _findings_named(findings, "database")
        )
    finally:
        db_path.unlink()


def test_cache_age_is_measured_in_whole_days(setup_paths):
    """Measuring from the current time makes staleness depend on the hour."""
    _, db_path = setup_paths
    _seed_cache(db_path, (date.today() - timedelta(days=3)).isoformat())
    assert all(f.severity == doctor.OK for f in _findings_named(doctor.check_database(), "cache"))


def test_a_cache_one_day_past_the_threshold_warns(setup_paths):
    _, db_path = setup_paths
    _seed_cache(db_path, (date.today() - timedelta(days=4)).isoformat())
    assert any(f.severity == doctor.WARN for f in _findings_named(doctor.check_database(), "cache"))


def test_a_malformed_cached_date_does_not_read_as_fresh(setup_paths):
    """Dates are stored as the API returned them, and junk sorts high."""
    _, db_path = setup_paths
    _seed_cache(db_path, "not-a-date")
    findings = _findings_named(doctor.check_database(), "cache")
    assert any(f.severity == doctor.WARN for f in findings)
    assert "not a calendar date" in " ".join(f.detail for f in findings)


def test_an_offline_host_is_not_told_to_run_sync(setup_paths, monkeypatch):
    """cli.py refuses `sync` offline, so advising it sends the user to exit 1."""
    _, db_path = setup_paths
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.get_db(db_path).close()

    fixes = " ".join(f.fix or "" for f in doctor.check_database())
    assert "google-health-mcp sync" not in fixes
    assert "cache-only" in fixes


def test_a_live_host_is_still_told_to_run_sync(setup_paths):
    _, db_path = setup_paths
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db.get_db(db_path).close()
    assert "google-health-mcp sync" in " ".join(f.fix or "" for f in doctor.check_database())


def test_macos_is_not_reported_as_headless(setup_paths, monkeypatch):
    """Neither display variable is set on macOS, yet the browser opens."""
    monkeypatch.setattr(doctor.sys, "platform", "darwin")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert not _findings_named(doctor.check_auth_prerequisites(), "auth browser")


def test_a_headless_linux_host_is_told_how_to_tunnel(setup_paths, monkeypatch):
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    findings = _findings_named(doctor.check_auth_prerequisites(), "auth browser")
    assert findings and findings[0].severity == doctor.WARN
    # The port the callback actually listens on, not a literal: advising a
    # tunnel to the wrong one sends a headless user to a port nothing binds.
    port = doctor.config.GOOGLE_CALLBACK_PORT
    assert f"ssh -L {port}:localhost:{port}" in findings[0].fix


def test_the_port_probed_is_the_one_the_callback_listens_on(setup_paths, monkeypatch):
    """Probing any other port answers a question nobody asked.

    A free port reads as "auth will work" while the real one is occupied, and
    an occupied one reads as a conflict that cannot affect the flow. The
    advice text is pinned above, but that is built from the constant even
    when the probe is not.

    An ephemeral port is occupied and the constant pointed at it, rather than
    binding the real one: the real port may legitimately be in use on the
    machine running the tests, which would make this pass for the wrong
    reason.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("localhost", 0))
        monkeypatch.setattr(doctor.config, "GOOGLE_CALLBACK_PORT", occupied.getsockname()[1])

        findings = _findings_named(doctor.check_auth_prerequisites(), "auth callback")

    assert findings and findings[0].severity == doctor.WARN
    assert "cannot receive the OAuth callback" in findings[0].detail


def test_an_empty_path_variable_is_not_reported_as_the_default(setup_paths, monkeypatch):
    """config.py reads the variable raw, so empty is still an override."""
    monkeypatch.setenv("GOOGLE_HEALTH_MCP_DB_PATH", "")
    detail = _findings_named(doctor.check_environment(), "database path")[0].detail
    assert "default" not in detail
    assert "empty" in detail


def test_a_dead_token_fails_rather_than_warns(setup_paths):
    """A revoked token is the failure that will not clear itself.

    sync_tools logged these as "error", which doctor grades WARN, so a setup
    that had not synced for weeks reported "No blocking problems" and exited
    zero.
    """
    _, db_path = setup_paths
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.get_db(db_path)
    db.log_sync(conn, "sleep", "auth_error", notes="could not obtain a token")
    conn.commit()
    conn.close()

    findings = doctor.check_sync_health()
    assert any(f.severity == doctor.FAIL for f in findings)


def test_a_sync_auth_failure_lands_as_the_status_doctor_grades_as_auth(setup_paths, monkeypatch):
    """The writer and the reader of sync_log.status must agree on the word.

    Exercised through run_sync rather than by reading its source, so a change
    to either side breaks this rather than passing a grep. The connection is
    pinned explicitly: db.py binds DB_PATH at import, so patching config alone
    would send run_sync to a different database than doctor reads.
    """
    from google_health_mcp import api
    from google_health_mcp.tools import sync_tools

    _, db_path = setup_paths
    conn = db.get_db(db_path)
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
    monkeypatch.setattr(sync_tools.db, "get_db", lambda *a, **k: conn)

    def refuse(*_args):
        raise api.HealthAuthError("no token")

    sync_tools.run_sync(["sleep"], days=1, handlers={"sleep": refuse})

    # run_sync closes the connection it was given. Read back with sqlite3
    # directly: db.get_db is monkeypatched above, so it would hand back the
    # same closed connection.
    reopened = sqlite3.connect(db_path)
    statuses = {r[0] for r in reopened.execute("SELECT status FROM sync_log").fetchall()}
    reopened.close()
    assert "auth_error" in statuses

    findings = doctor.check_sync_health()
    assert any(f.severity == doctor.FAIL for f in findings)


class TestDoctorStaysOffline:
    """doctor must not be able to touch the credentials at all.

    It runs on hosts that do not own the token file. An import of auth or
    api is enough: a refresh triggered from a diagnostic rotates a token
    another host is using, and no test watching a healthy setup would see it.
    """

    def _imported_names(self):
        """Every module name doctor.py imports, dotted forms included."""
        import ast
        from pathlib import Path

        tree = ast.parse(Path(doctor.__file__).read_text())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    names.add(f"{node.module}.{alias.name}" if node.module else alias.name)
                if node.module:
                    names.add(node.module)
            elif isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
        return names

    def test_doctor_imports_neither_auth_nor_api(self):
        # Match the last dotted segment so `import google_health_mcp.auth` is caught
        # as well as `from . import auth`.
        segments = {seg for name in self._imported_names() for seg in name.split(".")}
        assert "auth" not in segments
        assert "api" not in segments

    def test_doctor_imports_nothing_else_from_the_package(self):
        """A subset assertion would pass for any new sibling import."""
        siblings = {
            seg
            for name in self._imported_names()
            for seg in name.split(".")
            if seg
            in {"api", "auth", "config", "db", "helpers", "importer", "mcp_instance", "tools"}
        }
        assert siblings == {"config", "db"}


class TestASeriesThatHasStopped:
    """One data type going quiet while the rest continue.

    `_check_freshness` deliberately judges on the newest date across all
    types, because types written only when the user logs something lag by
    design and flagging those individually would cry wolf. That leaves the
    worst silent failure this package has uncovered: a response field renamed
    upstream makes every page parse to nothing while the request still
    succeeds, so one table quietly stops filling, `sync_log` records `ok`, and
    the newest date across the others keeps the cache looking current.

    What separates the two cases is density, not age. A series that produced
    a row on most days and then produced none is a series that stopped; one
    that has always been sporadic has not.
    """

    def _fill(self, conn, table, dates, **columns):
        for day in dates:
            getattr(db, f"save_{table}")(conn, {"date": day, **columns})

    def _days(self, first: int, count: int) -> list[str]:
        start = date(2026, 3, 1) + timedelta(days=first)
        return [(start + timedelta(days=i)).isoformat() for i in range(count)]

    def test_a_dense_series_that_stops_is_reported(self, setup_paths):
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        # Both types dense for a month; only sleep carries on afterwards, so
        # the cache as a whole still looks current.
        self._fill(conn, "hrv", self._days(0, 30), daily_rmssd=30.0)
        self._fill(conn, "sleep", self._days(0, 50), total_minutes=420)
        conn.commit()
        conn.close()

        findings = doctor.run_checks()

        stopped = [f for f in findings if "hrv" in f.detail]
        assert stopped, "a series that stopped filling was not reported"
        assert all(f.severity != doctor.FAIL for f in stopped), (
            "the cause can be behavioural, so this is a warning and not a failure"
        )

    def test_a_sparse_machine_series_is_not_reported(self, setup_paths):
        """Density is the guard, and it has to be tested on a type the
        exclusion list does not already cover - `weight` is user-logged, so
        the test below exercises that list rather than this."""
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        # SpO2 on four scattered nights in a month, then none: not a series
        # that stopped, because it was never dense enough to have been going.
        self._fill(conn, "spo2", self._days(0, 30)[::7], avg=96.0)
        self._fill(conn, "sleep", self._days(0, 50), total_minutes=420)
        conn.commit()
        conn.close()

        assert not [f for f in doctor.run_checks() if "spo2" in f.detail], (
            "a type that was never dense was reported as having stopped"
        )

    def test_a_sporadic_series_is_not_reported(self, setup_paths):
        """The case the freshness check refuses to flag, and this must not undo."""
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        # Weighed four times in a month, then not for a fortnight: ordinary.
        self._fill(conn, "weight", self._days(0, 30)[::7], weight_kg=71.9)
        self._fill(conn, "sleep", self._days(0, 50), total_minutes=420)
        conn.commit()
        conn.close()

        findings = doctor.run_checks()

        assert not [f for f in findings if "weight" in f.detail], (
            "a type that was always sporadic was reported as having stopped"
        )

    def test_a_series_dead_far_longer_than_the_windows_is_still_reported(self, setup_paths):
        """The check must not go blind on an old failure - it is permanent.

        Measuring density over the days before the recent window rather than
        before the type's own last row made a long-dead series read as never
        established, so `doctor` reported a clean bill of health over exactly
        the failure it exists to catch, from about six weeks afterwards on.
        """
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        # Dense for a month, then dead for 200 days while sleep carries on.
        self._fill(conn, "hrv", self._days(0, 30), daily_rmssd=30.0)
        self._fill(conn, "sleep", self._days(0, 230), total_minutes=420)
        conn.commit()
        conn.close()

        assert [f for f in doctor.run_checks() if "hrv" in f.detail], (
            "a series dead longer than the two windows combined was not reported"
        )

    def test_the_remediation_names_a_flag_that_can_reach_the_gap(self, setup_paths):
        """`--days` is read only when there is no cursor, and a stopped series
        always has one - the sync keeps recording that it attempted through
        today. Advising it sends the user to run a command that fetches the
        last few days, see nothing, and read that as the source having stopped."""
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        self._fill(conn, "hrv", self._days(0, 30), daily_rmssd=30.0)
        self._fill(conn, "sleep", self._days(0, 50), total_minutes=420)
        conn.commit()
        conn.close()

        last_row = self._days(0, 30)[-1]
        fixes = [f.fix for f in doctor.run_checks() if "hrv" in f.detail and f.fix]
        assert fixes
        for fix in fixes:
            # The date as well as the flag: `--since 1970-01-01` also carries
            # the right flag, runs, returns nothing, and leaves the user
            # reading the same wrong conclusion.
            assert f"--since {last_row}" in fix, "the advice must resume from the last row"
            assert "--days" not in fix, "--days is inert once a cursor exists"

    def test_a_dense_user_logged_series_that_stops_is_not_reported(self, setup_paths):
        """Density tells a stopped machine series from a sporadic one. For a
        type a person fills by hand it measures a habit, and habits end."""
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        for day in self._days(0, 30):
            db.save_food_log(conn, {"date": day, "calories_in": 2000})
        self._fill(conn, "sleep", self._days(0, 50), total_minutes=420)
        conn.commit()
        conn.close()

        assert not [f for f in doctor.run_checks() if "food_log" in f.detail], (
            "a logging habit that ended was reported as a fault"
        )

    def test_a_last_row_on_the_first_day_of_the_recent_window_is_not_reported(self, setup_paths):
        """The boundary: the recent window includes its own first day."""
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        self._fill(conn, "sleep", self._days(0, 60), total_minutes=420)
        # hrv dense, with its last row exactly on the window's first day.
        last = 59 - (doctor._STOPPED_RECENT_DAYS - 1)
        self._fill(conn, "hrv", self._days(last - 29, 30), daily_rmssd=30.0)
        conn.commit()
        conn.close()

        assert not [f for f in doctor.run_checks() if "hrv" in f.detail], (
            "a row on the first day of the recent window counts as recent"
        )

    def test_exactly_the_established_threshold_is_enough_to_judge(self, setup_paths):
        """The other boundary: `_ESTABLISHED_DAYS` days is established."""
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        self._fill(conn, "sleep", self._days(0, 60), total_minutes=420)
        # Exactly the threshold, on consecutive days ending well before the
        # recent window, so the density window holds precisely that many.
        self._fill(conn, "hrv", self._days(0, doctor._ESTABLISHED_DAYS), daily_rmssd=30.0)
        conn.commit()
        conn.close()

        assert [f for f in doctor.run_checks() if "hrv" in f.detail], (
            "exactly the threshold was treated as not established"
        )

    def test_a_type_with_no_history_is_not_reported(self, setup_paths):
        """Empty because this account has none of it, not because it broke."""
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        self._fill(conn, "sleep", self._days(0, 50), total_minutes=420)
        conn.commit()
        conn.close()

        findings = doctor.run_checks()

        assert not [f for f in findings if "food_log" in f.detail]

    def test_a_dense_series_still_filling_is_not_reported(self, setup_paths):
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        self._fill(conn, "hrv", self._days(0, 50), daily_rmssd=30.0)
        self._fill(conn, "sleep", self._days(0, 50), total_minutes=420)
        conn.commit()
        conn.close()

        findings = doctor.run_checks()

        assert not [f for f in findings if "hrv" in f.detail]

    def test_the_longest_normal_gap_is_not_reported(self, setup_paths):
        """The pattern that decides the window, and it is measured, not chosen.

        The longest gap in the last 400 days of a real cache is eleven days,
        in skin temperature and SpO2. A recent window a normal gap can fill
        would report a fault every time one happened, and a check that cries
        wolf is one nobody reads - so the fixture is that measured gap rather
        than a comfortable one, which is what makes it defend the constant.
        """
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        # Dense for a month, then the measured eleven-day gap, cache carrying on.
        self._fill(conn, "skin_temperature", self._days(0, 39), nightly_relative=0.4)
        self._fill(conn, "sleep", self._days(0, 50), total_minutes=420)
        conn.commit()
        conn.close()

        findings = doctor.run_checks()

        assert not [f for f in findings if "skin_temperature" in f.detail]


class TestTheMachineReadableReport:
    """`doctor --json` exists so a monitor can act on a WARN it cares about.

    The text report grades a stopped series `warn`, and `doctor` exits 1 only
    on `fail`, so the exit code cannot carry that condition. Everything here
    protects the contract an outside consumer depends on: a stable key to match
    on, and an exit code that does not move when a warning appears.
    """

    def _findings(self, *severities):
        return [
            doctor.Finding(f"check {i}", severity, "detail")
            for i, severity in enumerate(severities)
        ]

    def test_the_json_report_parses_and_holds_every_finding(self):
        findings = self._findings(doctor.OK, doctor.WARN, doctor.FAIL)

        payload = json.loads(doctor.format_json(findings))

        assert len(payload["findings"]) == len(findings)
        assert payload["counts"] == {doctor.OK: 1, doctor.WARN: 1, doctor.FAIL: 1}

    def test_every_finding_field_survives_the_round_trip(self):
        finding = doctor.Finding("a name", doctor.WARN, "a detail", "a fix", check="a-check")

        [emitted] = json.loads(doctor.format_json([finding]))["findings"]

        assert emitted == {
            "check": "a-check",
            "name": "a name",
            "severity": doctor.WARN,
            "detail": "a detail",
            "fix": "a fix",
        }

    def test_a_finding_nothing_consumes_still_carries_the_key_as_null(self):
        """A missing key and a null one read the same to a careless consumer,
        but only one of them survives a schema check - emit it always."""
        [emitted] = json.loads(doctor.format_json(self._findings(doctor.OK)))["findings"]

        assert "check" in emitted
        assert emitted["check"] is None

    @pytest.mark.parametrize("as_json", [False, True])
    def test_a_warning_does_not_change_the_exit_code(self, monkeypatch, capsys, as_json):
        monkeypatch.setattr(doctor, "run_checks", lambda: self._findings(doctor.WARN))

        assert doctor.run_doctor(as_json=as_json) == 0
        capsys.readouterr()

    @pytest.mark.parametrize("as_json", [False, True])
    def test_a_failure_does(self, monkeypatch, capsys, as_json):
        monkeypatch.setattr(doctor, "run_checks", lambda: self._findings(doctor.FAIL))

        assert doctor.run_doctor(as_json=as_json) == 1
        capsys.readouterr()

    def test_the_flag_actually_selects_the_format(self, monkeypatch, capsys):
        """Testing `format_json` directly proves the formatter, not the wiring.

        Without this, `run_doctor` could ignore `as_json` entirely and print the
        text report to a consumer expecting JSON - every exit-code assertion
        above still passes, and the failure surfaces as a parse error in
        somebody else's monitor.
        """
        monkeypatch.setattr(doctor, "run_checks", lambda: self._findings(doctor.OK))

        doctor.run_doctor(as_json=True)
        as_json_out = capsys.readouterr().out
        doctor.run_doctor(as_json=False)
        as_text_out = capsys.readouterr().out

        assert json.loads(as_json_out)["counts"][doctor.OK] == 1
        with pytest.raises(json.JSONDecodeError):
            json.loads(as_text_out)

    def test_the_stopped_series_finding_carries_its_stable_check_name(self, setup_paths):
        """The one key an outside monitor matches on.

        `name` is prose and carries the data type, so it cannot be matched on.
        If this slug changes, every consumer silently stops seeing the
        condition - a missing key reads as "no stopped series", not as an error.
        """
        _config_dir, db_path = setup_paths
        db_path.parent.mkdir(parents=True)
        conn = db.get_db(db_path)
        start = date(2026, 3, 1)
        days = [(start + timedelta(days=i)).isoformat() for i in range(30)]
        later = [(start + timedelta(days=i)).isoformat() for i in range(50)]
        for day in days:
            db.save_hrv(conn, {"date": day, "daily_rmssd": 30.0})
        for day in later:
            db.save_sleep(conn, {"date": day, "total_minutes": 420})
        conn.commit()
        conn.close()

        findings = doctor.run_checks()

        stopped = [f for f in findings if f.check == doctor.STOPPED_SERIES]
        assert stopped, "the stopped-series finding lost its machine-readable check name"
        assert all(f.severity == doctor.WARN for f in stopped)

    def test_the_stopped_series_slug_is_the_literal_consumers_match_on(self):
        """Its own test, so an earlier failure in the class cannot hide a
        rename - this is the only place the literal itself is pinned."""
        assert doctor.STOPPED_SERIES == "stopped-series"

    def test_the_payload_names_the_build_that_produced_it(self):
        """Without it a consumer cannot tell "no stopped series" from "this
        build has no such check": an older release omits the key entirely, and
        one older still rejects `--json` and exits 2.

        A round trip against the same source, so it pins that the key exists and
        tracks this package's metadata - not that the metadata is right.
        """
        payload = json.loads(doctor.format_json([]))

        assert payload["version"] == version("google-health-mcp")

    def test_a_tree_with_no_installed_metadata_still_produces_a_payload(self, monkeypatch):
        """`--json` must not be the one mode that dies on a half-broken install.

        Run from a source checkout with no distribution metadata the version
        lookup raises, and this path sits outside the catch-all that keeps the
        text report alive - so an unguarded lookup would traceback and emit
        nothing, exactly when a diagnostic is most wanted. The key stays present
        and null rather than disappearing, for the same reason `check` does.
        """

        def no_metadata(_name):
            raise PackageNotFoundError(_name)

        monkeypatch.setattr(doctor, "version", no_metadata)

        payload = json.loads(doctor.format_json(self._findings(doctor.OK)))

        assert payload["version"] is None
        assert payload["counts"][doctor.OK] == 1
