"""The report must not say something false about a site. Each test here is a claim that
was wrong for a real site before it was fixed."""
from __future__ import annotations

import pytest

from bvm.analyzers.technical_seo import TechnicalSeo
from bvm.collectors.site_crawler import PHONE, SiteCrawler, parse_page
from bvm.config import ClientConfig, split_site_address
from bvm.models import Fact, Finding
from bvm.pipeline import RunResult
from bvm.report import breakdown, render_report, score
from tests.test_fixes import Handler, conf, web_site  # noqa: F401 - the fake site's fixtures

SITE = "https://acme.test"
CFG = ClientConfig("Acme", "acme", SITE)


def page(path: str, **data) -> Fact:
    base = {"linked_from": "" if path == "/" else SITE + "/", "status": 200, "is_html": True,
            "title": "T" + path, "description": "d", "h1": ["h"], "meta_robots": "",
            "x_robots_tag": "", "has_structured_data": True, "images_missing_alt": 0,
            "seconds": 0.1, "googlebot_allowed": True, "word_count": 300, "links": [SITE + "/"]}
    return Fact("site-crawler", "page", SITE + path, {**base, **data})


def site_fact(**data) -> Fact:
    return Fact("site-crawler", "site", SITE,
                {"has_robots_txt": True, "has_sitemap": True, "ai_crawlers_blocked": [], **data})


def found(*pages: Fact, **site) -> dict[str, Finding]:
    return {f.key: f for f in TechnicalSeo().analyze(CFG, [site_fact(**site), *pages])}


# --- pictures ---------------------------------------------------------------------

def test_a_decorative_picture_with_an_empty_description_is_not_a_problem():
    html = '<html><body><img src="a" alt=""><img src="b"><img src="c" alt="pizza"></body></html>'
    facts = parse_page(SITE + "/", html)
    assert (facts["images"], facts["images_missing_alt"]) == (3, 1)


# --- links ------------------------------------------------------------------------

def test_only_a_page_that_is_really_gone_counts_as_a_broken_link():
    got = found(page("/"), page("/gone", status=404), page("/old", status=410),
                page("/members", status=403), page("/busy", status=429),
                page("/oops", status=500), page("/slowpoke", status=0, error="ReadTimeout"))
    listed = " ".join(got["broken-links"].evidence)
    assert "/gone" in listed and "/old" in listed
    for not_broken in ("/members", "/busy", "/oops", "/slowpoke"):
        assert not_broken not in listed


# --- what Google may read ---------------------------------------------------------

def test_the_crawler_reads_the_site_rules_as_google_does(conf, web_site):  # noqa: F811
    Handler.robots = "User-agent: *\nDisallow: *\nAllow: /\nAllow: /new\nDisallow: /inside\n"
    pages = {f.subject.removeprefix(web_site): f.data for f in SiteCrawler().collect(conf)
             if f.kind == "page"}
    assert pages["/"]["status"] == 200 and pages["/new"]["title"] == "New"
    assert pages["/inside"]["skipped"] == "robots.txt"
    assert pages["/inside"]["googlebot_allowed"] is False


def test_pages_closed_to_google_are_a_question_not_a_fault():
    got = found(page("/"), page("/cart", googlebot_allowed=False),
                Fact("site-crawler", "page", SITE + "/admin",
                     {"skipped": "robots.txt", "linked_from": SITE + "/", "googlebot_allowed": False}),
                # closed to our scanner only: Google may still read it, so it is not listed
                Fact("site-crawler", "page", SITE + "/bots",
                     {"skipped": "robots.txt", "linked_from": SITE + "/", "googlebot_allowed": True}))
    assert "robots-blocked" not in got
    listed = got["check-robots-blocked"].evidence
    assert listed == [SITE + "/cart", SITE + "/admin"]
    assert score(list(got.values()), None, 1) == 100         # most sites close some pages on purpose


def test_a_hidden_home_page_is_a_fault_and_a_hidden_inner_page_is_a_question():
    got = found(page("/", meta_robots="noindex"), page("/login", meta_robots="noindex, follow"))
    assert got["noindex"].evidence == [SITE + "/"] and got["noindex"].impact == 5
    assert got["check-noindex"].evidence == [SITE + "/login"]

    got = found(page("/"), page("/login", x_robots_tag="noindex"))
    assert "noindex" not in got and "check-noindex" in got
    assert score(list(got.values()), None, 1) == 100


# --- the sitemap ------------------------------------------------------------------

def test_a_page_that_merely_answers_is_not_a_sitemap(conf, web_site):  # noqa: F811
    # /sitemap_index.xml answers 200 with something that is not a sitemap (see the fake site)
    site = next(f for f in SiteCrawler().collect(conf) if f.kind == "site")
    assert site.data["has_sitemap"] is False
    assert "/sitemap_index.xml" in Handler.hits


def test_an_error_page_at_the_rules_address_is_not_rules(conf, web_site):  # noqa: F811
    Handler.robots = "<html><body>Not found, sorry. Disallow nothing.</body></html>"
    site = next(f for f in SiteCrawler().collect(conf) if f.kind == "site")
    assert site.data["has_robots_txt"] is False


# --- pages built in the browser -----------------------------------------------------

def test_an_empty_home_page_is_reported_even_when_other_pages_are_not():
    home = page("/", empty_without_browser=True, rendered=True)
    got = found(home, *[page(f"/p{n}") for n in range(6)])
    assert got["needs-js"].evidence == [SITE + "/"]


# --- phone numbers and addresses ----------------------------------------------------

@pytest.mark.parametrize("text", ["03-555-1234", "050-1234567", "0501234567", "*2700",
                                  "+972 50 123 4567", "+972-3-5551234", "1-700-700-123"])
def test_phone_numbers_as_people_write_them(text):
    assert PHONE.search("התקשרו " + text + " היום")


@pytest.mark.parametrize("text", ["01-01-2024", "2024-01-01", "מחיר 1,250", "מק״ט 00123",
                                  "10:00-23:00", "משנת 1999", "5 כוכבים"])
def test_dates_prices_and_times_are_not_phone_numbers(text):
    assert not PHONE.search(text)


def test_the_address_keeps_what_identifies_the_page():
    assert split_site_address("facebook.com/profile.php?id=1000123") == (
        "https://facebook.com", "/profile.php?id=1000123")
    assert split_site_address("https://x.test/menu#top") == ("https://x.test", "/menu")


# --- the score ----------------------------------------------------------------------

def test_the_report_shows_how_the_score_was_reached():
    findings = list(found(page("/", description="", images_missing_alt=2),
                          ai_crawlers_blocked=["OAI-SearchBot"], has_sitemap=False).values())
    rows = breakdown(findings)
    assert [(key, points) for key, _, points in rows] == [
        ("ai-crawlers-blocked", 15), ("missing-description", 5), ("image-alt", 3)]   # no list of pages: free
    assert 100 - sum(points for _, _, points in rows) == score(findings, None, 1) == 77
    html = render_report(CFG, RunResult(1, [site_fact(), page("/")], findings, []))
    assert "איך חושב הציון" in html and "<b>77</b>" in html
    for _, title, points in rows:
        assert title in html and f"−{points}" in html
