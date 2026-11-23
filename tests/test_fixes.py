"""Regression tests: redirects, page size, duplicate addresses, sitemaps, Hebrew, stuck scans."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bvm.analyzers.technical_seo import TechnicalSeo
from bvm.collectors import site_crawler
from bvm.collectors.ai_visibility import AiVisibility, first_mention
from bvm.collectors.site_crawler import MAX_BYTES, SiteCrawler, _clean, _same_host
from bvm.config import ClientConfig
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
    robots = "User-agent: *\nAllow: /\n"

    def do_GET(self):
        Handler.hits.append(self.path)
        redirects = {"/old": "/new", "/jump": "/inside", "/loop": "/loop"}
        if self.path in redirects:
            self.send_response(302)
            self.send_header("Location", redirects[self.path])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        pages = {
            "/": ("text/html", HOME.encode()),
            "/new": ("text/html", PAGE.format(t="New", links="").encode()),
            "/inside": ("text/html", PAGE.format(t="Inside", links="").encode()),
            "/hebrew": ("text/html", HEBREW.encode("utf-8")),   # no charset in the header
            "/huge": ("text/html", b"<html>" + b"x" * (MAX_BYTES + 500_000)),
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
