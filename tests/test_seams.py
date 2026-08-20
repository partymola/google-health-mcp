"""Claims no input to the program can falsify.

A behavioural test asks "given this input, what happens". The assertions
here are about the code's structure and its packaging, so nothing you can
feed the server exercises them - which is why each of these survived until
someone went looking. See AGENTS.md, "Seams the suite does not cross".
"""

import ast
import json
import re
import sqlite3
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import google_health_mcp
from google_health_mcp import api, db, doctor

_PATH_NAMES = {
    "CONFIG_DIR",
    "DB_PATH",
    "GOOGLE_CLIENT_PATH",
    "GOOGLE_TOKENS_PATH",
    "data_dir",
    "path",
}
_CLI_ONLY = {"cli.py", "doctor.py", "importer.py"}
_LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception", "log"}


def _shared_sources():
    root = Path(google_health_mcp.__file__).parent
    return [p for p in sorted(root.rglob("*.py")) if p.name not in _CLI_ONLY]


def test_no_logger_call_in_shared_code_carries_a_path():
    """A CLI subcommand may print a path; shared code may not.

    Server mode writes log lines to stderr, where the MCP client collects
    them, so a log line is closer to a tool response than to a terminal.

    _PATH_NAMES is a blocklist, which is weaker than the whitelist guarding
    setup_auth. It is defensible only because the path identifiers in
    config.py are a small closed set - a new one must be added here.
    """
    offenders = []
    for source in _shared_sources():
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            # Keyed on the logging method, not on how the logger is spelled:
            # `logger.info` and `logging.getLogger(__name__).info` are the
            # same call, and matching the receiver's name misses the second.
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS
            ):
                continue
            for arg in node.args + [kw.value for kw in node.keywords]:
                if any(name in ast.unparse(arg) for name in _PATH_NAMES):
                    offenders.append(f"{source.name}:{node.lineno} {ast.unparse(node)}")
    assert not offenders, f"log line carries a path: {offenders}"


def test_every_test_agents_md_names_exists():
    """AGENTS.md pins its invariants by naming the test that holds each one.

    A renamed or deleted test leaves that sentence pointing at nothing, and
    the sentence is what a maintainer reads before deciding an invariant is
    safe to change - so the citation going stale is worse than its absence.
    Nothing behavioural can see it: the doc is not imported by anything.
    """
    doc = (Path(google_health_mcp.__file__).parents[2] / "AGENTS.md").read_text()
    cited = set(re.findall(r"`(test_[A-Za-z0-9_]+|Test[A-Za-z0-9_]+)`", doc))
    # A module is cited as a path elsewhere in the sentence; only names of
    # tests are checked here.
    cited = {name for name in cited if not name.endswith(".py")}

    defined = set()
    for source in sorted((Path(__file__).parent).rglob("test_*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)

    assert cited, "no test names found in AGENTS.md - the pattern has stopped matching"
    missing = sorted(cited - defined)
    assert not missing, f"AGENTS.md names tests that do not exist: {missing}"


def test_the_readme_scope_table_is_the_scopes_that_are_requested():
    """A scope table nobody generates drifts from the constant it describes.

    It told readers to tick `profile.readonly` for a fortnight after that
    scope was dropped from GOOGLE_SCOPES, and a reader who ticks fewer than
    the code asks for gets a 403 at the first request rather than at consent.
    The table and the paragraph naming what this package declines are read
    separately, because they make opposite claims about the same constant.
    """
    from google_health_mcp.config import GOOGLE_SCOPES

    readme = (Path(google_health_mcp.__file__).parents[2] / "README.md").read_text()
    section = readme.split("## OAuth scopes", 1)[-1].split("\n## ", 1)[0]
    listed = set(re.findall(r"^\| `([a-z_]+\.readonly)` \|", section, re.M))
    assert listed, "the OAuth scopes table has stopped matching - check the heading"

    requested = {scope.split("googlehealth.", 1)[-1] for scope in GOOGLE_SCOPES.split()}
    assert listed == requested, (
        f"README lists {sorted(listed - requested)} that are not requested, and omits "
        f"{sorted(requested - listed)}"
    )

    # One paragraph in this section makes the opposite claim, and it must stay
    # disjoint from the table: naming a scope as deliberately not requested
    # while requesting it is the same defect wearing the other sign. Found by
    # what it says rather than by where it sits - anchored on position, it
    # read the paragraph below instead when a sentence was inserted, and
    # accused the table of listing `nutrition.readonly` wrongly.
    declines = [p for p in section.split("\n\n") if "does not request" in p]
    assert len(declines) == 1, "the paragraph naming the declined scopes has stopped matching"
    declined = set(re.findall(r"`([a-z_]+\.readonly)`", declines[0]))
    assert declined, "that paragraph now names no scope"
    assert not declined & requested, (
        f"named as not requested, but requested: {sorted(declined & requested)}"
    )


def test_every_data_type_a_fetch_names_is_one_the_api_has():
    """A typo'd path is a live 404 and nothing in the suite can see it.

    Every normaliser test mocks the client, so the path travels no further
    than the mock: replacing `"sleep"` with a type that does not exist leaves
    every behavioural test green while the real sync 404s and that table stops
    filling. The scope test catches it too, but only where `_SCOPE_READERS` is
    not edited to match; this one fires either way.

    Scans the whole tools package rather than `google_sync.py` alone, so that
    it and the scope test read the same calls - a fetch added elsewhere would
    otherwise be checked for scope coverage and not for a path that exists.
    """
    from google_health_mcp.tools import google_sync
    from tests.conftest import FETCHERS

    named = set()
    for source in sorted(Path(google_sync.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if called in FETCHERS and isinstance(node.args[0], ast.Constant):
                named.add(node.args[0].value)
    assert named, "no fetch found - the helper names in FETCHERS have moved"
    unknown = sorted(named - set(api.GOOGLE_TYPES))
    assert not unknown, f"fetched under a data type the API does not have: {unknown}"


def _registered_name(node) -> str:
    """The name a tool reaches the wire under, which need not be its own."""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                return keyword.value.value
    return node.name


def test_every_tool_written_here_reaches_the_wire():
    """A tool a module defines but nothing imports is absent with no error.

    Registration happens at import time, so a module `cli.py` never names
    simply does not run, and the suite - which imports each module directly -
    passes anyway.

    Two things make this hold. It asks the server's own registry rather than
    reading `cli.py`'s import list, so a change to how modules are discovered
    passes instead of pointing a maintainer at the wrong repair. And it asks
    in a fresh interpreter, because this one has already imported every tool
    module by hand: in-process, the registry holds tools nothing asked the
    server for, which is the exact condition being checked.
    """
    probe = (
        "import asyncio, json, google_health_mcp.cli;"
        "from google_health_mcp.mcp_instance import mcp;"
        "print(json.dumps([t.name for t in asyncio.run(mcp.list_tools())]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"the server would not start: {result.stderr[-500:]}"
    registered = set(json.loads(result.stdout))
    assert registered, "the server registry is empty - it has stopped being populated this way"

    defined = set()
    for source in sorted((Path(google_health_mcp.__file__).parent / "tools").glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            # Keyed on the name a tool is written under rather than on the
            # decorator: a defect that registers some other way carries no
            # decorator to match, and a check for one is satisfied by every
            # version of the bug that spells registration differently.
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("health_"):
                continue
            defined.add(_registered_name(node))
    assert defined, "no tool found - the naming convention has changed"

    missing = sorted(defined - registered)
    assert not missing, f"defined but never registered: {missing}"


def test_the_declared_version_and_the_lockfile_agree():
    """Nothing behavioural can see a lockfile left behind by a bump.

    A release does both by hand, and a commit exists in this history
    because one of them was missed.
    """
    root = Path(google_health_mcp.__file__).parents[2]
    declared = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    locked = tomllib.loads((root / "uv.lock").read_text())
    ours = [p for p in locked["package"] if p["name"] == "google-health-mcp"]
    assert ours, "google-health-mcp is not in uv.lock"
    assert ours[0]["version"] == declared, (
        f"pyproject.toml says {declared}, uv.lock says {ours[0]['version']} - run `uv lock`"
    )


def _columns(conn) -> dict[str, set[tuple[str, str]]]:
    """Name and declared type, because a migration can get the type wrong silently.

    An ALTER declaring TEXT where SCHEMA says REAL leaves an upgraded database
    storing a number as text while a fresh install stores it as a number: the
    two sort differently and doctor calls both healthy.
    """
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: {(r[1], r[2]) for r in conn.execute(f"PRAGMA table_info('{t}')")} for t in tables}


def _baselines() -> list[Path]:
    """One file per schema a release shipped, oldest first.

    Sorted by version rather than by filename, or 1.10.0 sorts before 1.9.0
    and the newest baseline is the wrong one. What each file is for, and when
    one is added: tests/schema_baselines/README.md.
    """
    files = sorted((Path(__file__).parent / "schema_baselines").glob("*.sql"))
    # Checked rather than assumed: an int() straight off the stem raises on
    # `1.1.0rc1.sql`, which errors every test in the class at once and makes
    # `test_a_released_schema_is_always_on_file` report that no baseline
    # exists - pointing a maintainer at the wrong repair.
    for path in files:
        assert re.fullmatch(r"\d+\.\d+\.\d+", path.stem), f"{path.name} is not named X.Y.Z.sql"
    return sorted(files, key=lambda p: tuple(int(part) for part in p.stem.split(".")))


def _built_from(path: Path, baseline: Path) -> dict[str, set[tuple[str, str]]]:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(baseline.read_text())
        conn.commit()
        return _columns(conn)
    finally:
        conn.close()


def _current_schema_columns() -> dict[str, set[tuple[str, str]]]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(db.SCHEMA)
        return _columns(conn)
    finally:
        conn.close()


class TestTheMigrationLockstep:
    """The lists a schema change has to move together.

    Every test builds its own database, so nothing a user could sync changes
    the answer. Break the lockstep and doctor tells a user with years of
    history to "re-create and re-import" a database that only needed an ALTER.
    """

    def test_a_released_schema_is_always_on_file(self):
        """With no baseline every other test in this class passes vacuously.

        That is not a theoretical state: it is what a package looks like
        before its first release, and a column added to SCHEMA then reaches
        the whole suite unchallenged while doctor tells the user who upgrades
        into it to re-create a database an ALTER would have fixed. So a
        baseline exists from 1.0.0 onwards, and each release that changes the
        schema adds its own.
        """
        assert _baselines(), (
            "no schema baseline on file, so nothing here checks anything - copy "
            "db.SCHEMA into tests/schema_baselines/<version>.sql"
        )

    def test_every_supported_database_gains_this_versions_schema(self, tmp_path):
        """A column added to SCHEMA reaches an existing database only via _migrate.

        CREATE TABLE IF NOT EXISTS creates whole tables and nothing else, so a
        new column with no ALTER beside it exists on fresh installs only.
        """
        expected = _current_schema_columns()
        for baseline in _baselines():
            path = tmp_path / f"{baseline.stem}.db"
            _built_from(path, baseline)
            conn = db.get_db(path)
            try:
                assert _columns(conn) == expected, baseline.stem
            finally:
                conn.close()

    def test_no_baseline_has_been_changed_since_it_was_added(self):
        """The escape hatch from the test above, and the only pin on it is history.

        A column added to SCHEMA with no migration fails the lockstep, and the
        cheapest-looking way out is editing the baseline to match - after
        which everything is green and the user who upgrades is the one who
        finds out. With a single baseline on file there is no older vintage
        left failing to give it away.

        Asked as "how many commits have touched this file", not as a diff
        against HEAD: the maintainer commits the edit, and from then on the
        worktree and HEAD agree forever - so a diff could never fire in CI,
        which tests a committed tree. It also has to say nothing about a
        baseline being *added* or *deleted*, which are the release step and
        the documented way of dropping support for an old database.
        """

        def git(*args):
            try:
                done = subprocess.run(
                    ["git", *args],
                    cwd=Path(google_health_mcp.__file__).parents[2],
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                pytest.skip("git is not installed")
            if done.returncode != 0:
                pytest.skip("not a git checkout")
            return done.stdout.split()

        # A shallow clone holds one commit per file whatever its real history,
        # so every baseline would look untouched and the check would pass
        # while seeing nothing.
        if git("rev-parse", "--is-shallow-repository") == ["true"]:
            pytest.skip("a shallow clone cannot show when a file was added")
        if not [
            name for name in git("ls-files", "tests/schema_baselines") if name.endswith(".sql")
        ]:
            pytest.skip("no baseline is committed yet, so history has nothing to say about one")

        for baseline in _baselines():
            path = f"tests/schema_baselines/{baseline.name}"
            commits = git("log", "--format=%H", "--", path)
            if not commits:
                continue  # added but not committed: the release step, in progress
            assert len(commits) == 1, (
                f"{baseline.name} has been changed since it was added, over "
                f"{len(commits)} commits - a released schema is written once"
            )
            assert not git("diff", "--name-only", "--diff-filter=M", "HEAD", "--", path), (
                f"{baseline.name} is modified in the working tree"
            )

    def test_no_older_baseline_has_been_refreshed_to_the_current_schema(self, tmp_path):
        """An older baseline equal to SCHEMA compares the schema with itself.

        The newest one is exempt because it is the released schema and equals
        SCHEMA whenever nothing has changed since, which is most of the time.
        That exemption is a hole, and the test above is what covers it.

        Deleting a baseline is not guarded: it is how support for databases
        that old is dropped, and an assertion that fired on it would be
        deleted instead.
        """
        expected = _current_schema_columns()
        for baseline in _baselines()[:-1]:
            path = tmp_path / f"{baseline.stem}-asis.db"
            assert _built_from(path, baseline) != expected, (
                f"{baseline.name} matches the current schema, so it checks nothing"
            )

    def test_self_healing_columns_is_exactly_what_migrate_adds(self, tmp_path):
        """doctor excuses a missing column only where _migrate will restore it.

        Too few entries and an ordinary upgrade reports FAIL; too many and a
        column that really is unrepairable is waved through as self-healing.
        """
        added = set()
        for baseline in _baselines():
            path = tmp_path / f"{baseline.stem}.db"
            before = _built_from(path, baseline)
            conn = db.get_db(path)
            try:
                after = _columns(conn)
            finally:
                conn.close()
            # Only tables the baseline had: one it lacked arrives whole, and
            # its columns are not something _migrate added.
            added |= {
                (table, column)
                for table, columns in before.items()
                for column, _declared in after[table] - columns
            }
        assert added == doctor._SELF_HEALING_COLUMNS

    def test_every_table_is_accounted_for_by_the_lists_that_enumerate_them(self):
        """Two lists restate SCHEMA's tables, and a table missing from either fails quietly.

        The exclusions are named rather than inferred, so each stays a
        decision: core_temperature is written with INSERT OR IGNORE, and
        sync_log holds no dated measurement.
        """
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(db.SCHEMA)
            tables = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            assert set(db._UPSERT_KEYS) == tables - {"core_temperature", "sync_log"}
            assert set(db._DATA_TABLE_MAP) == tables - {"sync_log"}
            assert all(t == name for name, t in db._DATA_TABLE_MAP.items())

            for table, keys in db._UPSERT_KEYS.items():
                primary = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')") if r[5]}
                assert set(keys) == primary, f"{table} is not keyed by its primary key"
        finally:
            conn.close()

    def test_every_cached_type_is_reachable_from_the_provider(self):
        """A cached type with no handler is a table nothing can ever write.

        One provider means the two lists must match exactly, in both
        directions: a type the cache holds and the sync cannot fill is a
        series that stops with nothing saying so, and a handler for a type
        the package does not cache is what a rename leaves behind.
        """
        from google_health_mcp.config import CACHED_DATA_TYPES
        from google_health_mcp.tools.google_sync import GOOGLE_SYNC_HANDLERS

        covered = set(GOOGLE_SYNC_HANDLERS)
        unreachable = sorted(set(CACHED_DATA_TYPES) - covered)
        assert not unreachable, f"nothing can sync {unreachable}"
        stray = sorted(covered - set(CACHED_DATA_TYPES))
        assert not stray, f"handler for a type the package does not cache: {stray}"

    def test_no_save_helper_reaches_the_database_except_through_the_upsert(
        self, tmp_db, monkeypatch
    ):
        """A helper running its own statement keeps the old behaviour unnoticed.

        Only five of the twelve upserted tables have a preservation test, so
        reverting one of the other seven passes the whole suite while its
        _UPSERT_KEYS entry stays and means nothing.

        With _upsert stubbed out, a row that still lands in the table was
        written some other way. Asking what reached the database rather than
        how the call was spelled is the point: REPLACE INTO is a synonym for
        INSERT OR REPLACE INTO, executemany is the natural idiom for a bulk
        importer, and a statement can always be run one function further down.
        """
        # The table a helper writes is read from the _upsert call it makes,
        # not guessed from its name: save_exercise writes `exercises` and
        # save_heart_rate_row writes `heart_rate`, and a name-shaped rule
        # needs a new exception for each of those.
        helpers = {}
        for node in ast.parse(Path(db.__file__).read_text()).body:
            if not (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("save_")
            ):
                continue
            for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
                if getattr(call.func, "id", None) == "_upsert" and len(call.args) > 1:
                    literal = call.args[1]
                    if isinstance(literal, ast.Constant):
                        helpers[node.name] = literal.value
            helpers.setdefault(node.name, None)
        assert len(helpers) > 1
        # Both directions, or a table whose writer is named anything else is
        # never driven and the check quietly stops covering it.
        unwritten = set(db._UPSERT_KEYS) - set(helpers.values())
        assert not unwritten, f"no save_ helper for {sorted(unwritten)}"

        seen: list[str] = []
        monkeypatch.setattr(db, "_upsert", lambda conn, table, row: seen.append(table))

        for name, table in helpers.items():
            if name == "save_core_temperature":
                continue
            assert table in db._UPSERT_KEYS, f"{name} upserts into no known table"
            row = {
                c: "2026-03-10" if c == "date" else None
                for c in (r[1] for r in tmp_db.execute(f"PRAGMA table_info('{table}')"))
            }
            seen.clear()
            if name == "save_heart_rate":
                db.save_heart_rate(tmp_db, row["date"], None, [])
            elif name == "save_exercise":
                db.save_exercise(tmp_db, "log1", {k: v for k, v in row.items() if k != "log_id"})
            else:
                getattr(db, name)(tmp_db, row)

            assert seen == [table], f"{name} did not upsert into {table}"
            stored = tmp_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            assert stored == 0, f"{name} wrote {stored} row(s) with _upsert stubbed out"

        # Keyed by (datetime, temp_celsius), so a changed reading is a new row
        # rather than a correction - the one helper that must not upsert.
        seen.clear()
        db.save_core_temperature(
            tmp_db, {"datetime": "2026-03-10T07:00:00", "date": "2026-03-10", "temp_celsius": 36.6}
        )
        assert tmp_db.execute("SELECT COUNT(*) FROM core_temperature").fetchone()[0] == 1
        assert seen == [], "save_core_temperature must not upsert"
