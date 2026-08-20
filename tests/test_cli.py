"""Tests for the google-health-mcp command-line entry point."""

from importlib.metadata import version
from unittest.mock import patch

import pytest

from google_health_mcp import cli


def test_version_flag_prints_package_version(capsys):
    with patch("sys.argv", ["google-health-mcp", "--version"]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"google-health-mcp {version('google-health-mcp')}"


def test_version_flag_takes_precedence_over_subcommand(capsys):
    with patch("sys.argv", ["google-health-mcp", "auth", "--version"]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"google-health-mcp {version('google-health-mcp')}"


def test_version_flag_does_not_mask_invalid_subcommand_args(capsys):
    with patch("sys.argv", ["google-health-mcp", "import", "--data-dir", "--version"]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    captured = capsys.readouterr()
    assert exc_info.value.code != 0
    assert f"google-health-mcp {version('google-health-mcp')}" not in captured.out


def test_auth_without_a_client_file_says_so_rather_than_crashing(capsys, tmp_path, monkeypatch):
    """The first command the README gives, run before the file is in place.

    It used to end in a five-frame traceback whose message told the reader to
    run the command they had just run - correct wording for a missing token,
    circular for the client file that `auth` reads first.
    """
    monkeypatch.setattr(
        "google_health_mcp.config.GOOGLE_CLIENT_PATH", tmp_path / "google_client.json"
    )
    with patch("sys.argv", ["google-health-mcp", "auth"]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 1
    message = capsys.readouterr().err
    assert "Desktop app" in message
    assert "Traceback" not in message


def test_sync_passes_since_and_until_to_run_sync(monkeypatch):
    captured = {}

    def fake_run_sync(types, days, since=None, until=None, handlers=None):
        captured.update(types=types, days=days, since=since, until=until)
        return {"sleep": {"status": "ok", "records": 0, "range": ""}}

    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", False)
    monkeypatch.setattr(cli.sync_tools, "run_sync", fake_run_sync)
    argv = ["google-health-mcp", "sync", "--types", "sleep"]
    argv += ["--since", "2026-03-05", "--until", "2026-03-09"]
    with patch("sys.argv", argv):
        cli.main()

    assert captured == {
        "types": ["sleep"],
        "days": 30,
        "since": "2026-03-05",
        "until": "2026-03-09",
    }


def test_sync_refuses_in_offline_mode(capsys, monkeypatch):
    monkeypatch.setattr("google_health_mcp.config.OFFLINE_MODE", True)
    with patch("sys.argv", ["google-health-mcp", "sync"]):
        with pytest.raises(SystemExit) as exc_info:
            cli.main()

    assert exc_info.value.code == 1
    assert "GOOGLE_HEALTH_MCP_OFFLINE" in capsys.readouterr().err
