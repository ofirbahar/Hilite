"""A business with no site of its own: its address is a page on Facebook, Wolt, Google Maps...

It gets a full check and a score like any business: is the page open to AI engines, does
it say enough, is there a phone and opening hours, and (with a key) its Google profile.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bvm.ai_providers import Answer
from bvm.analyzers.technical_seo import TechnicalSeo
from bvm.collectors.ai_visibility import AiVisibility
from bvm.collectors.site_crawler import SiteCrawler, parse_page
from bvm.config import platform_of
from bvm.pipeline import run
from bvm.report import render_report
from bvm.store import Store
from bvm.web import create_app
from tests.conftest import Handler
from tests.test_ai_and_web import FakeAssistant


def keys(cfg, facts):
    return [f.key for f in TechnicalSeo().analyze(cfg, facts)]


def test_a_page_on_a_known_platform_is_recognised():
    assert platform_of("https://www.facebook.com", "/pizza.roma") == "פייסבוק"
    assert platform_of("https://m.facebook.com", "/pizza.roma/") == "פייסבוק"
    assert platform_of("https://www.instagram.com", "/pizza_roma") == "אינסטגרם"
    assert platform_of("https://wolt.com", "/he/isr/haifa/restaurant/roma") == "Wolt"
    assert platform_of("https://www.google.com", "/maps/place/Roma") == "גוגל מפות"
    assert platform_of("https://maps.app.goo.gl", "/AbC123") == "גוגל מפות"
    # the platform's own home page is a website like any other, and so is everything else
    assert platform_of("https://www.facebook.com", "/") == ""
    assert platform_of("https://www.google.com", "/search") == ""
    assert platform_of("https://www.ikea.com", "/il/he/") == ""
    assert platform_of("https://notfacebook.com", "/x") == ""


def test_only_the_business_page_is_read_never_the_rest_of_the_platform(cfg, site):
    cfg.platform, cfg.start_path = "פייסבוק", "/about"
    facts = SiteCrawler().collect(cfg)
    pages = [f for f in facts if f.kind == "page"]
    assert [p.subject for p in pages] == [site + "/about"]      # its link to "/" is not followed
    assert pages[0].data["h1"] == ["About", "Team"]
    assert facts[0].kind == "site" and facts[0].data["profile"] == "פייסבוק"
    assert facts[0].data["ai_crawlers_blocked"] == ["GPTBot"]   # the platform's rules, for this page
    assert sorted(set(Handler.hits)) == ["/about", "/robots.txt"]


def test_page_facts_include_phone_and_opening_hours():
    with_both = parse_page("https://x.test/p", '<html><body><a href="tel:+97235551234">חייגו</a>'
                                               '<p>פתוח 10:00-23:00</p></body></html>')
    assert with_both["has_phone"] and with_both["has_hours"]
    written = parse_page("https://x.test/p", "<html><body>טלפון: 03-555-1234</body></html>")
    assert written["has_phone"] and not written["has_hours"]
    neither = parse_page("https://x.test/p", "<html><body>פיצה טובה מאז 1999</body></html>")
    assert not neither["has_phone"] and not neither["has_hours"]


def test_a_readable_business_page_gets_a_full_check(cfg):
    cfg.platform, cfg.start_path = "Wolt", "/about"         # three words, no phone, no hours
    found = TechnicalSeo().analyze(cfg, SiteCrawler().collect(cfg))
    assert [f.key for f in found] == [
        "profile-closed-to-ai", "no-own-site", "check-google-profile", "check-profile-thin"]
    assert found[0].evidence == ["GPTBot"] and "Wolt" in found[0].detail
    for f in found:                                  # the same short, plain wording as every task
        assert len(f.title.split()) <= 5 and len(f.detail.split()) <= 10, f.key
        assert len(f.how_to_fix.split()) <= 10 and f.owner and f.tech, f.key


def test_a_page_the_platform_keeps_closed_is_still_checked(cfg, site):
    cfg.platform, cfg.start_path = "פייסבוק", "/private/notes"
    facts = SiteCrawler().collect(cfg)
    assert "/private/notes" not in Handler.hits       # the platform said no, so it is not read
    found = TechnicalSeo().analyze(cfg, facts)
    assert [f.key for f in found] == ["profile-closed-to-ai", "no-own-site", "check-google-profile"]
    assert "פייסבוק" in found[0].detail

    cfg.platform = "גוגל מפות"                        # it already has a Google profile
    assert "check-google-profile" not in keys(cfg, SiteCrawler().collect(cfg))


def test_a_platform_that_turns_the_reader_away_costs_the_business_nothing(cfg):
    from bvm.models import Fact
    from bvm.report import score
    cfg.platform = "easy"
    facts = [Fact("site-crawler", "site", cfg.site, {"profile": "easy", "ai_crawlers_blocked": []}),
             Fact("site-crawler", "page", cfg.site + "/page/1", {"linked_from": "", "status": 403})]
    found = TechnicalSeo().analyze(cfg, facts)
    assert [f.key for f in found] == ["no-own-site", "check-google-profile", "check-page-by-hand"]
    assert score(found, None, 1) == 85               # only "no own site" is counted


def test_a_business_without_a_site_gets_a_score(cfg, tmp_path):
    cfg.platform, cfg.start_path = "Wolt", "/about"
    store = Store(tmp_path / "n.sqlite3")
    html = render_report(cfg, run(cfg, store, collectors=[SiteCrawler()]))
    # 100 - closed to AI 15 - no site 15. What we could not read on the page is free.
    assert "<b>70</b>" in html and "מתוך 100" in html and "אפשר לשפר" in html

    # with an AI check, half the score is how often the AI mentions the business
    cfg.buyer_questions = ["q1", "q2"]
    answers = {"q1": Answer("Acme is great."), "q2": Answer("Try others.")}
    ai = AiVisibility([FakeAssistant("fake-ai", answers)])
    assert "<b>60</b>" in render_report(cfg, run(cfg, store, collectors=[SiteCrawler(), ai]))


def test_web_form_takes_a_platform_page(tmp_path):
    seen = []
    client = create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                        collectors=lambda cfg: seen.append(cfg) or []).test_client()
    client.post("/scan", data={"name": "פיצה רומא", "site": "facebook.com/pizza.roma",
                               "city": " חיפה "})
    client.post("/scan", data={"name": "פיצה נאפולי", "site": "facebook.com/pizza.napoli"})
    assert (seen[0].platform, seen[0].start_path, seen[0].city) == ("פייסבוק", "/pizza.roma", "חיפה")
    assert seen[0].slug != seen[1].slug                  # two businesses on one platform
    form = client.get("/check").get_data(as_text=True)
    assert "פייסבוק" in form and 'name="city"' in form


# --- the Google business profile, through Google's own Places API -------------------

class Places(BaseHTTPRequestHandler):
    seen: list[dict] = []
    reply: dict = {}

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Places.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        payload = json.dumps(Places.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def places_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Places)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


FULL = {"displayName": {"text": "Pizza Roma"}, "formattedAddress": "Herzl 1, Haifa",
        "rating": 4.6, "userRatingCount": 212, "nationalPhoneNumber": "04-555-1234",
        "websiteUri": "https://roma.test", "businessStatus": "OPERATIONAL",
        "regularOpeningHours": {"weekdayDescriptions": ["Sun: 10-23"]},
        "photos": [{"name": "p1"}], "googleMapsUri": "https://maps.google.com/?cid=1"}


def test_google_profile_is_looked_up_by_name_and_city(cfg, places_api):
    from bvm.collectors.google_profile import GoogleProfile
    Places.seen.clear()
    Places.reply = {"places": [FULL]}
    cfg.name, cfg.city, cfg.target_country = "Pizza Roma", "Haifa", "IL"
    fact = GoogleProfile("k1", base_url=places_api).collect(cfg)[0]
    sent = Places.seen[0]
    assert sent["path"] == "/v1/places:searchText" and sent["headers"]["X-Goog-Api-Key"] == "k1"
    assert sent["body"] == {"textQuery": "Pizza Roma Haifa", "regionCode": "IL",
                            "languageCode": "he", "pageSize": 5}
    assert "places.userRatingCount" in sent["headers"]["X-Goog-FieldMask"]
    assert (fact.kind, fact.data["found"], fact.data["rating"], fact.data["reviews"]) == (
        "google_profile", True, 4.6, 212)
    assert fact.data["has_hours"] and fact.data["has_phone"] and fact.data["has_photos"]


def test_google_profile_findings(cfg, places_api):
    from bvm.analyzers.google_presence import GooglePresence
    from bvm.collectors.google_profile import GoogleProfile
    cfg.platform, cfg.name = "פייסבוק", "Pizza Roma"

    Places.reply = {"places": [FULL]}                                  # complete: nothing to fix
    facts = GoogleProfile("k", base_url=places_api).collect(cfg)
    assert GooglePresence().analyze(cfg, facts) == []
    assert "check-google-profile" not in keys(cfg, facts)              # it was checked for real

    Places.reply = {"places": [{"displayName": {"text": "Roma"}, "rating": 3.4,
                                "userRatingCount": 4, "businessStatus": "OPERATIONAL"}]}
    found = {f.key: f for f in GooglePresence().analyze(
        cfg, GoogleProfile("k", base_url=places_api).collect(cfg))}
    assert set(found) == {"google-hours", "google-phone", "google-photos", "google-reviews",
                          "google-rating"}
    assert "4" in found["google-reviews"].detail and "3.4" in found["google-rating"].detail

    Places.reply = {}                                                  # Google knows no such place
    found = GooglePresence().analyze(cfg, GoogleProfile("k", base_url=places_api).collect(cfg))
    assert [f.key for f in found] == ["google-missing"] and found[0].impact == 5


def test_google_profile_runs_only_with_a_key_and_counts_in_the_score(cfg, places_api, tmp_path):
    from bvm.collectors.google_profile import GoogleProfile, profile_from_env
    from bvm.pipeline import default_analyzers
    assert profile_from_env({}) is None
    assert profile_from_env({"GOOGLE_PLACES_API_KEY": "k"}).key == "k"

    Places.reply = {}
    cfg.platform, cfg.start_path = "Wolt", "/about"
    result = run(cfg, Store(tmp_path / "n.sqlite3"), analyzers=default_analyzers(),
                 collectors=[SiteCrawler(), GoogleProfile("k", base_url=places_api)])
    found = [f.key for f in result.findings]
    assert found[0] == "google-missing" and "check-google-profile" not in found
    assert "<b>55</b>" in render_report(cfg, result)   # the 70 of the page, less 15 for no profile
