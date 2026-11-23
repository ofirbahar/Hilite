"""Regression tests: redirects, page size, duplicate addresses, sitemaps, Hebrew, stuck scans."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bvm.analyzers.technical_seo import TechnicalSeo
from bvm.collectors import site_crawler
from bvm.collectors.ai_visibility import AiVisibility, first_mention
from bvm.collectors.site_crawler import MAX_BYTES, SiteCrawler, _clean, _same_host
from bvm.config import ClientConfig, ConfigError
from bvm.store import Store
from bvm.web import create_app
from tests.test_ai_and_web import ANSWERS, QUESTIONS, FakeAssistant

PAGE = ('<html><head><title>{t}</title><meta name="description" content="d">'
        '<script type="application/ld+json">{{}}</script></head>'
        '<body><h1>{t}</h1>{links}</body></html>')
HOME = PAGE.format(t="Home", links='<a href="/new">a</a> <a href="/old">b</a> '
                   '<a href="/new?utm_source=mail">c</a> <a href="/new#part">d</a> '
                   '<a href="/file.bin">e</a> <a href="/jump">f</a>')
HEBREW = ('<html><head><meta charset="utf-8"><title>שלום</title></head>'
          '<body><h1>שלום</h1></body></html>')


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []
    busy = 0
    robots = "User-agent: *\nAllow: /\n"

    def do_GET(self):
        Handler.hits.append(self.path)
        if self.path == "/busy" and Handler.busy:
            Handler.busy -= 1
            self.send_response(429)
            self.send_header("Retry-After", "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        other_name = f"http://localhost:{self.server.server_port}"   # same server, another host name
        redirects = {"/old": "/new", "/jump": "/inside", "/loop": "/loop", "/moved": other_name + "/new"}
        if self.path in redirects:
            self.send_response(302)
            self.send_header("Location", redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        pages = {
            "/": ("text/html", HOME.encode()),
            "/sitemap_index.xml": ("application/octet-stream", b"this is not a sitemap"),
            "/busy": ("text/html", PAGE.format(t="Busy", links="").encode()),
            "/new": ("text/html", PAGE.format(t="New", links="").encode()),
            "/inside": ("text/html", PAGE.format(t="Inside", links="").encode()),
            "/hebrew": ("text/html", HEBREW.encode("utf-8")),   # no charset in the header
            "/app": ("text/html", b"<html><head><title>App</title>" + b"<script></script>" * 4
                     + b'</head><body><div id="root"></div></body></html>'),
            "/huge": ("text/html", b"<html>" + b"x" * (MAX_BYTES + 500_000)),
            "/heavy": ("text/html", b"<html><head><script>" + b"x" * 3_000_000
                       + b'</script></head><body><h1>Deep</h1><a href="/new">n</a></body></html>'),
            "/file.bin": ("application/octet-stream", b"\0" * 100_000),
            "/robots.txt": ("text/plain", Handler.robots.encode()),
            "/map.xml": ("application/xml", b"<urlset/>"),
        }
        ctype, payload = pages.get(self.path, ("text/html", b"<html>gone</html>"))
        self.send_response(200 if self.path in pages else 404)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except OSError:
            pass  # the crawler stopped reading on purpose

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def web_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def conf(web_site):
    Handler.hits.clear()
    Handler.robots = "User-agent: *\nAllow: /\n"
    return ClientConfig("Acme", "acme", web_site, max_pages=20, delay_seconds=0)


# --- one page, one address ----------------------------------------------------

def test_same_page_under_several_addresses_is_read_once(conf, web_site):
    facts = SiteCrawler().collect(conf)
    titled = [f.subject for f in facts if f.kind == "page" and f.data.get("title") == "New"]
    assert titled == [web_site + "/new"]
    old = next(f for f in facts if f.subject == web_site + "/old")
    assert old.data["redirected_to"] == web_site + "/new" and old.data["is_html"] is False
    keys = {f.key for f in TechnicalSeo().analyze(conf, facts)}
    assert "duplicate-title" not in keys and "broken-links" not in keys


def test_addresses_are_normalised():
    assert _clean("HTTP://Example.com") == "http://example.com/"
    assert _clean("https://a.test/p?utm_source=x&id=3&fbclid=9#top") == "https://a.test/p?id=3"
    assert _same_host("https://www.acme.test/x", "https://acme.test")
    assert not _same_host("https://evil.test/x", "https://acme.test")


# --- size and encoding --------------------------------------------------------

def test_large_pages_are_cut_off_and_files_are_not_downloaded(conf, web_site):
    crawler = SiteCrawler()
    assert len(crawler._get(web_site + "/huge")[0].body) == MAX_BYTES
    binary = crawler._get(web_site + "/file.bin")[0]
    assert binary.status == 200 and binary.body == b""


def test_hebrew_page_without_a_charset_header_is_read_correctly(conf, web_site):
    page = SiteCrawler()._get(web_site + "/hebrew")[0]
    assert site_crawler.parse_page(page.url, page.body, page.charset)["title"] == "שלום"


# --- redirects cannot lead inside the network ---------------------------------

def test_public_only_refuses_private_addresses_and_redirects_to_them(conf, web_site, monkeypatch):
    assert SiteCrawler(public_only=True)._get(web_site + "/")[2] == "BlockedAddress"
    assert Handler.hits == []                      # refused before any request

    monkeypatch.setattr(site_crawler, "is_public_address", lambda url: "/inside" not in url)
    fetched, _, error = SiteCrawler(public_only=True)._get(web_site + "/jump")
    assert fetched is None and error == "BlockedAddress"
    assert "/jump" in Handler.hits and "/inside" not in Handler.hits


def test_redirect_loops_end(conf, web_site):
    assert SiteCrawler()._get(web_site + "/loop")[2] == "TooManyRedirects"


# --- sitemap ------------------------------------------------------------------

def test_sitemap_named_in_robots_counts(conf, web_site):
    site = lambda: next(f for f in SiteCrawler().collect(conf) if f.kind == "site").data
    assert site()["has_sitemap"] is False          # /sitemap.xml answers 404 here
    Handler.robots = f"User-agent: *\nAllow: /\nSitemap: {web_site}/map.xml\n"
    assert site()["has_sitemap"] is True


# --- Hebrew names -------------------------------------------------------------

def test_names_with_hebrew_prefixes_are_found():
    assert first_mention("כדאי לקנות באקמה", ["אקמה"]) == 12
    assert first_mention("מומלץ לפנות ל-Acme או לגלובקס", ["Acme"]) == 14
    assert first_mention("וכשמאקמה חוזרים", ["אקמה"]) is None     # four letters: another word
    assert first_mention("Acmeology", ["Acme"]) is None


# --- web interface ------------------------------------------------------------

def test_web_form_asks_as_a_buyer_in_israel(conf, web_site, tmp_path):
    assistant = FakeAssistant("fake-ai", ANSWERS)
    client = create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                        collectors=lambda cfg: [AiVisibility([assistant])]).test_client()
    client.post("/scan", data={"name": "Acme", "site": web_site,
                               "questions": "\n".join(QUESTIONS)})
    assert {country for _, country in assistant.asked} == {"IL"}


def test_scan_cut_off_by_a_restart_is_marked_failed(tmp_path):
    db = str(tmp_path / "w.sqlite3")
    store = Store(db)
    run_id = store.start_run("acme", ClientConfig("Acme", "acme", "https://acme.test").to_dict())
    store.db.execute("UPDATE runs SET started_at = '2020-01-01T00:00:00+00:00'")
    store.db.commit()
    client = create_app(db).test_client()
    page = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert 'http-equiv="refresh"' not in page and "נקטעה" in page
    assert store.get_run(run_id)["status"] == "failed"


# --- sites that used to fail a real scan ---------------------------------------

def test_address_without_a_scheme_is_accepted_and_keeps_its_path():
    from bvm.config import split_site_address
    assert split_site_address("ynet.co.il") == ("https://ynet.co.il", "/")
    assert split_site_address(" www.ikea.com/il/he/ ") == ("https://www.ikea.com", "/il/he/")
    assert split_site_address("http://a.test:8080/shop?x=1") == ("http://a.test:8080", "/shop?x=1")
    for bad in ("", "not a url", "ftp://a.test", "https://"):
        with pytest.raises(ConfigError):
            split_site_address(bad)


def test_default_port_and_www_do_not_make_another_site():
    assert _same_host("https://www.walla.co.il:443/", "https://walla.co.il")
    assert _same_host("http://a.test:80/x", "http://a.test")
    assert not _same_host("http://a.test:8080/x", "http://a.test")


def test_scan_starts_on_the_page_the_owner_gave(conf, web_site):
    conf.start_path = "/new"
    pages = [f for f in SiteCrawler().collect(conf) if f.kind == "page"]
    assert pages[0].subject == web_site + "/new" and pages[0].data["title"] == "New"


def test_redirects_do_not_use_up_the_page_budget(conf):
    conf.max_pages = 4                # home, a file, /new and /inside; two redirects on the way
    pages = [f for f in SiteCrawler().collect(conf) if f.kind == "page"]
    read = [p for p in pages if p.data.get("status") == 200 and p.data.get("is_html")]
    assert len(read) == 3


def test_a_heavy_page_is_still_read_to_its_content(conf, web_site):
    page = SiteCrawler()._get(web_site + "/heavy")[0]
    facts = site_crawler.parse_page(page.url, page.body, page.charset)
    assert facts["h1"] == ["Deep"] and web_site + "/new" in facts["links"]


class Blocked(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(403)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", "9")
        self.end_headers()
        self.wfile.write(b"Forbidden")

    def log_message(self, *args):
        pass


def test_a_site_that_blocks_the_scan_says_so_and_gets_no_score(tmp_path):
    from bvm.pipeline import run
    from bvm.report import render_report
    server = ThreadingHTTPServer(("127.0.0.1", 0), Blocked)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        cfg = ClientConfig("Acme", "acme", f"http://127.0.0.1:{server.server_port}", delay_seconds=0)
        result = run(cfg, Store(tmp_path / "n.sqlite3"), collectors=[SiteCrawler()])
    finally:
        server.shutdown()
    keys = [f.key for f in result.findings]
    assert keys == ["site-blocked"]                 # not "one broken link" and a high score
    html = render_report(cfg, result)
    assert "לא נבדק" in html and result.findings[0].title in html


def test_a_site_that_does_not_answer_says_so():
    cfg = ClientConfig("Acme", "acme", "http://127.0.0.1:9", delay_seconds=0)
    findings = TechnicalSeo().analyze(cfg, SiteCrawler().collect(cfg))
    assert [f.key for f in findings] == ["site-unreachable"]


def test_web_form_accepts_an_address_without_https(tmp_path):
    seen = []
    client = create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                        collectors=lambda cfg: seen.append(cfg) or []).test_client()
    posted = client.post("/scan", data={"name": "Acme", "site": "127.0.0.1:9/menu"})
    assert posted.status_code == 302
    assert (seen[0].site, seen[0].start_path) == ("https://127.0.0.1:9", "/menu")
    assert 'type="url"' not in client.get("/check").get_data(as_text=True)


# --- found by scanning a wider batch of real sites --------------------------------

def test_a_site_that_moved_to_its_own_subdomain_is_followed(conf, web_site, monkeypatch):
    assert site_crawler._related("https://shop.super-pharm.co.il/", "https://super-pharm.co.il")
    assert not site_crawler._related("https://validate.perfdrive.com/?x", "https://mako.co.il")
    monkeypatch.setattr(site_crawler, "_related", lambda a, b: True)
    conf.start_path = "/moved"
    facts = SiteCrawler().collect(conf)
    new_home = web_site.replace("127.0.0.1", "localhost")
    assert facts[0].subject == new_home                       # the site is now the new host
    assert [f.subject for f in facts if f.data.get("title") == "New"] == [new_home + "/new"]


def test_a_redirect_to_an_unrelated_host_is_reported_not_followed(conf, web_site):
    conf.start_path = "/moved"                                # 127.0.0.1 -> localhost: unrelated names
    findings = TechnicalSeo().analyze(conf, SiteCrawler().collect(conf))
    assert [f.key for f in findings] == ["site-moved"]
    assert "localhost" in findings[0].evidence[0]


def test_pages_that_are_empty_until_a_browser_runs_them_are_reported(conf):
    conf.start_path = "/app"
    keys = {f.key for f in TechnicalSeo().analyze(conf, SiteCrawler().collect(conf))}
    assert "needs-js" in keys and "h1" not in keys and "missing-description" not in keys


def test_a_site_that_asks_for_a_pause_gets_one_and_is_then_read(conf, web_site):
    Handler.busy = 1
    assert SiteCrawler()._get(web_site + "/busy")[0].status == 200
    Handler.busy = 5                                 # it keeps refusing: stop after one retry
    assert SiteCrawler()._get(web_site + "/busy")[0].status == 429
    Handler.busy = 0


def test_a_redirect_to_a_bot_check_page_is_a_block_not_a_move():
    from bvm.models import Fact
    cfg = ClientConfig("Acme", "acme", "https://acme.test")
    wall = "https://validate.guard.test/?ssc=https%3A%2F%2Facme.test%2F&token=" + "x" * 300
    facts = [Fact("site-crawler", "page", "https://acme.test/",
                  {"linked_from": "", "status": 301, "is_html": False, "redirected_to": wall})]
    found = TechnicalSeo().analyze(cfg, facts)
    assert [f.key for f in found] == ["site-blocked"]
    assert found[0].evidence == ["https://validate.guard.test"]     # no long token in the report
