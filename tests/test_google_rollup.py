"""Daily rollups: the read path for types that refuse to be listed.

Measured against the live API: `floors`, `total-calories` and
`calories-in-heart-rate-zone` answer a list request with "List is not supported
for data type X". `floors` is a column this package already syncs, so without
this path it cannot be filled at all.
"""

import json
import urllib.error
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from google_health_mcp import api, config


@pytest.fixture(autouse=True)
def _a_token_without_the_network(monkeypatch):
    monkeypatch.setattr(api, "refresh_google_token", lambda: "fake-access-token")
    monkeypatch.setattr(config, "OFFLINE_MODE", False)


def _rollup(points):
    resp = MagicMock()
    resp.read.return_value = json.dumps({"rollupDataPoints": points}).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


def _bodies(mock):
    return [json.loads(call.args[0].data.decode()) for call in mock.call_args_list]


def _day(point):
    return point["civilStartTime"]


class TestTheResponseKey:
    def test_points_are_read_from_rollup_data_points(self):
        """A rollup answers under `rollupDataPoints`, not `dataPoints`.

        Reading it under the list key returns nothing while the request
        succeeds - which is how a day of climbed floors becomes no data.
        """
        with patch("urllib.request.urlopen", side_effect=[_rollup([{"floors": {"sum": 3}}])]):
            got = api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 2))
        assert got == [{"floors": {"sum": 3}}]

    def test_a_response_carrying_the_list_key_yields_nothing(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"dataPoints": [{"floors": {}}]}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", side_effect=[resp]):
            assert api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 2)) == []

    def test_a_rollup_field_of_the_wrong_shape_is_refused(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"rollupDataPoints": {"floors": 1}}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", side_effect=[resp]):
            with pytest.raises(api.HealthAPIError, match="shape"):
                api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 2))


class TestTheRequest:
    def test_it_posts_a_civil_range(self):
        with patch("urllib.request.urlopen", side_effect=[_rollup([])]) as m:
            api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 4))
        body = _bodies(m)[0]
        assert body["range"]["start"]["date"] == {"year": 2026, "month": 3, "day": 1}
        assert body["range"]["end"]["date"] == {"year": 2026, "month": 3, "day": 4}

    def test_it_never_sends_a_page_size(self):
        """Measured: dailyRollUp answers `total-calories` with 400 when pageSize
        is present and succeeds without it. The response carries no page token,
        and a capped window cannot overflow one anyway.
        """
        with patch("urllib.request.urlopen", side_effect=[_rollup([])]) as m:
            api.daily_roll_up("total-calories", date(2026, 3, 1), date(2026, 3, 4))
        assert "pageSize" not in _bodies(m)[0]

    def test_it_states_the_window_size(self):
        with patch("urllib.request.urlopen", side_effect=[_rollup([])]) as m:
            api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 4))
        assert _bodies(m)[0]["windowSizeDays"] == 1

    def test_an_unexpected_page_token_is_loud(self):
        """The response schema has no such field; one appearing means the
        assumption that a capped window is never paged has stopped holding.
        """
        resp = MagicMock()
        resp.read.return_value = json.dumps(
            {"rollupDataPoints": [], "nextPageToken": "surprise"}
        ).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", side_effect=[resp]):
            with pytest.raises(api.HealthAPIError, match="page token"):
                api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 4))


class TestChunking:
    def test_a_window_inside_the_cap_is_one_request(self):
        with patch("urllib.request.urlopen", side_effect=[_rollup([])]) as m:
            api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 4, 1))
        assert len(_bodies(m)) == 1

    def test_a_window_over_the_cap_is_split(self):
        """A 30-day ask on a 14-day-cap type is three closed-open chunks."""
        pages = [_rollup([]) for _ in range(5)]
        with patch("urllib.request.urlopen", side_effect=pages) as m:
            api.daily_roll_up("total-calories", date(2026, 3, 1), date(2026, 3, 31))
        bodies = _bodies(m)
        assert len(bodies) == 3

    def test_the_chunks_are_closed_open_and_do_not_overlap(self):
        pages = [_rollup([]) for _ in range(5)]
        with patch("urllib.request.urlopen", side_effect=pages) as m:
            api.daily_roll_up("total-calories", date(2026, 3, 1), date(2026, 3, 31))
        edges = [
            (b["range"]["start"]["date"]["day"], b["range"]["end"]["date"]["day"])
            for b in _bodies(m)
        ]
        for (_, previous_end), (next_start, _) in zip(edges, edges[1:]):
            assert previous_end == next_start
        assert edges[0][0] == 1

    def test_no_chunk_exceeds_the_types_cap(self):
        pages = [_rollup([]) for _ in range(9)]
        with patch("urllib.request.urlopen", side_effect=pages) as m:
            api.daily_roll_up("total-calories", date(2026, 1, 1), date(2026, 3, 1))
        for body in _bodies(m):
            span = date(**{k: v for k, v in body["range"]["end"]["date"].items()}) - date(
                **{k: v for k, v in body["range"]["start"]["date"].items()}
            )
            assert span.days <= api.GOOGLE_TYPES["total-calories"].rollup_cap_days

    def test_points_from_every_chunk_come_back(self):
        pages = [
            _rollup([{"civilStartTime": "2026-03-01", "totalCalories": {}}]),
            _rollup([{"civilStartTime": "2026-03-15", "totalCalories": {}}]),
            _rollup([{"civilStartTime": "2026-03-29", "totalCalories": {}}]),
        ]
        with patch("urllib.request.urlopen", side_effect=pages):
            got = api.daily_roll_up("total-calories", date(2026, 3, 1), date(2026, 3, 31))
        assert [_day(p) for p in got] == ["2026-03-01", "2026-03-15", "2026-03-29"]

    def test_a_day_with_no_rollup_point_is_absent_rather_than_zero(self):
        """Absence is not zero, and the client must not decide which it is.

        A rollup covering fourteen days returned twelve points on real data.
        Whether the two missing days are off-wrist or genuinely nothing is a
        question for whatever writes them, not for the fetch.
        """
        with patch(
            "urllib.request.urlopen",
            side_effect=[_rollup([{"civilStartTime": "2026-03-01", "floors": {"sum": 2}}])],
        ):
            got = api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 4))
        assert len(got) == 1


class TestTheTwoReadPathsDoNotCross:
    def test_listing_a_rollup_only_type_is_refused_before_the_network(self):
        with patch("urllib.request.urlopen") as m:
            with pytest.raises(ValueError, match="daily_roll_up"):
                api.list_google_data_points("floors", date(2026, 3, 1), date(2026, 3, 2))
        assert not m.called

    def test_rolling_up_a_type_that_has_no_rollup_is_refused_before_the_network(self):
        with patch("urllib.request.urlopen") as m:
            with pytest.raises(ValueError, match="sleep"):
                api.daily_roll_up("sleep", date(2026, 3, 1), date(2026, 3, 2))
        assert not m.called

    def test_a_rollup_only_type_carries_no_filter(self):
        """A filter that is always an error to send should not exist."""
        for spec in api.GOOGLE_TYPES.values():
            if spec.listable:
                assert spec.filter_on, spec.path
            else:
                assert spec.filter_on is None, spec.path

    def test_offline_mode_refuses_before_any_request(self, monkeypatch):
        monkeypatch.setattr(config, "OFFLINE_MODE", True)
        with patch("urllib.request.urlopen") as m:
            with pytest.raises(api.HealthOfflineError):
                api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 2))
        assert not m.called

    def test_a_gateway_timeout_is_reported_rather_than_shrinking_the_window(self):
        """A capped rollup is at most 90 points, so there is nothing to halve.

        The list walk halves its page because it asks for up to 10,000 at once;
        repeating that machinery here would be answering a problem this path
        does not have.
        """
        err = urllib.error.HTTPError("https://x", 504, "err", {}, None)
        err.read = lambda: b"{}"
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(api.HealthAPIError):
                api.daily_roll_up("floors", date(2026, 3, 1), date(2026, 3, 2))
