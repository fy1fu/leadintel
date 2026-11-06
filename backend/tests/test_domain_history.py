"""Domain history observations (M3): Wayback CDX and RDAP parsing with a fake client (offline). Failures and odd
answers become "unavailable" / "not found" - never a guessed date - and these are observations, not WQS criteria."""
import json

from app.crawl import history


class R:
    def __init__(self, status, body):
        self.status_code, self.text = status, body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


class Client:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        a = self.answers(url, params or {})
        if isinstance(a, Exception):
            raise a
        return a


def test_wayback_first_and_last_capture():
    def ans(url, p):
        return R(200, [["timestamp"], ["20090314120000"]] if p.get("limit") == "1" else [["timestamp"], ["20261020083000"]])
    c = Client(ans)
    w = history.wayback(c, "https://www.acme-lawn.com/about")
    assert w["first_capture"] == "2009-03-14" and w["last_capture"] == "2026-10-20" and w["status"] == "ok"
    assert all(u == history.CDX_URL and p["url"] == "acme-lawn.com" for u, p in c.calls)


def test_wayback_without_captures_or_with_junk():
    assert history.wayback(Client(lambda u, p: R(200, [])), "http://x.com")["status"] == "no captures"
    bad = history.wayback(Client(lambda u, p: R(200, "<html>not json")), "http://x.com")
    assert bad["status"].startswith("unavailable") and bad["first_capture"] is None
    junk = history.wayback(Client(lambda u, p: R(200, [["timestamp"], ["notadate"]])), "http://x.com")
    assert junk["first_capture"] is None
    down = history.wayback(Client(lambda u, p: R(503, "")), "http://x.com")
    assert down["status"] == "unavailable (ValueError)"


def test_rdap_dates_and_limits():
    body = {"events": [{"eventAction": "registration", "eventDate": "2004-05-01T00:00:00Z"},
                       {"eventAction": "last changed", "eventDate": "2025-02-03T10:00:00Z"}]}
    c = Client(lambda u, p: R(200, body))
    r = history.rdap(c, "https://shop.acme-lawn.com/")
    assert r["registered"] == "2004-05-01" and r["last_changed"] == "2025-02-03" and r["status"] == "ok"
    assert c.calls[0][0] == "https://rdap.verisign.com/com/v1/domain/acme-lawn.com"
    assert history.rdap(Client(lambda u, p: R(404, "")), "http://gone.net")["status"] == "not found"
    org = Client(lambda u, p: R(200, body))
    assert history.rdap(org, "http://acme.org")["status"].startswith("not assessed") and org.calls == []
    assert history.rdap(Client(lambda u, p: TimeoutError()), "http://x.com")["status"] == "unavailable (TimeoutError)"


def test_domain_history_combines_both():
    h = history.domain_history(Client(lambda u, p: R(500, "")), "http://www.example.com/x")
    assert h["domain"] == "example.com" and set(h) == {"domain", "wayback", "rdap"}
