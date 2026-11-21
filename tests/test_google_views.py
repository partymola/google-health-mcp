"""The merged stream: the same walk, asking for Google's merge of the sources.

`reconcile` is read with the same paging as a list. It changes what is asked
for rather than how it is parsed, so the tests read the requests sent.
"""

import json
from datetime import date
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from google_health_mcp import api, config


@pytest.fixture(autouse=True)
def _a_token_without_the_network(monkeypatch):
    monkeypatch.setattr(api, "refresh_google_token", lambda: "fake-access-token")
    monkeypatch.setattr(config, "OFFLINE_MODE", False)
    monkeypatch.setattr(api.time, "sleep", lambda _s: None)


def _answer(points, token=None):
    body = {"dataPoints": points}
    if token:
        body["nextPageToken"] = token
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


def _window(url):
    """The two dates a request's filter names."""
    (flt,) = parse_qs(urlsplit(url).query)["filter"]
    low, high = (part.split('"')[1] for part in flt.split(" AND "))
    return date.fromisoformat(low), date.fromisoformat(high)


class TestTheMergedStream:
    def test_reconcile_asks_for_the_merged_stream_with_the_same_filter(self):
        sent = []

        def urlopen(req, timeout=None):
            sent.append(req)
            return _answer([])

        with patch("urllib.request.urlopen", side_effect=urlopen):
            api.list_google_data_points(
                "sedentary-period", date(2026, 3, 1), date(2026, 3, 8), view="reconcile"
            )
        (req,) = sent
        path = urlsplit(req.full_url).path
        assert path.endswith("/users/me/dataTypes/sedentary-period/dataPoints:reconcile")
        assert req.data is None
        assert _window(req.full_url) == (date(2026, 3, 1), date(2026, 3, 8))
        assert "sedentary_period.interval.civil_start_time" in req.full_url

    def test_the_default_is_the_per_source_list(self):
        with patch("urllib.request.urlopen", side_effect=[_answer([])]) as m:
            api.list_google_data_points("sedentary-period", date(2026, 3, 1), date(2026, 3, 8))
        assert urlsplit(m.call_args.args[0].full_url).path.endswith("/dataPoints")

    def test_reconcile_pages_to_exhaustion(self):
        """An empty first page with a token is not an empty window, here too."""
        answers = [_answer([], token="t1"), _answer([{"sedentaryPeriod": {}}])]
        with patch("urllib.request.urlopen", side_effect=answers) as m:
            points = api.list_google_data_points(
                "sedentary-period", date(2026, 3, 1), date(2026, 3, 8), view="reconcile"
            )
        assert len(points) == 1
        urls = [c.args[0].full_url for c in m.call_args_list]
        assert "pageToken=t1" in urls[1]
        assert ":reconcile" in urls[1]

    def test_a_failure_names_the_reconcile_operation_and_nothing_else(self):
        import urllib.error

        err = urllib.error.HTTPError("https://x", 418, "err", {}, None)
        err.read = lambda: b"{}"
        with patch("urllib.request.urlopen", side_effect=err):
            with pytest.raises(api.HealthAPIError) as raised:
                api.list_google_data_points(
                    "sedentary-period", date(2026, 3, 1), date(2026, 3, 8), view="reconcile"
                )
        assert str(raised.value) == "API error 418 for sedentary-period reconcile"

    def test_an_unknown_view_is_refused_before_any_request(self):
        with patch("urllib.request.urlopen", side_effect=pytest.fail) as m:
            with pytest.raises(ValueError):
                api.list_google_data_points(
                    "sedentary-period", date(2026, 3, 1), date(2026, 3, 8), view="rollup"
                )
        assert not m.called

    def test_a_long_range_is_one_walk(self):
        """Height is read from 1970 on every sync."""
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _answer([])) as m:
            api.list_google_data_points("height", date(1970, 1, 1), date(2026, 3, 1))
        assert m.call_count == 1


class TestTheTypesThisReleaseReads:
    @pytest.mark.parametrize(
        "path,field,filter_on",
        [
            ("altitude", "altitude", "altitude.interval.civil_start_time"),
            ("swim-lengths-data", "swimLengthsData", "swim_lengths_data.interval.civil_start_time"),
            (
                "respiratory-rate-sleep-summary",
                "respiratoryRateSleepSummary",
                "respiratory_rate_sleep_summary.sample_time.civil_time",
            ),
            ("sedentary-period", "sedentaryPeriod", "sedentary_period.interval.civil_start_time"),
            ("daily-heart-rate-zones", "dailyHeartRateZones", "daily_heart_rate_zones.date"),
        ],
    )
    def test_each_is_spelled_as_measured(self, path, field, filter_on):
        """Read off the live probe's key paths: a wrong field parses to nothing."""
        spec = api.GOOGLE_TYPES[path]
        assert (spec.field, spec.filter_on) == (field, filter_on)

    def test_the_respiratory_summary_has_no_daily_rollup(self):
        with pytest.raises(ValueError, match="no daily rollup"):
            api.daily_roll_up("respiratory-rate-sleep-summary", date(2026, 3, 1), date(2026, 3, 2))
