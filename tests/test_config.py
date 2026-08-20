"""Tests for configuration module."""

import ast
import os
from pathlib import Path
from unittest.mock import patch

from google_health_mcp import api
from google_health_mcp.tools import google_sync
from tests.conftest import FETCHERS

#: The data types fetched under each scope this package requests. Google
#: publishes a category per scope ("your Google Health sleep data") and no
#: per-type mapping, so the attribution of a type to a category is ours; what
#: the test below checks is that every scope has one, not which one.
_SCOPE_READERS = {
    "activity_and_fitness": (
        "steps",
        "distance",
        "floors",
        "total-calories",
        "active-zone-minutes",
        "exercise",
    ),
    "health_metrics_and_measurements": (
        "daily-resting-heart-rate",
        "daily-heart-rate-variability",
        "daily-oxygen-saturation",
        "daily-respiratory-rate",
        "daily-sleep-temperature-derivations",
        "daily-vo2-max",
        "weight",
        "body-fat",
        "core-body-temperature",
    ),
    "sleep": ("sleep",),
    "nutrition": ("nutrition-log", "hydration-log"),
    "ecg": ("electrocardiogram",),
    "irn": ("irregular-rhythm-notification",),
    # Paired devices is the one endpoint that is neither a list nor a rollup,
    # so it has no entry in GOOGLE_TYPES to name.
    "settings": ("list_paired_devices",),
}
_NOT_A_DATA_TYPE = {"list_paired_devices"}


def _types_the_package_fetches() -> set[str]:
    """Every data type a fetch in the tools package is given, plus the device call.

    Read off the calls rather than off the source's string constants, because
    `GOOGLE_TYPES` catalogues a dozen types nothing fetches - so a scope could
    otherwise be justified by cataloguing a type for it, and `api.py`, which
    holds that catalogue, is not scanned.
    """
    fetched = set()
    for source in sorted(Path(google_sync.__file__).parent.glob("*.py")):
        for node in ast.walk(ast.parse(source.read_text())):
            if not isinstance(node, ast.Call):
                continue
            # Both spellings, or importing a function instead of reaching it
            # through its module fails a test about scopes.
            called = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if called in _NOT_A_DATA_TYPE:
                fetched.add(called)
            first = node.args[0] if node.args else None
            if called in FETCHERS and isinstance(first, ast.Constant):
                fetched.add(first.value)
    return fetched


class TestConfigDefaults:
    """Test default configuration values."""

    def test_default_paths_exist(self):
        from google_health_mcp.config import CONFIG_DIR, DB_PATH

        # Defaults use XDG-compatible locations under the user home directory
        assert isinstance(CONFIG_DIR, Path)
        assert isinstance(DB_PATH, Path)
        assert CONFIG_DIR.name == "google-health-mcp"
        assert str(CONFIG_DIR).startswith(str(Path.home()))
        assert DB_PATH.name == "google_health.db"
        assert str(DB_PATH).startswith(str(Path.home()))

    def test_api_constants(self):
        from google_health_mcp.config import (
            GOOGLE_API_BASE,
            GOOGLE_AUTH_URL,
            GOOGLE_CALLBACK_PORT,
            GOOGLE_REDIRECT_URI,
            GOOGLE_TOKEN_URL,
        )

        assert GOOGLE_API_BASE == "https://health.googleapis.com/v4"
        assert "oauth2" in GOOGLE_AUTH_URL
        assert "token" in GOOGLE_TOKEN_URL
        assert f"localhost:{GOOGLE_CALLBACK_PORT}" in GOOGLE_REDIRECT_URI

    def test_every_scope_is_readonly(self):
        """A write scope here would be asked for on every consent screen.

        The package only ever reads, so a scope granting more than that is a
        permission the user cannot tell they gave.
        """
        from google_health_mcp.config import GOOGLE_SCOPES

        scopes = GOOGLE_SCOPES.split()
        assert scopes
        for scope in scopes:
            assert scope.endswith(".readonly"), f"not a read-only scope: {scope}"

    def test_no_scope_is_asked_for_that_nothing_reads(self):
        """A scope with no reader is a permission the user grants for nothing.

        `profile` was requested and read nowhere for as long as nobody looked,
        and nothing behavioural can see it: a granted scope changes only the
        consent screen. Stating the list here in prose did not hold either -
        adding a scope and a line of prose beside it passed. So each scope
        names the data types fetched under it, and those are checked against
        the calls the sync actually makes.

        The other direction matters as much and is checked by the same
        equality: a scope dropped while something still fetches its types
        fails at the request as a 403, which reads like a publishing problem
        rather than a missing permission. Asking only that each scope has
        *some* reader let a scope and its entry here be deleted together.
        """
        from google_health_mcp.config import GOOGLE_SCOPES

        granted = {scope.rsplit(".", 2)[-2] for scope in GOOGLE_SCOPES.split()}
        assert granted == set(_SCOPE_READERS), "a granted scope with no reader named"

        claimed = [reader for readers in _SCOPE_READERS.values() for reader in readers]
        # Or a scope invented for no reason is justified by naming types
        # another scope already covers, which costs one line and no reader.
        assert len(claimed) == len(set(claimed)), "a type is claimed by two scopes"

        unknown = [r for r in claimed if r not in api.GOOGLE_TYPES and r not in _NOT_A_DATA_TYPE]
        assert not unknown, f"named types that do not exist: {unknown}"

        assert _types_the_package_fetches() == set(claimed), (
            "the types this package fetches and the types its scopes authorise have parted"
        )


class TestConfigOverrides:
    """Test environment variable overrides."""

    def test_config_dir_override(self, tmp_path):
        with patch.dict(os.environ, {"GOOGLE_HEALTH_MCP_CONFIG_DIR": str(tmp_path)}):
            # Re-import to pick up env var
            import importlib

            import google_health_mcp.config

            importlib.reload(google_health_mcp.config)
            assert google_health_mcp.config.CONFIG_DIR == tmp_path
            # Restore
            importlib.reload(google_health_mcp.config)

    def test_db_path_override(self, tmp_path):
        db_path = tmp_path / "custom.db"
        with patch.dict(os.environ, {"GOOGLE_HEALTH_MCP_DB_PATH": str(db_path)}):
            import importlib

            import google_health_mcp.config

            importlib.reload(google_health_mcp.config)
            assert google_health_mcp.config.DB_PATH == db_path
            importlib.reload(google_health_mcp.config)


class TestOfflineMode:
    """Test GOOGLE_HEALTH_MCP_OFFLINE parsing."""

    def test_offline_default_false(self):
        import importlib

        import google_health_mcp.config

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_HEALTH_MCP_OFFLINE", None)
            importlib.reload(google_health_mcp.config)
            assert google_health_mcp.config.OFFLINE_MODE is False
        importlib.reload(google_health_mcp.config)

    def test_offline_truthy_values(self):
        import importlib

        import google_health_mcp.config

        for val in ("1", "true", "True", "YES", "on"):
            with patch.dict(os.environ, {"GOOGLE_HEALTH_MCP_OFFLINE": val}):
                importlib.reload(google_health_mcp.config)
                assert google_health_mcp.config.OFFLINE_MODE is True, val
        importlib.reload(google_health_mcp.config)

    def test_offline_falsy_values(self):
        import importlib

        import google_health_mcp.config

        for val in ("0", "false", "no", "off", "", "  "):
            with patch.dict(os.environ, {"GOOGLE_HEALTH_MCP_OFFLINE": val}):
                importlib.reload(google_health_mcp.config)
                assert google_health_mcp.config.OFFLINE_MODE is False, val
        importlib.reload(google_health_mcp.config)
