"""The Google client: paging, windowing, and the failures that look like no data.

Every assertion here guards a mistake whose symptom is an empty result rather
than an error - a page loop that stops early, a filter naming the wrong field,
a response read under the wrong key, a page size the server silently truncates.
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


def _page(points, token=None):
    body = {"dataPoints": points}
    if token:
        body["nextPageToken"] = token
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


def _http_error(code, body=b"{}"):
    err = urllib.error.HTTPError("https://health.googleapis.com/v4/x", code, "err", {}, None)
    err.read = lambda: body
    return err


def _requested_urls(mock):
    return [call.args[0].full_url for call in mock.call_args_list]


class TestPagingToExhaustion:
    def test_an_empty_page_carrying_a_token_is_not_the_end(self):
        """Measured against the real API: a month of steps returned an empty
        first page with a token, and 6025 points behind it. Any loop that
        concludes "no data" from one response reports a gap that is not there.
        """
        pages = [
            _page([], token="t1"),
            _page([], token="t2"),
            _page([{"steps": {"count": 100}}]),
        ]
        with patch("urllib.request.urlopen", side_effect=pages) as m:
            points = api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert len(points) == 1
        assert len(_requested_urls(m)) == 3

    def test_a_page_without_a_token_ends_the_walk(self):
        with patch("urllib.request.urlopen", side_effect=[_page([{"steps": {}}])]) as m:
            api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert len(_requested_urls(m)) == 1

    def test_an_empty_token_ends_the_walk(self):
        pages = [_page([{"steps": {}}], token=""), _page([{"steps": {}}])]
        with patch("urllib.request.urlopen", side_effect=pages) as m:
            api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert len(_requested_urls(m)) == 1

    def test_every_page_after_the_first_carries_the_token(self):
        pages = [_page([], token="tok-1"), _page([])]
        with patch("urllib.request.urlopen", side_effect=pages) as m:
            api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert "pageToken=tok-1" in _requested_urls(m)[1]

    def test_the_walk_is_bounded(self):
        """A server that keeps returning a token must not hang the caller."""
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _page([], token="t")):
            with pytest.raises(api.HealthAPIError, match="too many pages"):
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))


class TestThePageSize:
    def test_every_request_states_it(self):
        """Left unstated the server uses 1440 - or 25 for sleep and exercise."""
        with patch("urllib.request.urlopen", side_effect=[_page([])]) as m:
            api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert "pageSize=" in _requested_urls(m)[0]

    @pytest.mark.parametrize("data_type", ["sleep", "exercise"])
    def test_sleep_and_exercise_ask_for_25(self, data_type):
        """Their maximum is 25 and a larger value is truncated, not refused."""
        with patch("urllib.request.urlopen", side_effect=[_page([])]) as m:
            api.list_google_data_points(data_type, date(2026, 3, 1), date(2026, 3, 2))
        assert "pageSize=25" in _requested_urls(m)[0]

    def test_no_type_asks_for_more_than_the_server_accepts(self):
        for name, spec in api.GOOGLE_TYPES.items():
            assert 1 <= spec.page_size <= api.GOOGLE_MAX_PAGE_SIZE, name


class TestTheTypeTable:
    """The three spellings of each type have to agree, and only one fails loudly."""

    def test_every_entry_is_keyed_by_its_own_path(self):
        for key, spec in api.GOOGLE_TYPES.items():
            assert key == spec.path

    def test_the_filter_field_names_the_same_type_as_the_path(self):
        """A helper called with a mismatched path builds a filter for another type.

        Rollup-only types carry no filter at all - sending one is always an
        error - so there is nothing here to check for them.
        """
        for spec in api.GOOGLE_TYPES.values():
            if not spec.listable:
                continue
            assert spec.filter_on.startswith(spec.path.replace("-", "_") + "."), spec.path

    def test_the_response_field_is_the_paths_camel_case(self):
        """This is the spelling whose mistake is silent.

        A wrong path 404s and a wrong filter is rejected, but a wrong response
        field leaves the request succeeding and every page parsing to nothing,
        which reads as the user having no data of that type.
        """
        for spec in api.GOOGLE_TYPES.values():
            head, *rest = spec.path.split("-")
            assert spec.field == head + "".join(w.capitalize() for w in rest), spec.path

    def test_no_type_is_named_in_the_rollup_exclusions_and_nowhere_else(self):
        """A renamed type leaves its exclusion behind, granting a rollup the API refuses.

        The realistic drift is a consistent rename - path and field moved
        together - which every other assertion here accepts, because they only
        compare the entry against itself.
        """
        dead = sorted(set(api._NO_DAILY_ROLLUP) - set(api.GOOGLE_TYPES))
        assert not dead, f"_NO_DAILY_ROLLUP names no type in GOOGLE_TYPES: {dead}"

    def test_every_type_is_readable_by_at_least_one_path(self):
        """Not listable and not rollupable is a type nothing can fetch."""
        unreadable = sorted(
            spec.path
            for spec in api.GOOGLE_TYPES.values()
            if not spec.listable and spec.path in api._NO_DAILY_ROLLUP
        )
        assert not unreadable, f"no read path for {unreadable}"

    def test_a_date_literal_is_never_sent_where_a_timestamp_is_expected(self):
        for spec in api.GOOGLE_TYPES.values():
            assert spec.literal in ("date", "timestamp"), spec.path
            if spec.literal == "timestamp":
                assert not spec.filter_on.endswith("civil_start_time"), spec.path


class TestTheFilter:
    def test_a_daily_summary_filters_on_its_date(self):
        with patch("urllib.request.urlopen", side_effect=[_page([])]) as m:
            api.list_google_data_points(
                "daily-heart-rate-variability", date(2026, 3, 1), date(2026, 3, 3)
            )
        url = _requested_urls(m)[0]
        assert "daily_heart_rate_variability.date" in url
        assert "2026-03-01" in url and "2026-03-03" in url

    def test_sleep_bins_on_the_end_of_the_night(self):
        """A night starting on the 1st and ending on the 2nd belongs to the 2nd."""
        with patch("urllib.request.urlopen", side_effect=[_page([])]) as m:
            api.list_google_data_points("sleep", date(2026, 3, 1), date(2026, 3, 3))
        assert "sleep.interval.civil_end_time" in _requested_urls(m)[0]

    def test_an_exercise_session_filters_on_its_civil_start(self):
        with patch("urllib.request.urlopen", side_effect=[_page([])]) as m:
            api.list_google_data_points("exercise", date(2026, 3, 1), date(2026, 3, 3))
        assert "exercise.interval.civil_start_time" in _requested_urls(m)[0]

    def test_ecg_is_bounded_below_only(self):
        """Filtering an ECG on end time is documented as unsupported.

        Asking for one anyway is a malformed filter, so the window is open at
        the top and the caller discards what falls outside it.
        """
        with patch("urllib.request.urlopen", side_effect=[_page([])]) as m:
            api.list_google_data_points("electrocardiogram", date(2026, 3, 1), date(2026, 3, 3))
        url = _requested_urls(m)[0]
        assert "electrocardiogram.interval.start_time" in url
        assert url.count("electrocardiogram.interval") == 1
        assert "%3C" not in url and "<" not in url

    def test_every_other_type_is_bounded_on_both_sides(self):
        with patch("urllib.request.urlopen", side_effect=[_page([])]) as m:
            api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 3))
        url = _requested_urls(m)[0]
        assert url.count("steps.interval") == 2


class TestTheResponseKey:
    def test_points_come_back_whole(self):
        """The caller reads the type's own camelCase field off each point.

        Path, filter and response field are three different spellings of one
        data type, and only the third fails silently - the request succeeds
        and every page parses to nothing.
        """
        with patch("urllib.request.urlopen", side_effect=[_page([{"steps": {"count": 7}}])]):
            points = api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert points == [{"steps": {"count": 7}}]

    def test_a_response_with_no_data_points_key_is_an_empty_page(self):
        resp = MagicMock()
        resp.read.return_value = b"{}"
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", side_effect=[resp]):
            assert api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2)) == []


class TestFailures:
    def test_a_missing_scope_and_an_unpublished_app_are_told_apart(self):
        body = json.dumps(
            {"error": {"status": "PERMISSION_DENIED", "message": "insufficient scopes"}}
        ).encode()
        with patch("urllib.request.urlopen", side_effect=_http_error(403, body)):
            with pytest.raises(api.HealthAuthError) as excinfo:
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert "scope" in str(excinfo.value).lower()

    def test_no_part_of_the_error_body_reaches_the_message(self):
        body = json.dumps({"error": {"message": "SECRETMARKER"}}).encode()
        with patch("urllib.request.urlopen", side_effect=_http_error(403, body)):
            with pytest.raises(Exception) as excinfo:
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert "SECRETMARKER" not in str(excinfo.value)

    def test_a_rate_limit_is_reported_as_one(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(429)):
            with pytest.raises(api.HealthRateLimitError):
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))

    def test_a_gateway_timeout_retries_with_a_smaller_page(self):
        """Google's own advice: do not re-send a large payload immediately."""
        pages = [_http_error(504), _page([])]
        with patch("urllib.request.urlopen", side_effect=pages) as m:
            api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        urls = _requested_urls(m)
        first = int(urls[0].split("pageSize=")[1].split("&")[0])
        second = int(urls[1].split("pageSize=")[1].split("&")[0])
        assert second == first // 2

    def test_a_gateway_timeout_mid_walk_does_not_replay_a_token_at_a_new_size(self):
        """A page token may only be replayed with every other field unchanged.

        The first version halved the page size and carried on with the token
        in hand, which is the one pairing the API rules out - and the test
        that covered it fired the 504 on the first page, where no token
        exists yet.
        """
        pages = [_page([{"steps": {}}], token="TOK1"), _http_error(504), _page([{"steps": {}}])]
        with patch("urllib.request.urlopen", side_effect=pages) as m:
            api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        for url in _requested_urls(m):
            size = int(url.split("pageSize=")[1].split("&")[0])
            if "pageToken=" in url:
                assert size == api.GOOGLE_MAX_PAGE_SIZE, url

    def test_a_restarted_walk_does_not_double_count(self):
        """Restarting after a 504 re-fetches, so anything already held is dropped."""
        pages = [_page([{"steps": {"count": 1}}], token="TOK1"), _http_error(504), _page([])]
        with patch("urllib.request.urlopen", side_effect=pages):
            points = api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert points == []

    def test_the_walk_gives_up_on_wall_clock_not_only_on_page_count(self, monkeypatch):
        """500 pages at the socket timeout is hours on the tool-call thread."""
        monkeypatch.setattr(api, "GOOGLE_WALK_DEADLINE", 0)
        with patch("urllib.request.urlopen", side_effect=lambda *a, **k: _page([], token="t")):
            with pytest.raises(api.HealthAPIError, match="gave up"):
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))

    def test_a_rejected_token_is_not_answered_with_publishing_advice(self):
        with patch("urllib.request.urlopen", side_effect=_http_error(401)):
            with pytest.raises(api.HealthAuthError) as excinfo:
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert "publish" not in str(excinfo.value).lower()

    def test_a_data_points_field_of_the_wrong_shape_is_refused(self):
        resp = MagicMock()
        resp.read.return_value = json.dumps({"dataPoints": {"steps": 1}}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", side_effect=[resp]):
            with pytest.raises(api.HealthAPIError, match="shape"):
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))

    def test_offline_mode_refuses_before_any_request(self, monkeypatch):
        monkeypatch.setattr(config, "OFFLINE_MODE", True)
        with patch("urllib.request.urlopen") as m:
            with pytest.raises(api.HealthOfflineError):
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert not m.called

    def test_an_unreadable_body_is_reported_rather_than_escaping(self):
        resp = MagicMock()
        resp.read.return_value = b"<html>captive portal</html>"
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda *a: None
        with patch("urllib.request.urlopen", side_effect=[resp]):
            with pytest.raises(api.HealthAPIError):
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))

    def test_an_unknown_data_type_is_refused_before_the_network(self):
        with patch("urllib.request.urlopen") as m:
            with pytest.raises(ValueError, match="not-a-type"):
                api.list_google_data_points("not-a-type", date(2026, 3, 1), date(2026, 3, 2))
        assert not m.called


class TestTheTokenLayerIsClassified:
    """The two types the refresh boundary guarantees, mapped at their only caller.

    `doctor` grades the two differently and answers one of them with "run
    auth", which rotates the token file the syncing host owns - so a condition
    that clears on its own must never arrive as the other. The messages are
    asserted by equality because `run_sync` writes them into `sync_log` and
    hands them to the client: a substring check passes however much a future
    version interpolates alongside.
    """

    def _list_with_refresh_raising(self, exc, monkeypatch):
        from google_health_mcp import auth

        monkeypatch.setattr(api, "refresh_google_token", MagicMock(side_effect=exc))
        return api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2)), auth

    def test_a_refusal_becomes_an_auth_error(self, monkeypatch):
        from google_health_mcp import auth

        with pytest.raises(api.HealthAuthError) as caught:
            self._list_with_refresh_raising(auth.TokenRefused("no"), monkeypatch)
        assert str(caught.value) == "Could not obtain an access token. Run: google-health-mcp auth"

    def test_a_network_failure_is_not_an_auth_failure(self, monkeypatch):
        from google_health_mcp import auth

        with pytest.raises(api.HealthAPIError) as caught:
            self._list_with_refresh_raising(auth.RefreshNetworkError("no route"), monkeypatch)
        assert not isinstance(caught.value, api.HealthAuthError)
        assert str(caught.value) == "Network error. Check your connection."

    def test_neither_message_carries_the_original(self, monkeypatch):
        """A refresh failure's own text is a filesystem path often enough."""
        from google_health_mcp import auth

        for exc in (
            auth.TokenRefused("/etc/secret/path is missing"),
            auth.RefreshNetworkError("/etc/secret/path timed out"),
        ):
            with pytest.raises(Exception) as caught:
                self._list_with_refresh_raising(exc, monkeypatch)
            assert "/etc/secret" not in str(caught.value)

    def test_a_transport_failure_carries_no_path(self):
        """A TLS or socket failure's own text is an absolute path."""
        error = urllib.error.URLError(
            OSError(2, "No such file or directory: '/home/someone/certs/ca.pem'")
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with pytest.raises(api.HealthAPIError) as caught:
                api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))
        assert "/home/someone" not in str(caught.value)
        assert str(caught.value) == "Network error. Check your connection."


class TestAgainstTheRealGoogleRefresh:
    """Driven through the real auth code, so the classification is a fact.

    Every test above injects the exception it expects, which pins what the
    client does with one but not that auth raises that kind at all. A broken
    token file exercised end to end is what proves the two agree.
    """

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        from google_health_mcp import auth

        monkeypatch.setattr(config, "OFFLINE_MODE", False)
        monkeypatch.setattr(auth, "_cached_google_tokens", None)
        monkeypatch.setattr(auth, "_cached_google_client", None)
        monkeypatch.setattr(config, "GOOGLE_TOKENS_PATH", tmp_path / "google_tokens.json")
        monkeypatch.setattr(config, "GOOGLE_CLIENT_PATH", tmp_path / "google_client.json")
        self.dir = tmp_path
        yield
        auth._cached_google_tokens = None
        auth._cached_google_client = None

    def _list_with_token_file(self, contents):
        if contents is not None:
            (self.dir / "google_tokens.json").write_text(contents)
        (self.dir / "google_client.json").write_text(
            json.dumps({"installed": {"client_id": "fake-id", "client_secret": "fake-secret"}})
        )
        return api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 2))

    def test_a_missing_token_file_is_an_auth_failure(self):
        with pytest.raises(api.HealthAuthError):
            self._list_with_token_file(None)

    def test_a_token_file_that_is_not_json_is_an_auth_failure(self):
        with pytest.raises(api.HealthAuthError):
            self._list_with_token_file("{not json")

    def test_a_token_file_with_no_refresh_token_is_an_auth_failure(self):
        with pytest.raises(api.HealthAuthError):
            self._list_with_token_file('{"access_token": "a", "expires_at": 0}')

    def test_a_refresh_that_cannot_reach_google_is_not_an_auth_failure(self, monkeypatch):
        from google_health_mcp import auth

        monkeypatch.setattr(
            auth.urllib.request,
            "urlopen",
            MagicMock(side_effect=urllib.error.URLError("no route")),
        )
        with pytest.raises(api.HealthAPIError) as caught:
            self._list_with_token_file(
                '{"access_token": "a", "refresh_token": "r", "expires_at": 0}'
            )
        assert not isinstance(caught.value, api.HealthAuthError)


class TestAnEmptyWindow:
    def test_a_zero_length_window_asks_nothing(self):
        """Measured: the API refuses it with INVALID_TIME_RANGE.

        An incremental sync produces exactly this window whenever it is
        already up to date, so requesting it would turn a no-op into an error
        row on every run.
        """
        with patch("urllib.request.urlopen") as m:
            assert api.list_google_data_points("steps", date(2026, 3, 1), date(2026, 3, 1)) == []
        assert not m.called

    def test_a_backwards_window_asks_nothing(self):
        with patch("urllib.request.urlopen") as m:
            assert api.list_google_data_points("steps", date(2026, 3, 2), date(2026, 3, 1)) == []
        assert not m.called
