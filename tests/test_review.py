"""Bugs found by reading the code and by an independent review. Each test is one of them."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import pytest

from bvm.ai_providers import Answer
from bvm.analyzers.ai_gap import AiGap
from bvm.analyzers.technical_seo import TechnicalSeo
from bvm.collectors.ai_visibility import AiVisibility
from bvm.collectors.site_crawler import PHONE, SiteCrawler, _clean, parse_page
from bvm.config import ClientConfig
from bvm.models import Fact
from bvm.pipeline import RunResult, run
from bvm.report import render_report, score
from bvm.robots import Rules
from bvm.store import Store
from bvm.web import create_app
from tests.test_accuracy import CFG, SITE, found, page, site_fact
from tests.test_ai_and_web import FakeAssistant

HEB = "/מוצרים"                                   # a Hebrew path, as Israeli sites have
PAGE = "<html><head><title>{t}</title></head><body><h1>{t}</h1>{body}</body></html>"


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []
    robots = "User-agent: *\nDisallow: /secret\n"
    busy = 0

    def _send(self, status, ctype, payload, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path
        Handler.hits.append(path)
        if path == "/robots.txt":
            return self._send(200, "text/plain", Handler.robots.encode("utf-8"))
        if path == "/go":
            return self._send(302, "text/html", b"", [("Location", "/secret")])
        if path == "/he":                           # Location sent as raw UTF-8, as servers do
            self.send_response(301)
            self.flush_headers()
            self.wfile.write(b"Location: " + HEB.encode("utf-8") + b"\r\nContent-Length: 0\r\n\r\n")
            return None
        if path == "/patient" and Handler.busy:
            Handler.busy -= 1
            return self._send(429, "text/html", b"", [("Retry-After", "1")])
        pages = {
            "/": PAGE.format(t="Home", body='<a href="/go">a</a> <a href="/he">b</a> '
                                            '<a href="http://[broken">c</a> <a href="/ok">d</a>'),
            "/ok": PAGE.format(t="Ok", body=""),
            "/patient": PAGE.format(t="Patient", body=""),
            "/secret": PAGE.format(t="Secret", body=""),
            quote(HEB): PAGE.format(t="Products", body=""),
        }
        if path in pages:
            return self._send(200, "text/html; charset=utf-8", pages[path].encode("utf-8"))
        return self._send(404, "text/html", b"<html>gone</html>")

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def host():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def cfg(host):
    Handler.hits.clear()
    Handler.robots, Handler.busy = "User-agent: *\nDisallow: /secret\n", 0
    return ClientConfig("Acme", "acme", host, max_pages=20, delay_seconds=0)


def crawl(cfg, host):
    return {f.subject.removeprefix(host): f.data for f in SiteCrawler().collect(cfg) if f.kind == "page"}


# --- the crawl ----------------------------------------------------------------------

def test_one_malformed_link_does_not_lose_the_whole_scan(cfg, host):
    pages = crawl(cfg, host)
    assert pages["/"]["title"] == "Home" and pages["/ok"]["title"] == "Ok"


def test_a_closed_page_is_not_fetched_through_a_redirect_either(cfg, host):
    pages = crawl(cfg, host)
    assert "/secret" not in Handler.hits
    assert pages["/secret"]["skipped"] == "robots.txt"


def test_a_redirect_to_a_hebrew_address_is_followed_not_reported_broken(cfg, host):
    pages = crawl(cfg, host)
    assert pages[quote(HEB)]["title"] == "Products"
    assert not [p for p in pages.values() if p.get("status") == 404]


def test_one_page_under_two_spellings_is_one_page():
    assert _clean("https://x.test/%d7%9e") == _clean("https://x.test/%D7%9E") == _clean("https://x.test/מ")


def test_rules_file_with_a_byte_order_mark_is_still_obeyed(cfg, host):
    Handler.robots = "﻿User-agent: *\nDisallow: /\n"
    pages = crawl(cfg, host)
    assert pages["/"]["skipped"] == "robots.txt" and "/" not in Handler.hits


def test_our_own_wait_is_not_the_pages_load_time(cfg, host):
    Handler.busy = 1
    fetched, seconds, _ = SiteCrawler()._get(host + "/patient")
    assert fetched.status == 200 and seconds < 0.9      # the one-second pause is ours


def test_the_page_title_is_the_one_in_the_head():
    html = '<html><head></head><body><svg><title>Logo</title></svg><h1>x</h1></body></html>'
    assert parse_page(SITE + "/", html)["title"] == ""


@pytest.mark.parametrize("text", ["(03) 555-1234", "054.555.1234", "052 555 12 34",
                                  "+972 (0)3 555 1234"])
def test_more_ways_people_write_a_phone_number(text):
    assert PHONE.search("טלפון: " + text)


# --- reading robots.txt -------------------------------------------------------------

def test_robots_edge_cases():
    blank = Rules("User-agent:\nAllow: /\n\nUser-agent: *\nDisallow: /\n")
    assert not blank.can_fetch("AnyBot", "https://x.test/")        # the nameless group is nobody's
    hebrew = Rules("User-agent: *\nDisallow: " + HEB + "\n")
    assert not hebrew.can_fetch("b", "https://x.test" + quote(HEB) + "/1")
    assert not hebrew.can_fetch("b", "https://x.test" + HEB)


# --- findings -----------------------------------------------------------------------

def test_a_home_page_closed_to_google_is_a_fault_and_caps_the_score():
    got = found(page("/", googlebot_allowed=False), page("/a", googlebot_allowed=False))
    assert got["home-closed-to-google"].impact == 5
    assert got["check-robots-blocked"].evidence == [SITE + "/a"]
    assert score(list(got.values()), None, 2) == 49               # invisible on Google is never "good"

    hidden = found(page("/", meta_robots="noindex"))
    assert score(list(hidden.values()), None, 1) == 49


def test_a_rule_for_another_crawler_does_not_hide_the_page_from_google():
    assert "noindex" not in found(page("/", x_robots_tag="otherbot: noindex"))
    assert "noindex" in found(page("/", x_robots_tag="googlebot: noindex, nofollow"))
    assert "noindex" in found(page("/", x_robots_tag="noindex"))


def test_the_same_page_with_a_sort_order_is_not_a_duplicate_title():
    got = found(page("/"), page("/list", title="List"),
                page("/list?sort=a", title="List", canonical=SITE + "/list"))
    assert "duplicate-title" not in got
    got = found(page("/"), page("/a", title="Same"), page("/b", title="Same"))
    assert "duplicate-title" in got


def test_a_small_page_that_is_small_in_the_browser_too_is_not_a_shell():
    small = page("/", scripts=4, word_count=12, links=[], empty_without_browser=True,
                 rendered=True, small_page=True)
    assert "needs-js" not in found(small)


# --- the score ----------------------------------------------------------------------

def test_a_scan_that_was_cut_off_shows_no_score(tmp_path):
    facts = [site_fact(), page("/")]
    for part in ("pipeline", "site-crawler", "technical-seo"):
        html = render_report(CFG, RunResult(1, facts, [], [(part, "RuntimeError: boom")]))
        assert "<b>?</b>" in html and "לא נמצאו בעיות" not in html, part
    other = render_report(CFG, RunResult(1, facts, [], [("ai-visibility", "x")]))
    assert "<b>100</b>" in other                                   # the site part did finish


def test_a_site_that_could_not_be_read_has_no_site_score_whatever_else_ran():
    facts = [site_fact(), Fact("site-crawler", "page", SITE + "/", {"linked_from": "", "status": 403}),
             Fact("google-profile", "google_profile", "Acme", {"found": False})]
    findings = TechnicalSeo().analyze(CFG, facts)
    assert [f.key for f in findings] == ["site-blocked"]
    assert "<b>?</b>" in render_report(CFG, RunResult(1, facts, findings, []))


# --- the AI check -------------------------------------------------------------------

def test_cited_sites_are_clean_and_own_site_is_recognised(cfg):
    cfg.buyer_questions, cfg.site = ["q"], "https://acme.test"
    answer = Answer("See https://example.com. or **https://bar.com** and https://acme.test, ok")
    data = AiVisibility([FakeAssistant("ai", {"q": answer})]).collect(cfg)[0].data
    assert data["cited_domains"] == ["acme.test", "bar.com", "example.com"]


def test_a_partly_failed_ai_check_says_so_in_the_report(cfg, tmp_path):
    cfg.buyer_questions = ["q1", "q2"]
    answers = {"q1": Answer("Acme."), "q2": TimeoutError("slow")}
    result = run(cfg, Store(tmp_path / "n.sqlite3"),
                 collectors=[AiVisibility([FakeAssistant("fake-ai", answers)])])
    html = render_report(cfg, result)
    assert "fake-ai" in html and "לא ענה" in html


def test_ai_gap_counts_answers_as_answers(cfg):
    cfg.buyer_questions, cfg.competitors = ["q1"], ["Globex"]
    answers = {"q1": Answer("Use Globex.")}
    facts = AiVisibility([FakeAssistant("a", answers), FakeAssistant("b", answers)]).collect(cfg)
    gap = {f.key: f for f in AiGap().analyze(cfg, facts)}["ai-competitor-gap"]
    assert "2 תשובות" in gap.detail and "שאלות" not in gap.detail


# --- the web interface --------------------------------------------------------------

def test_another_site_cannot_start_a_scan_and_scans_are_limited(tmp_path):
    client = create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                        collectors=lambda cfg: []).test_client()
    data = {"name": "Acme", "site": "example.com"}
    assert client.post("/scan", data=data, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/scan", data=data, headers={"Origin": "http://localhost"}).status_code == 302
    assert client.post("/scan", data=data).status_code == 302          # no Origin: same browser tab

    release = threading.Event()

    def slow(cfg):
        release.wait(5)
        return []
    busy = create_app(str(tmp_path / "b.sqlite3"), allow_private=True, collectors=slow).test_client()
    codes = [busy.post("/scan", data=data).status_code for _ in range(4)]
    release.set()
    assert codes == [302, 302, 302, 429]


def test_the_home_page_is_still_the_home_page_when_reached_by_redirect():
    moved = Fact("site-crawler", "page", SITE + "/",
                 {"linked_from": "", "status": 301, "is_html": False, "redirected_to": SITE + "/he"})
    home = dict(linked_from=SITE + "/")                       # as the crawler records it
    assert "noindex" in found(moved, page("/he", meta_robots="noindex", **home))
    assert "home-closed-to-google" in found(moved, page("/he", googlebot_allowed=False, **home))
    shell = page("/he", empty_without_browser=True, rendered=True, **home)
    assert "needs-js" in found(moved, shell, *[page(f"/p{n}") for n in range(6)])
