"""Paired device query tool (live only - no caching)."""

import anyio

from .. import api
from ..helpers import format_response, require_auth
from ..mcp_instance import mcp


def _fetch_devices() -> list[dict]:
    """The paired devices, under this package's own names for their fields."""
    devices = []
    for entry in api.list_paired_devices():
        if not isinstance(entry, dict):
            continue
        devices.append(
            {
                "id": entry.get("name"),
                "type": entry.get("deviceType"),
                "device_version": entry.get("deviceVersion"),
                "battery": entry.get("batteryStatus"),
                "battery_level": entry.get("batteryLevel"),
                "last_sync_time": entry.get("lastSyncTime"),
                "mac": entry.get("macAddress"),
                "features": entry.get("features", []),
            }
        )
    return devices


@mcp.tool()
@require_auth
async def health_get_devices() -> str:
    """List paired devices with battery level and last sync time.

    Live-only (no caching) - reflects current device state. Useful for
    monitoring tracker health, knowing which device produced data, and
    spotting sync gaps.

    Returns one entry per paired device with id, type, device_version,
    battery (e.g. "High"), battery_level (0-100), last_sync_time, mac,
    and features list.
    """
    devices = await anyio.to_thread.run_sync(_fetch_devices)
    if not devices:
        return format_response({"message": "No devices found.", "devices": []})
    return format_response({"devices": devices, "count": len(devices)})
