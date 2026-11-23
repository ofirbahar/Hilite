from __future__ import annotations

from datetime import date

import pytest
from click.testing import CliRunner

from bvm.analyzers.technical_seo import TechnicalSeo
from bvm.cli import main
from bvm.collectors.site_crawler import USER_AGENT, SiteCrawler
from bvm.config import ConfigError, load_config
from bvm.pipeline import run
from bvm.report import render_report
from bvm.store import Store
from tests.conftest import Handler


def by_url(facts, site):
    return {f.subject.removeprefix(site): f.data for f in facts if f.kind == "page"}


# --- site-crawler -----------------------------------------------------------

def test_crawler_reads_page_facts(cfg, site):
    pages = by_url(SiteCrawler().collect(cfg), site)
    home = pages["/"]
    assert home["status"] == 200
    assert home["title"] == "Acme Home"
    assert home["description"] == "Acme makes widgets."
    assert home["h1"] == ["Acme"]
    assert home["lang"] == "en"
    assert home["images_missing_alt"] == 0
    assert pages["/about"]["title"] == ""
    assert pages["/about"]["h1"] == ["About", "Team"]
    assert pages["/about"]["images_missing_alt"] == 1
    assert "noindex" in pages["/pricing"]["meta_robots"]


def test_crawler_records_broken_links_and_where_they_are_linked_from(cfg, site):
    pages = by_url(SiteCrawler().collect(cfg), site)
    assert pages["/missing"]["status"] == 404
    assert pages["/missing"]["linked_from"] == site + "/"


def test_crawler_obeys_robots_and_stays_on_the_site(cfg, site):
    pages = by_url(SiteCrawler().collect(cfg), site)
    assert pages["/private/notes"] == {"skipped": "robots.txt", "linked_from": site + "/"}
    assert "/private/notes" not in Handler.hits
    assert all(not url.startswith("http") for url in pages)  # nothing off-site


def test_crawler_reports_site_level_facts(cfg):
    site_fact = next(f for f in SiteCrawler().collect(cfg) if f.kind == "site")
    assert site_fact.data == {
        "has_robots_txt": True,
        "has_sitemap": False,
        "ai_crawlers_blocked": ["GPTBot"],
    }


def test_crawler_identifies_itself_and_respects_page_limit(cfg):
    crawler = SiteCrawler()
    assert crawler.session.headers["User-Agent"] == USER_AGENT
    cfg.max_pages = 2
    assert sum(1 for f in crawler.collect(cfg) if f.kind == "page") == 2


# --- technical-seo ----------------------------------------------------------

@pytest.fixture
def findings(cfg):
    return {f.key: f for f in TechnicalSeo().analyze(cfg, SiteCrawler().collect(cfg))}


def test_analyzer_finds_the_planted_problems(findings, site):
    assert set(findings) == {
        "broken-links", "noindex", "robots-blocked", "missing-title", "missing-description",
        "h1", "slow-pages", "structured-data", "image-alt", "sitemap", "ai-crawlers-blocked",
    }
    assert findings["noindex"].evidence == [site + "/pricing"]
    assert findings["missing-title"].evidence == [site + "/about"]
    assert findings["ai-crawlers-blocked"].evidence == ["GPTBot"]
    assert site + "/missing" in findings["broken-links"].evidence[0]
    assert site + "/slow" in findings["slow-pages"].evidence[0]


def test_noindex_page_is_not_also_reported_as_duplicate_title(findings):
    # /pricing shares the home title but is hidden from search, so it is not a duplicate.
    assert "duplicate-title" not in findings


def test_findings_are_ranked_high_impact_cheap_fixes_first(cfg):
    ranked = TechnicalSeo().analyze(cfg, SiteCrawler().collect(cfg))
    scores = [f.score for f in ranked]
    assert scores == sorted(scores, reverse=True)
    assert ranked[0].impact == 5 and ranked[0].effort == 1


def test_clean_site_has_no_findings(cfg):
    from bvm.models import Fact
    facts = [
        Fact("site-crawler", "site", cfg.site,
             {"has_robots_txt": True, "has_sitemap": True, "ai_crawlers_blocked": []}),
        Fact("site-crawler", "page", cfg.site + "/", {
            "status": 200, "is_html": True, "title": "Home", "description": "d", "h1": ["Home"],
            "meta_robots": "", "x_robots_tag": "", "has_structured_data": True,
            "images_missing_alt": 0, "seconds": 0.05, "googlebot_allowed": True}),
    ]
    assert TechnicalSeo().analyze(cfg, facts) == []


# --- pipeline, notebook, report --------------------------------------------

def test_pipeline_saves_facts_and_findings_to_the_notebook(cfg, tmp_path):
    store = Store(tmp_path / "n.sqlite3")
    result = run(cfg, store)
    assert result.failures == []
    assert len(store.load_facts(result.run_id)) == len(result.facts) > 3
    saved = store.db.execute("SELECT COUNT(*) FROM findings WHERE run_id = ?",
                             (result.run_id,)).fetchone()[0]
    assert saved == len(result.findings) > 0
    assert store.db.execute("SELECT finished_at FROM runs").fetchone()[0]


def test_one_failing_part_does_not_stop_the_run(cfg, tmp_path):
    class Broken:
        name = "broken-collector"

        def collect(self, cfg):
            raise RuntimeError("source is down")

    store = Store(tmp_path / "n.sqlite3")
    result = run(cfg, store, collectors=[Broken(), SiteCrawler()])
    assert result.failures == [("broken-collector", "RuntimeError: source is down")]
    assert result.findings  # the crawler still ran
    assert "broken-collector" in render_report(cfg, result)


def test_report_shows_one_score_and_three_tasks_first(cfg, tmp_path):
    result = run(cfg, Store(tmp_path / "n.sqlite3"))
    cfg.name = "<b>Acme</b>"
    html = render_report(cfg, result, today=date(2026, 11, 23))
    assert 'dir="rtl"' in html and "23.11.2026" in html
    assert "מתוך 100" in html and "דורש טיפול" in html
    assert "מה לתקן קודם" in html and "עוד משימות" in html
    assert "<b>Acme</b>" not in html and "&lt;b&gt;Acme&lt;/b&gt;" in html
    first_screen = html.split("<h2>פרטים</h2>")[0]
    assert first_screen.count('<span class="n">') == 3      # exactly three numbered tasks
    assert result.findings[0].title in first_screen and result.findings[3].title not in first_screen
    for jargon in ("noindex", "robots.txt", "schema"):       # jargon only under "technical details"
        assert jargon not in html.split('<details class="tech">')[0]


def test_findings_say_who_fixes_them_and_keep_the_technical_name_aside(cfg):
    for finding in TechnicalSeo().analyze(cfg, SiteCrawler().collect(cfg)):
        assert finding.owner and finding.tech
        for jargon in ("noindex", "robots", "schema", "H1", "alt", "sitemap", "JSON"):
            assert jargon not in finding.title + finding.detail + finding.how_to_fix


def test_score_and_grade():
    from bvm.models import Finding
    from bvm.report import grade, score

    def site(impact):
        return Finding("technical-seo", "k", "t", "d", "h", impact=impact)

    assert score([], None, 5) == 100 and grade(100) == ("good", "מצב טוב")
    assert score([site(5), site(4)], None, 5) == 76 and grade(76)[0] == "warn"
    assert score([site(5)] * 9, None, 5) == 0 and grade(0) == ("bad", "דורש טיפול")
    assert score([], {"mentioned": 1, "total": 4}, 5) == 62      # half site, half AI
    assert score([Finding("ai-gap", "k", "t", "d", "h", impact=5)], None, 5) == 100
    assert score([], None, 0) is None and grade(None)[0] == "unknown"
    assert score([], {"mentioned": 2, "total": 4}, 0) == 50      # site unreadable: AI only


def test_tasks_are_short(cfg):
    for finding in TechnicalSeo().analyze(cfg, SiteCrawler().collect(cfg)):
        assert len(finding.title.split()) <= 5, finding.title
        assert len(finding.detail.split()) <= 10 and len(finding.how_to_fix.split()) <= 10


# --- settings and command line ---------------------------------------------

def test_settings_file_is_validated(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: X\nslug: x\nsite: example.com\n")
    with pytest.raises(ConfigError):
        load_config(bad)
    bad.write_text("name: X\nsite: https://example.com\n")
    with pytest.raises(ConfigError):
        load_config(bad)
    assert load_config("clients/example.yaml").max_pages == 50


def test_command_line_writes_a_report(site, tmp_path):
    settings = tmp_path / "acme.yaml"
    settings.write_text(f"name: Acme\nslug: acme\nsite: {site}\ncrawl:\n  delay_seconds: 0\n")
    out = CliRunner().invoke(main, ["run", str(settings), "--db", str(tmp_path / "n.sqlite3"),
                                    "--out", str(tmp_path / "reports")])
    assert out.exit_code == 0, out.output
    reports = list((tmp_path / "reports").glob("acme-*.html"))
    assert len(reports) == 1 and "Acme" in reports[0].read_text(encoding="utf-8")
