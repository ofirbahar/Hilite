"""Sites whose pages are built in the browser: the scan opens them in a real browser."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytest.importorskip("playwright.sync_api")

from bvm import render  # noqa: E402
from bvm.analyzers.technical_seo import TechnicalSeo  # noqa: E402
from bvm.collectors.site_crawler import USER_AGENT, SiteCrawler  # noqa: E402
from bvm.config import ClientConfig  # noqa: E402

SCRIPTS = "<script></script>" * 3


def app(title: str, body_js: str) -> bytes:
    """A page that is empty until its script runs."""
    return (f'<html lang="he"><head><title>{title}</title><meta name="description" content="d">{SCRIPTS}'
            f'</head><body><div id="root"></div><script>document.getElementById("root").innerHTML = '
            f'{body_js};</script></body></html>').encode()


WORDS = '"<p>" + "מילה ".repeat(60) + "</p>"'
PAGES = {
    "/": app("Shop", f'"<h1>חנות</h1><a href=\\"/item\\">פריט</a><a href=\\"/secret\\">x</a>" + {WORDS}'),
    "/item": app("Item", f'"<h1>פריט</h1><a href=\\"/\\">בית</a>" + {WORDS}'),
    "/plain": b'<html><head><title>Plain</title></head><body><h1>Plain</h1><a href="/">h</a></body></html>',
}


class Handler(BaseHTTPRequestHandler):
    agents: dict[str, set] = {}
    hits: list[str] = []

    def do_GET(self):
        Handler.hits.append(self.path)
        Handler.agents.setdefault(self.path, set()).add(self.headers.get("User-Agent", ""))
        if self.path == "/robots.txt":
            status, ctype, payload = 200, "text/plain", b"User-agent: *\nDisallow: /secret\n"
        elif self.path in PAGES:
            status, ctype, payload = 200, "text/html; charset=utf-8", PAGES[self.path]
        else:
            status, ctype, payload = 404, "text/html", b"<html>gone</html>"
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def app_site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def conf(app_site):
    Handler.hits.clear()
    Handler.agents.clear()
    return ClientConfig("Shop", "shop", app_site, max_pages=10, delay_seconds=0)


def test_a_browser_built_site_is_read_in_full(conf, app_site):
    facts = SiteCrawler(render=True).collect(conf)
    pages = {f.subject.removeprefix(app_site): f.data for f in facts if f.kind == "page"}
    home = pages["/"]
    assert home["h1"] == ["חנות"] and home["word_count"] >= 60
    assert home["empty_without_browser"] is True
    assert pages["/item"]["h1"] == ["פריט"]            # found through a link only the browser sees
    assert pages["/secret"]["skipped"] == "robots.txt"
    assert "/secret" not in Handler.hits                # the site's rules hold for the browser too
    assert all(agent == USER_AGENT for agent in Handler.agents["/"])   # it never hides who it is


def test_the_report_still_says_crawlers_see_nothing_but_checks_the_real_content(conf):
    findings = {f.key: f for f in TechnicalSeo().analyze(conf, SiteCrawler(render=True).collect(conf))}
    assert "needs-js" in findings                       # true for AI crawlers, whatever we can read
    assert "h1" not in findings and "missing-title" not in findings


def test_ordinary_pages_never_start_a_browser(conf, app_site, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("a browser was started for a page that did not need one")
    monkeypatch.setattr(render, "Browser", boom)
    conf.start_path = "/plain"
    conf.max_pages = 1
    page = next(f for f in SiteCrawler(render=True).collect(conf) if f.kind == "page")
    assert page.data["title"] == "Plain" and "empty_without_browser" not in page.data


def test_without_a_browser_the_scan_still_finishes(conf, monkeypatch):
    monkeypatch.setattr(render, "available", lambda: False)
    findings = {f.key for f in TechnicalSeo().analyze(conf, SiteCrawler(render=True).collect(conf))}
    assert "needs-js" in findings


def test_public_only_browser_refuses_private_addresses(conf, app_site):
    with render.Browser(USER_AGENT, public_only=True) as browser:
        assert browser.html(app_site + "/") is None      # 127.0.0.1 is not the public internet
    assert Handler.hits == []
