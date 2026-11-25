"""The checks that look past "can the site be read" to what holds it back: how it looks on
a phone, how fast it is there by Google's own measure, what it tells engines about the
business, and whether it says anything about what buyers ask."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bvm.analyzers.buyer_questions import BuyerQuestions
from bvm.analyzers.page_experience import PageExperience
from bvm.collectors.page_speed import PageSpeed, page_speed_from_env
from bvm.collectors.site_crawler import SiteCrawler, parse_page
from bvm.config import ClientConfig
from bvm.models import Fact
from bvm.pipeline import RunResult
from bvm.questions import cores, unanswered
from bvm.report import breakdown, render_report
from bvm.scoring import parts
from tests.test_accuracy import CFG, SITE, found, page

LD = ('<script type="application/ld+json">{"@context":"https://schema.org","@type":"Restaurant",'
      '"name":"Roma"%s}</script>')


# --- what a page records ------------------------------------------------------------------

def test_a_page_records_its_phone_setting_and_what_it_tells_engines():
    plain = parse_page(SITE, "<html><head><title>T</title></head><body>hi</body></html>")
    assert plain["viewport"] is False and plain["business"] == {}       # no details at all
    full = parse_page(SITE, '<html><head><meta name="viewport" content="width=device-width">'
                      + LD % ',"telephone":"03-5551234","address":{"@type":"PostalAddress"},'
                             '"openingHours":"Mo-Su 10:00-22:00"' + "</head><body>x</body></html>")
    assert full["viewport"] is True
    assert full["business"] == {"phone": True, "address": True, "hours": True}
    bare = parse_page(SITE, "<html><head>" + LD % "" + "</head><body>x</body></html>")
    assert bare["business"] == {"phone": False, "address": False, "hours": False}
    # details about the page itself are not details about the business
    crumbs = parse_page(SITE, '<html><head><script type="application/ld+json">{"@type":"WebSite",'
                        '"name":"R","telephone":"1"}</script></head><body>x</body></html>')
    assert crumbs["business"] == {"phone": False, "address": False, "hours": False}


# --- the new site checks ------------------------------------------------------------------

def test_a_site_not_set_up_for_phones_is_a_fault():
    got = found(page("/", viewport=True), page("/a", viewport=False), page("/b", viewport=False))
    assert got["mobile-viewport"].evidence == [SITE + "/a", SITE + "/b"]
    assert dict((k, lost) for k, _, lost in breakdown(list(got.values())))["mobile-viewport"] == 9
    # a check saved before this was recorded says nothing about it
    assert "mobile-viewport" not in found(page("/"), page("/a"))
    # nor does a page the browser could not confirm
    assert "mobile-viewport" not in found(page("/"), page("/a", viewport=False, unverified=True))


def test_titles_google_cuts_off_and_descriptions_that_repeat():
    got = found(page("/"), page("/a", title="כ" * 71), page("/b", title="כ" * 70),
                page("/c", description="same"), page("/d", description="same"))
    assert got["title-length"].evidence == [f"{SITE}/a (71 תווים)"]
    assert "/c" in got["duplicate-description"].evidence[0] and "/d" in got["duplicate-description"].evidence[0]
    assert "duplicate-description" not in found(page("/"), page("/a"))
    # one page under two addresses is one page
    assert "duplicate-description" not in found(
        page("/"), page("/c", description="same"),
        page("/c?sort=1", description="same", canonical=SITE + "/c"))


def test_pages_with_little_text_are_a_thing_to_look_at_and_cost_nothing():
    got = found(page("/", word_count=20), page("/contact", word_count=30), page("/menu"))
    assert got["check-thin-pages"].evidence == [f"{SITE}/contact (30 מילים)"]   # not the home page
    assert not [row for row in breakdown(list(got.values())) if row[0] == "check-thin-pages"]


def test_details_the_site_shows_people_but_not_engines():
    some = {"phone": False, "address": True, "hours": False}
    got = found(page("/", business=some, has_phone=True, has_hours=False))
    assert got["business-details-partial"].evidence == ["טלפון"]
    both = found(page("/", business=some, has_phone=True), page("/hours", has_hours=True))
    assert both["business-details-partial"].evidence == ["טלפון", "שעות פתיחה"]
    # given on any page is given; and a site that shows no phone is not missing one
    assert "business-details-partial" not in found(
        page("/", business=some, has_phone=True),
        page("/contact", business={"phone": True, "address": True, "hours": False}))
    assert "business-details-partial" not in found(page("/", business=some))
    assert "business-details-partial" not in found(page("/", has_phone=True))   # no JSON-LD read


# --- buyers' questions --------------------------------------------------------------------

def test_a_question_is_reduced_to_the_words_that_carry_it():
    assert cores("מה המסעדה הכי טובה באזור?") == ["סעדה"]          # filler and prefixes go
    # a leading letter that may be a prefix is dropped; what is left still sits inside the word
    assert cores("איפה כדאי לאכול ארוחת בוקר בתל אביב?") == ["אכול", "ארוחת", "וקר", "בתל", "אביב"]
    assert cores("best pizza near me") == ["pizza"]
    assert cores("") == []


def test_only_a_question_the_site_says_almost_nothing_about_is_called_unanswered():
    questions = ["כמה עולה התקנת מזגן בחיפה?", "מי מתקין דודי שמש בקריות?"]
    home = parse_page(SITE, "<html><body><p>התקנת מזגנים בחיפה והסביבה, מחירים הוגנים</p></body></html>",
                      look_for=frozenset(w for q in questions for w in cores(q)))
    assert unanswered(questions, [home["words_found"]]) == ["מי מתקין דודי שמש בקריות?"]
    assert unanswered(["מה המסעדה הכי טובה?"], [[]]) == []          # one word: too little to say


def test_the_unanswered_questions_are_a_task_that_costs_nothing(tmp_path):
    cfg = ClientConfig("Acme", "acme", SITE, buyer_questions=["מי מתקין דודי שמש בקריות?"])
    facts = [page("/", words_found=[])]
    task = BuyerQuestions().analyze(cfg, facts)[0]
    assert task.key.startswith("check-") and task.evidence == cfg.buyer_questions
    assert not breakdown([task])
    assert BuyerQuestions().analyze(cfg, [page("/")]) == []         # an older check: not looked for
    assert BuyerQuestions().analyze(CFG, facts) == []               # no questions entered
    # a page we could not really read says nothing about what the site answers
    assert BuyerQuestions().analyze(cfg, [page("/", words_found=[], empty_without_browser=True)]) == []
    assert BuyerQuestions().analyze(cfg, [page("/", words_found=[], unverified=True)]) == []
    on_platform = Fact("site-crawler", "site", SITE, {"profile": "פייסבוק"})
    assert BuyerQuestions().analyze(cfg, [on_platform, *facts]) == []


def test_the_crawler_looks_for_the_questions_words(cfg):
    cfg.buyer_questions = ["Does Acme sell widgets with pricing plans?"]
    pages = {f.subject.rsplit("/", 1)[-1]: f.data for f in SiteCrawler().collect(cfg)
             if f.kind == "page" and f.data.get("status") == 200}
    assert pages[""]["words_found"] == ["acme", "pricing", "widgets"] and pages[""]["viewport"]
    assert BuyerQuestions().analyze(cfg, SiteCrawler().collect(cfg)) == []


# --- speed on a phone, as Google measures it --------------------------------------------

class SpeedApi(BaseHTTPRequestHandler):
    seen: list[str] = []
    reply: dict = {}

    def do_GET(self):
        SpeedApi.seen.append(self.path)
        payload = json.dumps(SpeedApi.reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def speed_api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), SpeedApi)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def lab(lcp=1800, cls=0.02, tbt=100, score=0.9):
    return {"lighthouseResult": {"categories": {"performance": {"score": score}}, "audits": {
        "largest-contentful-paint": {"numericValue": lcp},
        "cumulative-layout-shift": {"numericValue": cls},
        "total-blocking-time": {"numericValue": tbt}}}}


def speed(**data) -> Fact:
    base = {"score": 90, "lcp_ms": 1800, "cls": 0.02, "tbt_ms": 100, "visitors": {}}
    return Fact("page-speed", "page_speed", SITE + "/", {**base, **data})


def test_the_speed_check_asks_google_for_the_phone_measurements(speed_api):
    SpeedApi.seen.clear()
    SpeedApi.reply = {**lab(lcp=5200.4, score=0.41), "loadingExperience": {"metrics": {
        "LARGEST_CONTENTFUL_PAINT_MS": {"percentile": 3100},
        "INTERACTION_TO_NEXT_PAINT": {"percentile": 180},
        "CUMULATIVE_LAYOUT_SHIFT_SCORE": {"percentile": 12}}}}
    cfg = ClientConfig("Acme", "acme", "https://acme.test", start_path="/he/")
    data = PageSpeed("k", speed_api).collect(cfg)[0].data
    asked = SpeedApi.seen[0]
    assert asked.startswith("/pagespeedonline/v5/runPagespeed?")
    assert "strategy=mobile" in asked and "key=k" in asked and "acme.test%2Fhe%2F" in asked
    assert data["score"] == 41 and data["lcp_ms"] == 5200.4
    assert data["visitors"] == {"lcp_ms": 3100.0, "inp_ms": 180.0, "cls": 0.12}

    SpeedApi.reply = lab()                                   # a site with too few visitors
    quiet = PageSpeed("k", speed_api).collect(cfg)[0].data
    assert quiet["visitors"] == {"lcp_ms": None, "inp_ms": None, "cls": None}
    # a page on someone else's platform is as fast as they made it
    assert PageSpeed("k", speed_api).collect(
        ClientConfig("A", "a", "https://www.facebook.com", start_path="/a", platform="פייסבוק")) == []
    assert page_speed_from_env({}) is None
    assert page_speed_from_env({"GOOGLE_PAGESPEED_API_KEY": "k"}).key == "k"


def keys(*facts) -> dict:
    return {f.key: f for f in PageExperience().analyze(CFG, list(facts))}


def test_what_real_visitors_had_is_judged_by_google_s_limits():
    assert keys() == {} and keys(speed()) == {}                     # not run; all good
    got = keys(speed(visitors={"lcp_ms": 3100, "inp_ms": 260, "cls": 0.3}))
    assert set(got) == {"mobile-slow", "layout-shift", "mobile-unresponsive"}
    assert got["mobile-slow"].impact == 3 and "3.1" in got["mobile-slow"].detail
    assert keys(speed(visitors={"lcp_ms": 4500}))["mobile-slow"].impact == 4
    assert keys(speed(visitors={"lcp_ms": 2400, "inp_ms": 190, "cls": 0.1})) == {}
    # real visitors were fine, so a slow test run is not reported over them
    assert keys(speed(lcp_ms=6000, visitors={"lcp_ms": 2000})) == {}


def test_one_test_run_is_reported_only_when_it_is_clearly_poor():
    assert keys(speed(lcp_ms=3900, cls=0.2, tbt_ms=500)) == {}      # borderline: comes and goes
    got = keys(speed(lcp_ms=4100, cls=0.3, tbt_ms=700))
    assert set(got) == {"mobile-slow", "layout-shift", "mobile-unresponsive"}
    assert "בדיקת מעבדה" in got["mobile-slow"].tech


def test_speed_counts_in_the_score_and_in_what_two_checks_must_share():
    finding = keys(speed(visitors={"lcp_ms": 4500}))["mobile-slow"]
    assert breakdown([finding]) == [("mobile-slow", finding.title, 9)]
    assert parts([page("/"), speed()]) == {"site", "speed"}
    assert parts([page("/")]) == {"site"}


# --- the report says what each task changes ------------------------------------------------

def test_every_task_says_where_it_shows():
    findings = [*found(page("/", viewport=False), ai_crawlers_blocked=["OAI-SearchBot"]).values(),
                *keys(speed(visitors={"lcp_ms": 4500})).values()]
    html = render_report(CFG, RunResult(1, [page("/")], findings))
    assert html.count("משפיע על:") == len(findings) == 3
    assert "משפיע על: תשובות AI" in html and "משפיע על: גוגל<" in html


def test_new_tasks_are_short():
    cfg = ClientConfig("Acme", "acme", SITE, buyer_questions=["מי מתקין דודי שמש בקריות?"])
    some = {"phone": False, "address": True, "hours": False}
    tasks = [*found(page("/", viewport=False, title="כ" * 80, business=some, has_phone=True),
                    page("/a", word_count=10, description="d/")).values(),
             *keys(speed(lcp_ms=4100, cls=0.3, tbt_ms=700)).values(),
             *keys(speed(visitors={"lcp_ms": 3000, "inp_ms": 600, "cls": 0.3})).values(),
             *BuyerQuestions().analyze(cfg, [page("/", words_found=[])])]
    assert {"mobile-viewport", "title-length", "duplicate-description", "check-thin-pages",
            "business-details-partial", "mobile-slow", "check-questions-unanswered"} <= {t.key for t in tasks}
    for task in tasks:
        assert len(task.title.split()) <= 5, task.title
        assert len(task.detail.split()) <= 10, task.detail
        assert len(task.how_to_fix.split()) <= 10, task.how_to_fix
