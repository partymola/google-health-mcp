"""A date range that cannot be queried is refused before anything is fetched.

An empty answer to an inverted range reads as a period without data, and a
refused date that syncs first costs a request. Driven through `mcp.call_tool`
over every registered tool taking a `start_date`, so a date tool added later
is covered without being named here.
"""

import asyncio

import pytest

import google_health_mcp.cli  # noqa: F401 - registers every tool module
from google_health_mcp import db, helpers
from google_health_mcp.mcp_instance import mcp


class _Exists:
    def exists(self):
        return True


def _date_tools():
    return sorted(
        t.name
        for t in asyncio.run(mcp.list_tools())
        if "start_date" in t.input_schema.get("properties", {})
    )


def _cache_refused(tool):
    # pytest.fail, not assert: auto_sync_if_stale catches Exception, and
    # `Failed` is a BaseException it cannot absorb.
    def _refuse(*a, **k):
        pytest.fail(f"{tool} opened the cache before refusing its dates")

    return _refuse


def _refusal(monkeypatch, tool, args):
    """The text a tool answers `args` with, failing if it reached the cache."""
    monkeypatch.setattr(helpers, "GOOGLE_CLIENT_PATH", _Exists())
    monkeypatch.setattr(helpers, "GOOGLE_TOKENS_PATH", _Exists())
    # Every sync, and every query, starts by opening the cache.
    monkeypatch.setattr(db, "get_db", _cache_refused(tool))
    try:
        asyncio.run(mcp.call_tool(tool, args))
    except Exception as e:
        return str(e)
    pytest.fail(f"{tool} answered {args} instead of refusing them")


def test_the_stub_raises_what_a_sync_cannot_swallow():
    """Raising an Exception, it would pass every tool that syncs first."""
    with pytest.raises(BaseException) as caught:
        _cache_refused("health_get_heart_rate")()
    assert not isinstance(caught.value, Exception)


def test_the_date_tools_are_found():
    """Otherwise the parametrised test below passes over nothing."""
    tools = _date_tools()
    assert "health_trends" in tools
    assert "health_get_heart_rate" in tools


@pytest.mark.parametrize(
    ("dates", "expected"),
    [
        (("2026-03-02", "2026-03-01"), "is after end date 2026-03-01"),
        (("2026-13", None), "YYYY-MM-DD"),
    ],
    ids=["inverted", "impossible"],
)
@pytest.mark.parametrize("tool", _date_tools())
def test_every_date_tool_refuses_a_bad_range_before_any_sync(monkeypatch, tool, dates, expected):
    start_date, end_date = dates
    args = {"start_date": start_date} | ({"end_date": end_date} if end_date else {})
    assert expected in _refusal(monkeypatch, tool, args)


def test_a_bad_compare_period_is_refused_before_any_sync(monkeypatch):
    said = _refusal(monkeypatch, "health_trends", {"compare": "2026-13 vs 2026-02"})
    assert "Cannot parse period '2026-13'" in said
