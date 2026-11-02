"""Doctor says "valid until" only for a token the next call would send."""

import json
import time

import pytest

from google_health_mcp import auth, config, doctor

_NOW = 1_800_000_000


@pytest.fixture
def access_finding(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: _NOW)
    monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", tmp_path / "google_tokens.json")

    def finding(expires_at):
        config.GOOGLE_TOKENS_PATH.write_text(
            json.dumps({"access_token": "a", "refresh_token": "r", "expires_at": expires_at})
        )
        return next(f for f in doctor._check_token_file() if f.name == "access token")

    return finding


@pytest.mark.parametrize("offset", [-60, 0, 100, 299, 300, 301, 3600])
def test_doctor_agrees_with_what_the_next_call_does(access_finding, offset):
    expires_at = _NOW + offset
    sent = auth._usable({"access_token": "a", "expires_at": expires_at})
    assert sent == (offset > 300)
    assert ("valid until" in access_finding(expires_at).detail) == sent


@pytest.mark.parametrize(
    "offset, word",
    [(-60, "expired at"), (100, "expires at")],
    ids=["past", "inside-the-buffer"],
)
def test_a_token_the_next_call_replaces_is_said_to_be_refreshed(access_finding, offset, word):
    finding = access_finding(_NOW + offset)
    assert finding.severity == doctor.OK
    assert word in finding.detail
    assert "the next call refreshes it" in finding.detail
    assert finding.fix is None
