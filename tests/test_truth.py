"""The scanner must not say a business lacks something it has.

Every test here is a false claim found by scanning real sites and comparing each claim
with what a real browser showed.
"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bvm import render
from bvm.analyzers.technical_seo import TechnicalSeo
from bvm.collectors.site_crawler import SiteCrawler, parse_page
from bvm.config import ClientConfig
from bvm.models import Fact
from bvm.report import breakdown
from tests.test_accuracy import CFG, SITE, found, page, site_fact

# --- pictures that need no description ----------------------------------------------

def test_pictures_nobody_sees_are_not_counted_as_missing_a_description():
    html = """<html><body>
      <noscript><img src="pixel.gif"></noscript>
      <template><img src="later.png"></template>
      <img src="track.gif" width="1" height="1">
      <img src="deco.svg" aria-hidden="true">
      <img src="line.png" role="presentation">
      <img src="gone.png" style="display: none">
      <img src="off.png" hidden>
      <img :alt="product.title" :src="product.image">
      <img v-bind:alt="name" src="x.png">
      <img class="placeholder">
      <img src="logo2.png" aria-label="Roma Pizza">
      <img src="search.png" width="18" height="18">
      <span style="display:none"><img src="mic.gif" width="45" height="45"></span>
      <div aria-hidden="true"><img src="shape.png"></div>
      <img class="lazyload" data-src="/img/loader/ring.gif" width="100" height="100">
      <img src="pizza.jpg">
      <img src="logo.png" alt="Roma">
    </body></html>"""
    facts = parse_page(SITE + "/", html)
    assert facts["images_missing_alt"] == 1


# --- business details written in other accepted ways ---------------------------------

@pytest.mark.parametrize("markup, has", [
    ('<div itemscope itemtype="https://schema.org/Restaurant"><span itemprop="name">R</span></div>', True),
    ('<div vocab="https://schema.org/" typeof="Restaurant"><span property="name">R</span></div>', True),
    ('<script type="application/ld+json">{"@type":"Restaurant","name":"R"}</script>', True),
    ('<div itemscope><span>no vocabulary named</span></div>', False),
    ("<p>nothing</p>", False),
])
def test_business_details_are_recognised_in_every_format_search_engines_read(markup, has):
    assert parse_page(SITE + "/", f"<html><body>{markup}</body></html>")["has_structured_data"] is has


# --- headings -------------------------------------------------------------------------

def test_several_main_headings_are_not_a_problem_but_none_is():
    assert "h1" not in found(page("/", h1=["a", "b", "c"]))
    none = found(page("/"), page("/a", h1=[]))["h1"]
    assert "אין כותרת ראשית" in none.detail and none.evidence == [SITE + "/a"]


# --- one page under two addresses ---------------------------------------------------------

def test_a_page_reached_under_two_addresses_is_one_page():
    twice = found(page("/"), page("/about", description="", canonical=SITE + "/about"),
                  page("/about/", description="", canonical=SITE + "/about"), page("/b"))
    gap = twice["missing-description"]
    assert (gap.affected, gap.of, len(gap.evidence)) == (1, 3, 1)


# --- what cannot be seen is not called missing ------------------------------------------

def test_a_list_of_pages_we_did_not_find_is_a_thing_to_check_not_a_fault():
    keys = found(page("/"), has_sitemap=False)
    assert "sitemap" not in keys and "check-sitemap" in keys
    assert breakdown(list(keys.values())) == []
    assert "Search Console" in keys["check-sitemap"].detail + keys["check-sitemap"].how_to_fix


def test_a_gap_we_could_not_confirm_in_a_browser_is_not_reported():
    keys = found(page("/"), page("/a", description="", h1=[], title="", unverified=True),
                 page("/b", description=""))
    assert keys["missing-description"].evidence == [SITE + "/b"]
    assert "h1" not in keys and "missing-title" not in keys


def test_two_titles_that_may_differ_once_scripts_run_are_not_called_the_same():
    keys = found(page("/", title="Shop"), page("/a", title="Shop", title_unverified=True))
    assert "duplicate-title" not in keys
    assert "duplicate-title" in found(page("/", title="Shop"), page("/a", title="Shop"))


# --- a business page on a platform ------------------------------------------------------

def profile(platform: str, **data) -> list:
    cfg = ClientConfig("Roma", "roma", "https://platform.test", start_path="/roma", platform=platform)
    base = {"status": 200, "is_html": True, "linked_from": "", "profile": platform,
            "word_count": 400, "has_hours": True, "has_phone": True, "meta_robots": "",
            "x_robots_tag": "", **data}
    facts = [Fact("site-crawler", "site", cfg.site, {"profile": platform, "ai_crawlers_blocked": []}),
             Fact("site-crawler", "page", cfg.site + "/roma", base)]
    return TechnicalSeo().analyze(cfg, facts)


def test_a_google_maps_page_is_never_said_to_lack_what_we_cannot_read():
    keys = [f.key for f in profile("גוגל מפות", word_count=12, has_hours=False, has_phone=False)]
    assert keys == ["no-own-site"]


def test_hours_and_phone_we_did_not_find_on_a_platform_page_cost_nothing():
    findings = profile("Wolt", has_hours=False, has_phone=False)
    keys = [f.key for f in findings]
    assert "check-profile-hours" in keys and "check-profile-phone" in keys
    assert "profile-hours" not in keys and "profile-phone" not in keys
    assert [key for key, _, _ in breakdown(findings)] == ["no-own-site"]


def test_a_platform_page_we_could_barely_read_is_a_thing_to_look_at_by_hand():
    findings = profile("Wolt", word_count=12, has_hours=False, has_phone=False)
    keys = [f.key for f in findings]
    assert "check-profile-thin" in keys and "profile-thin" not in keys
    assert "check-profile-hours" not in keys and "check-profile-phone" not in keys   # unknown
    assert [key for key, _, _ in breakdown(findings)] == ["no-own-site"]


# --- a real site, in a real browser -------------------------------------------------------

LATE = ('<html><head><title>Shop</title></head><body><p>' + "word " * 80 + '</p>'
        '<a href="/also">also</a><a href="/full">full</a><script>'
        'var m = document.createElement("meta"); m.name = "description"; m.content = "added late";'
        'document.head.appendChild(m);'
        'var h = document.createElement("h1"); h.textContent = "Late heading"; document.body.appendChild(h);'
        'var s = document.createElement("script"); s.type = "application/ld+json";'
        's.textContent = JSON.stringify({"@type": "Store", "name": "Shop"}); document.head.appendChild(s);'
        '</script></body></html>')
ALSO = ('<html><head><title>Shop</title><meta name="description" content="d"></head><body><h1>x</h1>'
        '<p>' + "word " * 80 + '</p><script>document.title = "Another page";</script></body></html>')
FULL = ('<html><head><title>Full</title><meta name="description" content="d">'
        '<script type="application/ld+json">{"@type":"Store","name":"Shop"}</script></head>'
        '<body><h1>Full</h1><p>' + "word " * 80 + '</p><script>var x = 1;</script></body></html>')
BARE = ('<html><head><title>Bare</title></head><body><p>' + "word " * 80 + '</p>'
        '<script>var nothing = 0;</script></body></html>')
HIDDEN = FULL.replace("<title>", '<meta name="robots" content="noindex"><title>')
SITE_PAGES = {"/": LATE, "/also": ALSO, "/full": FULL, "/bare": BARE, "/always-hidden": HIDDEN}


class Handler(BaseHTTPRequestHandler):
    slow_once: set = set()
    hits: list = []

    def do_GET(self):
        import time
        Handler.hits.append(self.path)
        if self.path == "/slow-once" and self.path not in Handler.slow_once:
            Handler.slow_once.add(self.path)
            time.sleep(0.5)
        if self.path == "/always-slow":
            time.sleep(0.5)
        body = SITE_PAGES.get(self.path)
        if self.path == "/sometimes-hidden":
            body = HIDDEN if Handler.hits.count(self.path) == 1 else FULL
        if self.path in ("/slow-once", "/always-slow"):
            body = FULL
        if self.path == "/slow-home":
            body = FULL.replace("<h1>Full</h1>", '<h1>Full</h1><a href="/slow-once">a</a><a href="/always-slow">b</a>')
        status = 200 if body else 404
        payload = (body or "<html>gone</html>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def live():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def crawl(live, start="/", **kwargs):
    cfg = ClientConfig("Shop", "shop", live, start_path=start, max_pages=10, delay_seconds=0,
                       slow_page_seconds=0.3)
    facts = SiteCrawler(**kwargs).collect(cfg)
    pages = {f.subject.removeprefix(live): f.data for f in facts if f.kind == "page"}
    return cfg, facts, pages


needs_browser = pytest.mark.skipif(not render.available(), reason="no browser installed")


@needs_browser
def test_what_scripts_add_to_a_page_is_seen_before_anything_is_called_missing(live):
    cfg, facts, pages = crawl(live, render=True)
    home = pages["/"]
    assert home["description"] == "added late" and home["h1"] == ["Late heading"]
    assert home["has_structured_data"] and home["checked_in_browser"]
    assert pages["/also"]["title"] == "Another page"          # not the same title as the home page
    keys = {f.key for f in TechnicalSeo().analyze(cfg, facts)}
    assert not keys & {"missing-description", "h1", "structured-data", "duplicate-title"}


@needs_browser
def test_a_page_with_nothing_to_confirm_never_starts_a_browser(live, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a browser was started for a complete page")
    monkeypatch.setattr(render, "Browser", boom)
    cfg, facts, pages = crawl(live, start="/full", render=True)
    assert pages["/full"]["h1"] == ["Full"]


@needs_browser
def test_a_gap_that_is_real_is_still_reported_after_the_browser_looked(live):
    cfg, facts, pages = crawl(live, start="/bare", render=True)
    keys = {f.key for f in TechnicalSeo().analyze(cfg, facts)}
    assert pages["/bare"]["checked_in_browser"] and {"missing-description", "h1"} <= keys


@needs_browser
def test_when_the_browser_budget_runs_out_the_rest_is_not_guessed(live, monkeypatch):
    import bvm.collectors.site_crawler as crawler
    monkeypatch.setattr(crawler, "MAX_RENDERED", 0)
    cfg, facts, pages = crawl(live, start="/bare", render=True)
    assert pages["/bare"]["unverified"] is True
    assert "missing-description" not in {f.key for f in TechnicalSeo().analyze(cfg, facts)}


def test_without_any_browser_the_page_is_judged_as_it_arrives(live):
    cfg, facts, pages = crawl(live, start="/bare", render=False)
    assert "unverified" not in pages["/bare"]
    assert "missing-description" in {f.key for f in TechnicalSeo().analyze(cfg, facts)}


# --- one slow answer is not a slow page ---------------------------------------------------

def test_a_page_is_slow_only_if_it_is_slow_twice(live):
    Handler.slow_once.clear()
    cfg, facts, pages = crawl(live, start="/slow-home")
    assert pages["/slow-once"]["seconds"] < 0.3 <= pages["/always-slow"]["seconds"]
    slow = {f.key: f for f in TechnicalSeo().analyze(cfg, facts)}["slow-pages"]
    assert len(slow.evidence) == 1 and "/always-slow" in slow.evidence[0]


# --- the gravest claim is made only when every answer agrees ----------------------------

def test_a_home_page_marked_hidden_only_now_and_then_is_not_called_hidden(live):
    Handler.hits.clear()
    cfg, facts, pages = crawl(live, start="/sometimes-hidden")
    keys = {f.key for f in TechnicalSeo().analyze(cfg, facts)}
    assert pages["/sometimes-hidden"]["noindex_unsure"] and "noindex" not in keys
    assert "check-noindex" in keys                      # still shown, to be looked at
    cfg, facts, pages = crawl(live, start="/always-hidden")
    assert "noindex" in {f.key for f in TechnicalSeo().analyze(cfg, facts)}


# --- a browser that was turned away proves nothing ----------------------------------------

def test_a_blocked_screen_shown_to_the_browser_does_not_replace_the_page(monkeypatch):
    crawler = SiteCrawler(render=True)
    monkeypatch.setattr(render, "available", lambda: True)
    monkeypatch.setattr(crawler, "_render", lambda url: (
        "<html><head><title>Access denied</title><meta name='robots' content='noindex'></head>"
        "<body><h1>blocked</h1></body></html>"))
    data = {"title": "Pizza Roma", "description": "", "h1": [], "word_count": 600,
            "has_structured_data": False, "images": 9, "images_missing_alt": 0}
    crawler._confirm(SITE + "/", data)
    assert data["title"] == "Pizza Roma" and data["h1"] == [] and data["unverified"] is True
    assert "checked_in_browser" not in data


def test_the_browser_has_a_time_budget_for_one_scan(monkeypatch):
    import bvm.collectors.site_crawler as module
    crawler = SiteCrawler(render=True)
    monkeypatch.setattr(render, "available", lambda: True)
    crawler._render_seconds = module.RENDER_SECONDS
    assert crawler._render(SITE + "/") is None


def test_closing_a_site_to_ai_training_is_not_called_hiding_from_ai():
    """A site that keeps its text out of model training can still be read for answers, and
    many owners choose exactly that. It is a thing to look at, and costs no points."""
    from bvm.scoring import breakdown
    from tests.test_accuracy import found as findings, page as a_page

    def found(blocked):
        return findings(a_page("/"), ai_crawlers_blocked=blocked)

    training = found(["GPTBot", "ClaudeBot", "Google-Extended"])
    assert "ai-crawlers-blocked" not in training
    assert training["check-ai-training-blocked"].evidence == ["GPTBot", "ClaudeBot", "Google-Extended"]
    assert not breakdown(list(training.values()))                 # nothing lost for it
    both = found(["OAI-SearchBot", "GPTBot"])
    assert "check-ai-training-blocked" not in both
    assert dict((key, lost) for key, _, lost in breakdown(list(both.values())))["ai-crawlers-blocked"] == 15
    # A check saved before the two kinds were told apart named PerplexityBot, which reads for answers.
    assert "ai-crawlers-blocked" in found(["PerplexityBot"])
    assert not {"ai-crawlers-blocked", "check-ai-training-blocked"} & set(found([]))
