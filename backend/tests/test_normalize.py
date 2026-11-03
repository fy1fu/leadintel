import pytest

from app.normalize.core import address_key, classify_url, email_norm, is_po_box, name_norm, phone_e164


@pytest.mark.parametrize("raw,norm", [
    ("ABC Landscaping, LLC", "abc landscaping"), ("The Clean Team Inc.", "clean team"),
    ("Smith & Sons Plumbing L.L.C.", "smith and sons plumbing"), ("Café Ñandú", "cafe nandu"),
    ("LLC Services Co", "llc services"),
])
def test_name_norm(raw, norm):
    assert name_norm(raw) == norm


def test_phone_and_email():
    assert phone_e164("(904) 264-6171") == "+19042646171"
    assert phone_e164("9042646171") == "+19042646171" and phone_e164("+19042646171") == "+19042646171"
    assert phone_e164("123") is None and phone_e164("") is None
    assert email_norm("Info@Example.COM ") == "info@example.com" and email_norm("not-an-email") is None


@pytest.mark.parametrize("url,platform,kind", [
    ("https://www.facebook.com/CoolCatsHVAC/", "facebook", "social"),
    ("http://m.facebook.com/profile.php?id=1&fbclid=x", "facebook", "social"),
    ("instagram.com/jaxlawn", "instagram", "social"),
    ("https://linktr.ee/jaxclean", "linktree", "link_hub"),
    ("https://www.yelp.com/biz/x", "yelp", "listing"),
    ("https://maps.app.goo.gl/abc", "google_listing", "listing"),
    ("https://www.google.com/maps/place/x", "google_listing", "listing"),
    ("https://www.callcoolcats.com/?utm_source=gbp", "website", "independent"),
    ("https://jaxpressure.wixsite.com/home", "website", "independent"),
])
def test_url_classification(url, platform, kind):
    c = classify_url(url)
    assert (c.platform, c.platform_kind) == (platform, kind)


def test_social_urls_are_never_independent_and_empty_urls_dropped():
    assert classify_url("") is None and classify_url("   ") is None and classify_url(None) is None
    assert classify_url("https://facebook.com/x").platform_kind != "independent"
    assert classify_url("https://jaxpressure.wixsite.com/home").unbranded_domain is True
    assert classify_url("https://www.callcoolcats.com/?utm_source=gbp").url_norm == "https://callcoolcats.com"


def test_address_key_and_po_box():
    a = address_key("4480 Eternal Prince Drive", None, "33573-1234")
    b = address_key("4480 ETERNAL PRINCE DR.", "", "33573")
    assert a == b == "4480 ETERNAL PRINCE DR|33573"
    assert address_key("123 Main St", "Suite 200", "32202") != address_key("123 Main St", "Suite 300", "32202")
    assert address_key("123 Main St", None, None) is None
    assert is_po_box("P.O. Box 123") and not is_po_box("123 Box Elder Rd")
