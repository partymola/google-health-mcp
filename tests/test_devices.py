"""Paired devices - the one tool with no cache behind it.

Every other query tool reads the database, so `live` there means "sync this
window first". Devices has no table and no history worth one: the answer is a
battery level and a last-sync time, both of which are only ever interesting
now. So it still fetches on every call, and these tests cover that fetch.
"""

from unittest.mock import patch

import pytest

from google_health_mcp import api
from google_health_mcp.tools.devices_tools import _fetch_devices


def _device(name="users/me/pairedDevices/abc123", **overrides):
    """One paired device, in the shapes the API documents.

    `deviceType` is an enum of TRACKER and SCALE, and `batteryStatus` is one of
    High / Medium / Low / Empty - not the screaming-snake form the rest of this
    API uses for enums. A fixture that invents either reads as evidence about
    the API to whoever writes the next test against it.
    """
    device = {
        "name": name,
        "deviceType": "TRACKER",
        "deviceVersion": "Fictional Watch 3",
        "batteryStatus": "High",
        "batteryLevel": 88,
        "lastSyncTime": "2026-05-03T07:00:00Z",
        "macAddress": "AA:BB:CC:DD:EE:FF",
        "features": [],
    }
    device.update(overrides)
    return device


class TestTheDeviceFetch:
    @patch("google_health_mcp.tools.devices_tools.api.list_paired_devices")
    def test_returns_devices(self, mock_list):
        mock_list.return_value = [_device()]
        result = _fetch_devices()
        assert len(result) == 1
        assert result[0]["device_version"] == "Fictional Watch 3"
        assert result[0]["battery"] == "High"
        assert result[0]["battery_level"] == 88
        assert result[0]["mac"] == "AA:BB:CC:DD:EE:FF"

    @patch("google_health_mcp.tools.devices_tools.api.list_paired_devices")
    def test_an_empty_account_yields_nothing(self, mock_list):
        mock_list.return_value = []
        assert _fetch_devices() == []

    @patch("google_health_mcp.tools.devices_tools.api.list_paired_devices")
    def test_an_entry_of_the_wrong_shape_is_skipped(self, mock_list):
        """One malformed entry must not cost the reading of the others."""
        mock_list.return_value = ["not a device", _device()]
        assert len(_fetch_devices()) == 1


class TestTheDeviceListWalk:
    """The same paging invariant every other read here carries.

    A first page that answers with a `nextPageToken` is not the whole list,
    however few devices it holds, and an account's second watch is exactly the
    kind of absence nobody would notice.
    """

    def test_a_single_page_is_returned(self, monkeypatch):
        monkeypatch.setattr(api, "google_get", lambda *a, **k: {"pairedDevices": [_device()]})
        assert len(api.list_paired_devices()) == 1

    def test_it_follows_the_page_token(self, monkeypatch):
        pages = [
            {"pairedDevices": [_device(name="one")], "nextPageToken": "more"},
            {"pairedDevices": [_device(name="two")]},
        ]
        seen = []

        def fake_get(path, params, body=None):
            seen.append(params.get("pageToken"))
            return pages[len(seen) - 1]

        monkeypatch.setattr(api, "google_get", fake_get)
        devices = api.list_paired_devices()
        assert [d["name"] for d in devices] == ["one", "two"]
        assert seen == [None, "more"]

    def test_a_response_carrying_no_devices_is_not_an_error(self, monkeypatch):
        monkeypatch.setattr(api, "google_get", lambda *a, **k: {})
        assert api.list_paired_devices() == []

    def test_a_devices_field_of_the_wrong_shape_is_refused(self, monkeypatch):
        monkeypatch.setattr(api, "google_get", lambda *a, **k: {"pairedDevices": {"a": 1}})
        with pytest.raises(api.HealthAPIError, match="shape"):
            api.list_paired_devices()

    def test_the_walk_is_bounded(self, monkeypatch):
        """A server that always returns a token would otherwise never stop."""
        monkeypatch.setattr(
            api,
            "google_get",
            lambda *a, **k: {"pairedDevices": [_device()], "nextPageToken": "always"},
        )
        with pytest.raises(api.HealthAPIError, match="pages"):
            api.list_paired_devices()
