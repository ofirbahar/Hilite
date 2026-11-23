from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bvm.ai_providers import (AnthropicProvider, Answer, ChatCompletionsProvider,
                              providers_from_env)
from bvm.analyzers.ai_gap import AiGap, summarize
from bvm.collectors.ai_visibility import AiVisibility, NotConfigured, first_mention
from bvm.collectors.site_crawler import SiteCrawler
from bvm.config import ClientConfig, is_public_address
from bvm.pipeline import run
from bvm.report import render_report
from bvm.store import Store
from bvm.web import create_app


class FakeAssistant:
    """Stands in for a real AI assistant: fixed answer per question."""

    def __init__(self, name, answers):
        self.name, self.answers, self.asked = name, answers, []

    def ask(self, question, country):
        self.asked.append((question, country))
        answer = self.answers[question]
        if isinstance(answer, Exception):
            raise answer
        return answer


QUESTIONS = ["best widget tools?", "alternatives to Globex?", "cheap widgets?"]
ANSWERS = {
    "best widget tools?": Answer("Top picks: Globex, then Acme Corp and Initech.",
                                 ["https://www.reviews.test/best", "https://blog.test/x"]),
    "alternatives to Globex?": Answer("Try Initech. See https://www.reviews.test/alt for more."),
    "cheap widgets?": Answer("Many small shops sell widgets."),
}


@pytest.fixture
def ai_cfg(cfg):
    cfg.name, cfg.aliases = "Acme", ["Acme Corp"]
    cfg.competitors = ["Globex", "Initech"]
    cfg.buyer_questions = list(QUESTIONS)
    cfg.target_country = "IL"
    return cfg


# --- ai-visibility collector -------------------------------------------------

def test_first_mention_matches_whole_names_only():
    assert first_mention("We like ACME a lot", ["Acme"]) == 8
    assert first_mention("Acmeology is a field", ["Acme"]) is None
    assert first_mention("nothing here", ["Acme", ""]) is None


def test_ai_collector_records_mentions_position_competitors_and_sources(ai_cfg):
    assistant = FakeAssistant("fake-ai", ANSWERS)
    facts = {f.subject: f.data for f in AiVisibility([assistant]).collect(ai_cfg)}
    best = facts["best widget tools?"]
    assert best["mentioned"] is True and best["position"] == 2
    assert best["competitors_mentioned"] == ["Globex", "Initech"]
    assert best["cited_domains"] == ["blog.test", "reviews.test"]
    alt = facts["alternatives to Globex?"]
    assert alt["mentioned"] is False and alt["position"] is None
    assert alt["cited_domains"] == ["reviews.test"]  # taken from the answer text
    assert facts["cheap widgets?"]["competitors_mentioned"] == []
    assert assistant.asked[0] == ("best widget tools?", "IL")


def test_ai_collector_needs_an_assistant_only_when_there_are_questions(ai_cfg):
    with pytest.raises(NotConfigured):
        AiVisibility([]).collect(ai_cfg)
    ai_cfg.buyer_questions = []
    assert AiVisibility([]).collect(ai_cfg) == []


def test_ai_collector_keeps_going_when_one_request_fails(ai_cfg):
    answers = dict(ANSWERS, **{"cheap widgets?": TimeoutError("slow")})
    facts = AiVisibility([FakeAssistant("fake-ai", answers)]).collect(ai_cfg)
    assert sum(1 for f in facts if f.kind == "ai_answer") == 2
    assert [f.data["errors"] for f in facts if f.kind == "ai_errors"] == [["fake-ai: TimeoutError"]]
    all_fail = {q: RuntimeError("down") for q in QUESTIONS}
    with pytest.raises(RuntimeError):
        AiVisibility([FakeAssistant("fake-ai", all_fail)]).collect(ai_cfg)


# --- ai-gap analyzer ---------------------------------------------------------

def test_ai_gap_reports_share_gaps_and_trusted_sources(ai_cfg):
    facts = AiVisibility([FakeAssistant("fake-ai", ANSWERS)]).collect(ai_cfg)
    assert summarize(ai_cfg, facts) == {
        "total": 3, "mentioned": 1, "assistants": ["fake-ai"],
        "competitors": [("Globex", 1), ("Initech", 2)],
    }
    findings = {f.key: f for f in AiGap().analyze(ai_cfg, facts)}
    assert set(findings) == {"ai-share", "ai-competitor-gap", "ai-citation-sources"}
    assert "1 מתוך 3" in findings["ai-share"].detail
    assert findings["ai-share"].impact == 4
    assert findings["ai-competitor-gap"].evidence == ["alternatives to Globex? (fake-ai): Initech"]
    assert findings["ai-citation-sources"].evidence[0] == "reviews.test (2 תשובות)"


def test_ai_gap_is_silent_without_ai_facts_or_when_always_mentioned(ai_cfg):
    assert AiGap().analyze(ai_cfg, []) == []
    always = {q: Answer("Acme is the best.") for q in QUESTIONS}
    facts = AiVisibility([FakeAssistant("fake-ai", always)]).collect(ai_cfg)
    assert AiGap().analyze(ai_cfg, facts) == []


def test_report_shows_ai_section_and_missing_key_as_a_failure(ai_cfg, tmp_path):
    store = Store(tmp_path / "n.sqlite3")
    result = run(ai_cfg, store,
                 collectors=[SiteCrawler(), AiVisibility([FakeAssistant("fake-ai", ANSWERS)])])
    html = render_report(ai_cfg, result)
    assert "מה ה-AI עונה" in html and "Initech" in html
    no_key = run(ai_cfg, store, collectors=[SiteCrawler(), AiVisibility([])])
    assert no_key.failures[0][0] == "ai-visibility"
    assert no_key.findings and "מה ה-AI עונה" not in render_report(ai_cfg, no_key)


# --- real provider request shapes, against a fake API on this machine ---------

class ApiHandler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        ApiHandler.seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.path == "/v1/messages":
            reply = {"content": [{"type": "text", "text": "Use Acme."}]}
        else:
            reply = {"choices": [{"message": {"content": "Use Globex."}}],
                     "citations": ["https://src.test/a"]}
        payload = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def api():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_providers_send_the_documented_request_shapes(api):
    ApiHandler.seen.clear()
    claude = AnthropicProvider("k1", base_url=api)
    assert claude.ask("q?", "US").text == "Use Acme."
    sent = ApiHandler.seen[0]
    assert sent["path"] == "/v1/messages" and sent["headers"]["x-api-key"] == "k1"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    assert sent["body"]["messages"] == [{"role": "user", "content": "q?"}]
    assert sent["body"]["max_tokens"] > 0 and "US" in sent["body"]["system"]

    other = ChatCompletionsProvider("perplexity", "k2", "some-model", api)
    answer = other.ask("q?", "US")
    assert answer.text == "Use Globex." and answer.citations == ["https://src.test/a"]
    sent = ApiHandler.seen[1]
    assert sent["path"] == "/chat/completions" and sent["headers"]["Authorization"] == "Bearer k2"
    assert sent["body"]["model"] == "some-model"


def test_assistants_are_switched_on_only_by_their_keys():
    assert providers_from_env({}) == []
    assert providers_from_env({"OPENAI_API_KEY": "k"}) == []  # no model name, stays off
    names = [p.name for p in providers_from_env({
        "ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "BVM_OPENAI_MODEL": "m",
        "PERPLEXITY_API_KEY": "p", "BVM_PERPLEXITY_MODEL": "m"})]
    assert names == ["claude", "chatgpt", "perplexity"]


# --- web interface -----------------------------------------------------------

def make_app(tmp_path, **kwargs):
    kwargs.setdefault("allow_private", True)
    kwargs.setdefault("background", False)
    return create_app(str(tmp_path / "web.sqlite3"), **kwargs).test_client()


def test_web_scan_shows_the_report_and_lists_it(site, tmp_path):
    assistant = FakeAssistant("fake-ai", ANSWERS)
    client = make_app(tmp_path, collectors=lambda cfg: [SiteCrawler(), AiVisibility([assistant])])
    landing = client.get("/").get_data(as_text=True)
    assert "ממליצים על העסק שלך" in landing and 'href="/check"' in landing
    assert 'name="site"' in client.get("/check").get_data(as_text=True)

    posted = client.post("/scan", data={
        "name": "Acme", "site": site + "/some/page", "competitors": "Globex, Initech",
        "questions": "\n".join(QUESTIONS)})
    assert posted.status_code == 302 and "/runs/1?key=" in posted.headers["Location"]
    report = client.get("/runs/1").get_data(as_text=True)
    assert "מה לתקן קודם" in report and "מתוך 100" in report
    assert "מה ה-AI עונה" in report and "בדיקה חדשה" in report
    assert [q for q, _ in assistant.asked] == QUESTIONS

    home = client.get("/check").get_data(as_text=True)
    assert "בדיקות קודמות" in home and "Acme" in home


def test_web_rejects_bad_input_and_addresses_inside_the_network(tmp_path):
    client = make_app(tmp_path, allow_private=False)
    for data in ({"name": "Acme", "site": "not-a-url"},
                 {"name": "Acme", "site": "http://127.0.0.1:8080"},
                 {"name": "Acme", "site": "http://localhost"}):
        response = client.post("/scan", data=data)
        assert response.status_code == 400 and 'role="alert"' in response.get_data(as_text=True)
    assert client.get("/runs/1").status_code == 404  # nothing was started
    assert not is_public_address("http://10.0.0.5") and not is_public_address("http://[::1]")


def test_web_shows_waiting_page_while_running_and_marks_crashes(tmp_path):
    db = str(tmp_path / "web.sqlite3")
    store = Store(db)
    run_id = store.start_run("acme", ClientConfig("Acme", "acme", "https://acme.test").to_dict())
    client = create_app(db).test_client()
    waiting = client.get(f"/runs/{run_id}").get_data(as_text=True)
    assert "בודקים את Acme" in waiting and 'http-equiv="refresh"' in waiting

    def exploding(cfg):
        raise RuntimeError("boom")

    crash = create_app(db, allow_private=True, background=False, collectors=exploding).test_client()
    crash.post("/scan", data={"name": "Acme", "site": "http://127.0.0.1:1"})
    home = crash.get("/check").get_data(as_text=True)
    assert "נכשלה" in home
    assert "boom" in crash.get("/runs/2").get_data(as_text=True)
