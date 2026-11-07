"""Census batch geocoder retries short outages (offline: a fake client stands in for the Census service)."""
import httpx
import pytest

from app.geo.geocode import BATCH_URL, CensusGeocoder
from app.net.mirrors import RetryableServiceError


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def request(self, method, url, **kw):
        self.calls += 1
        out = self.outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return httpx.Response(out, text="ok", request=httpx.Request(method, url))


def geocoder(outcomes):
    sleeps = []
    client = FakeClient(outcomes)
    return CensusGeocoder(client, retry_delays=(1.0, 2.0), sleep=sleeps.append), client, sleeps


def test_retries_a_502_then_succeeds():
    g, client, sleeps = geocoder([502, 200])
    assert g._post_batch(b"x").status_code == 200
    assert client.calls == 2 and sleeps == [1.0]


def test_retries_timeouts_and_dropped_connections():
    req = httpx.Request("POST", BATCH_URL)
    g, client, sleeps = geocoder([httpx.ReadTimeout("slow", request=req), httpx.ConnectError("reset", request=req), 200])
    assert g._post_batch(b"x").status_code == 200
    assert client.calls == 3 and sleeps == [1.0, 2.0]


def test_gives_up_after_the_last_retry():
    g, client, sleeps = geocoder([503, 502, 502])
    with pytest.raises(RetryableServiceError, match="HTTP 502"):
        g._post_batch(b"x")
    assert client.calls == 3 and sleeps == [1.0, 2.0]


def test_rate_limit_is_retried():
    g, client, sleeps = geocoder([429, 200])
    assert g._post_batch(b"x").status_code == 200 and sleeps == [1.0]


def test_client_errors_are_not_retried():
    g, client, sleeps = geocoder([400])
    with pytest.raises(httpx.HTTPStatusError):
        g._post_batch(b"x")
    assert client.calls == 1 and sleeps == []


def test_default_waits():
    assert CensusGeocoder.RETRY_DELAYS == (10.0, 30.0)
