"""Every handler asks Google for a window that holds the days it was given.

The handler tests answer the same points whatever window they are asked
for, so a handler asking for too little passes them all. This asks every
handler for one day and checks that day sits inside each request it sends.
"""

from datetime import date
from unittest.mock import patch

import pytest

from google_health_mcp import db
from google_health_mcp.tools import google_sync

_DAY = date(2026, 3, 15)

#: Every handler storing a day at a time, each named for its table.
_DAY_LIST_HANDLERS = sorted(set(google_sync.GOOGLE_SYNC_HANDLERS) & db._DAY_LIST_TABLES)


def _windows(tmp_db, handler, day):
    asked = []

    def record(data_type, start, end, view="list"):
        asked.append((data_type, start, end))
        return []

    with (
        patch.object(google_sync.api, "list_google_data_points", side_effect=record),
        patch.object(google_sync.api, "daily_roll_up", side_effect=record),
        patch.object(google_sync.api, "get_profile", return_value={}),
        patch.object(google_sync.api, "get_settings", return_value={}),
        patch.object(google_sync.api, "get_irn_profile", return_value={}),
    ):
        handler(tmp_db, day, day)
    return asked


def test_every_handler_asks_for_the_day_it_is_given(tmp_db):
    for name, handler in google_sync.GOOGLE_SYNC_HANDLERS.items():
        asked = _windows(tmp_db, handler, _DAY)
        if name == "account":
            continue
        assert asked, f"{name} asked for nothing"
        for data_type, start, end in asked:
            assert start <= _DAY < end, f"{name} asked {data_type} for [{start}, {end})"


def test_the_handlers_storing_a_day_at_a_time_are_the_day_list_tables(tmp_db):
    """Found by what each handler calls, so one missing from the tables cannot hide."""
    storing = set()
    for name, handler in google_sync.GOOGLE_SYNC_HANDLERS.items():
        with patch.object(google_sync, "_store_day_lists", return_value=0) as store:
            _windows(tmp_db, handler, _DAY)
        if store.called:
            storing.add(name)
    assert storing == db._DAY_LIST_TABLES == set(_DAY_LIST_HANDLERS)


@pytest.mark.parametrize("name", _DAY_LIST_HANDLERS)
def test_a_day_list_request_has_a_day_of_margin_each_side(tmp_db, name):
    handler = google_sync.GOOGLE_SYNC_HANDLERS[name]
    ((_, start, end),) = _windows(tmp_db, handler, _DAY)
    assert (start, end) == (date(2026, 3, 14), date(2026, 3, 17))


@pytest.mark.parametrize("name", _DAY_LIST_HANDLERS)
def test_the_margin_stops_at_the_first_date_there_is(tmp_db, name):
    """A backfill from the first representable date has no day before it."""
    handler = google_sync.GOOGLE_SYNC_HANDLERS[name]
    ((_, start, _end),) = _windows(tmp_db, handler, date.min)
    assert start == date.min
