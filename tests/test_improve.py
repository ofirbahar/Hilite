"""Accuracy of the three weakest measurements, and what brings an owner back: a re-check
that shows what changed, a way to delete a check, and an address to write to."""
from __future__ import annotations

import pytest

from bvm.analyzers.google_presence import GooglePresence
from bvm.collectors.ai_visibility import AiVisibility, mentions
from bvm.collectors.google_profile import GoogleProfile, best_match
from bvm.config import ClientConfig
from bvm.models import Fact, Finding
from bvm.pipeline import RunResult
from bvm.report import breakdown, render_report, score
from bvm.store import Store
from bvm.web import create_app
from tests.test_accuracy import CFG, found, page, site_fact
from tests.test_ai_and_web import Answer, FakeAssistant
from tests.test_profiles import FULL, Places, places_api  # noqa: F401 - the fake Places API

# --- names that contain one another ------------------------------------------------


def test_a_name_inside_a_longer_known_name_is_not_counted_twice():
    names = {"own": ["Pizza Roma"], "Roma": ["Roma"], "Roma Express": ["Roma Express"]}
    assert mentions("Try Pizza Roma downtown.", names) == {"own": 4}
    assert mentions("Roma Express is fast, Pizza Roma is better.", names) == {
        "Roma Express": 0, "own": 22}
    assert mentions("Roma is fine. Pizza Roma too.", names) == {"Roma": 0, "own": 14}
    assert mentions("ממליצים על פיצה רומא וגם על רומא.",
                    {"own": ["פיצה רומא"], "רומא": ["רומא"]}) == {"own": 11, "רומא": 28}
    assert mentions("nothing here", names) == {}


def test_a_competitor_named_inside_the_business_is_not_counted_when_the_business_is(cfg):
    cfg.name, cfg.aliases, cfg.competitors = "Pizza Roma", [], ["Roma", "Napoli"]
    cfg.buyer_questions = ["q1", "q2"]
    assistant = FakeAssistant("A", {"q1": Answer("Go to Pizza Roma."),
                                    "q2": Answer("Roma is good, Napoli too.")})
    first, second = AiVisibility([assistant]).collect(cfg)
    assert (first.data["mentioned"], first.data["competitors_mentioned"]) == (True, [])
    assert first.data["position"] == 1
    assert (second.data["mentioned"], second.data["competitors_mentioned"]) == (
        False, ["Roma", "Napoli"])


# --- which Google profile is the business's ----------------------------------------

def place(name, address="", site="", **more):
    return {"displayName": {"text": name}, "formattedAddress": address, "websiteUri": site,
            **more}


def test_the_profile_with_the_business_site_wins_over_the_first_hit(cfg):
    cfg.name, cfg.site, cfg.city = "Roma", "https://www.roma.test", "חיפה"
    places = [place("Roma Hair Salon", "תל אביב"), place("Pizza Roma", "חיפה", "http://roma.test/menu")]
    assert best_match(cfg, places) == (places[1], "site")


def test_a_business_page_on_a_platform_is_matched_by_its_whole_address(cfg):
    cfg.name, cfg.site, cfg.start_path, cfg.platform = (
        "Roma", "https://facebook.com", "/pizza.roma", "פייסבוק")
    other = place("Roma", "x", "https://www.facebook.com/roma.hair/")
    assert best_match(cfg, [other])[1] == "name"          # the same platform is not the same page
    mine = place("רומא", "x", "https://www.facebook.com/pizza.roma/")
    assert best_match(cfg, [other, mine]) == (mine, "site")


def test_without_a_site_match_the_name_decides_and_the_city_breaks_ties(cfg):
    cfg.name, cfg.site, cfg.city = "פיצה רומא", "https://roma.test", "חיפה"
    places = [place("פיצה רומא", "הרצל 1, תל אביב"), place('פיצה רומא בע"מ', "הנמל 3, חיפה")]
    assert best_match(cfg, places) == (places[1], "name")
    assert best_match(cfg, [place("מספרת רומא", "חיפה")]) == (None, "")
    assert best_match(cfg, [place("Pizza  ROMA!", "x")])[1] == ""      # another language: unsure
    cfg.name = "pizza roma"
    assert best_match(cfg, [place("Pizza  ROMA!", "x")])[1] == "name"
    assert best_match(cfg, []) == (None, "")


def test_google_is_asked_for_several_candidates(cfg, places_api):  # noqa: F811
    Places.seen.clear()
    Places.reply = {"places": [place("Roma Hair", "x"), FULL]}
    cfg.name, cfg.site = "Pizza Roma", "https://roma.test"
    fact = GoogleProfile("k", base_url=places_api).collect(cfg)[0]
    assert Places.seen[0]["body"]["pageSize"] == 5
    assert (fact.data["name"], fact.data["match"], fact.data["reviews"]) == ("Pizza Roma", "site", 212)


def test_an_unsure_match_is_shown_to_the_owner_and_costs_no_points(cfg, places_api):  # noqa: F811
    Places.reply = {"places": [place("Roma Hair Salon", "Tel Aviv", rating=2.1, userRatingCount=3,
                                     googleMapsUri="https://maps.google.com/?cid=9")]}
    cfg.name, cfg.site = "Pizza Roma", "https://roma.test"
    facts = GoogleProfile("k", base_url=places_api).collect(cfg)
    assert (facts[0].data["found"], facts[0].data["match"]) == (True, "unsure")
    findings = GooglePresence().analyze(cfg, facts)
    assert [f.key for f in findings] == ["check-google-match"]        # no rating, no reviews
    assert "Roma Hair Salon" in findings[0].evidence[0]
    assert breakdown(findings) == []
    html = render_report(cfg, RunResult(1, facts, findings, []))
    assert "לוודא שזה העסק שלך בגוגל" in html and "?</b>" in html      # nothing was measured


# --- a problem on one page costs less than the same problem everywhere ---------------

def pages(count: int, **bad) -> list[Fact]:
    return [page("/")] + [page(f"/p{n}") for n in range(1, count - 1)] + [page("/last", **bad)]


def test_a_finding_knows_how_much_of_the_site_it_touches():
    one = found(*pages(20, description=""))["missing-description"]
    assert (one.affected, one.of) == (1, 20)
    most = found(page("/"), *[page(f"/p{n}", description="") for n in range(19)])
    assert (most["missing-description"].affected, most["missing-description"].of) == (19, 20)
    assert found(page("/"), ai_crawlers_blocked=["GPTBot"])["ai-crawlers-blocked"].of == 0   # whole site


def test_points_grow_with_the_share_of_pages_and_stop_at_half():
    def lost(bad_pages: int, total: int = 20) -> int:
        some = [page(f"/p{n}", title="" if n >= total - bad_pages else f"T{n}")
                for n in range(total)]                                 # the first page is home
        return dict((key, points) for key, _, points in
                    breakdown(list(found(*some).values())))["missing-title"]
    assert lost(1) == 4 and lost(5) == 6 and lost(10) == 9 == lost(19)
    assert lost(1) < lost(5) < lost(10)
    assert lost(1, total=1) == 9                                       # the only page is all of it


def test_a_whole_site_problem_and_an_old_saved_finding_cost_the_full_points():
    whole = Finding("technical-seo", "sitemap", "t", "d", "h", [], 3, 1)
    old = Finding("technical-seo", "missing-title", "t", "d", "h", ["a"], 4, 1)   # saved before
    assert [points for _, _, points in breakdown([whole, old])] == [9, 5]


def test_the_report_says_how_many_pages_each_problem_touches():
    findings = list(found(*pages(20, description="")).values())
    html = render_report(CFG, RunResult(1, [site_fact(), *pages(20)], findings, []))
    assert "1 מתוך 20 דפים" in html and "−2" in html
    assert score(findings, None, 20) == 98


def test_the_extent_survives_the_notebook(tmp_path):
    store = Store(tmp_path / "n.sqlite3")
    run_id = store.start_run("acme")
    store.save_findings(run_id, "acme", [Finding("technical-seo", "k", "t", "d", "h", [], 3, 1,
                                                 affected=2, of=30)])
    loaded = store.load_findings(run_id)[0]
    assert (loaded.affected, loaded.of) == (2, 30)


def test_a_notebook_from_before_this_change_still_opens(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
            config TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE findings (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL,
            client TEXT NOT NULL, analyzer TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL,
            detail TEXT NOT NULL, how_to_fix TEXT NOT NULL, evidence TEXT NOT NULL,
            impact INTEGER NOT NULL, effort INTEGER NOT NULL, score INTEGER NOT NULL,
            owner TEXT NOT NULL DEFAULT '', tech TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
        INSERT INTO runs (client, started_at, status) VALUES ('acme', '2026-01-01T00:00:00+00:00', 'done');
        INSERT INTO findings (run_id, client, analyzer, key, title, detail, how_to_fix, evidence,
            impact, effort, score, created_at) VALUES (1, 'acme', 'technical-seo', 'h1', 't', 'd',
            'h', '[]', 2, 2, 2, 'x');
    """)
    db.commit()
    db.close()
    store = Store(path)
    assert store.load_findings(1)[0].of == 0 and store.get_run(1)["score"] is None
    assert store.previous_run("acme", before=99) is None    # no saved score: nothing to compare
    store.close()


# --- checking again, and what changed -------------------------------------------------

class Site:
    """Stands in for the crawler: the site as it is 'today'."""

    name = "site-crawler"
    pages: list[Fact] = []
    site: dict = {}

    def collect(self, cfg):
        return [site_fact(**Site.site), *Site.pages]


@pytest.fixture
def app_client(tmp_path):
    Site.pages, Site.site = [page("/", description="")], {"has_sitemap": False}
    app = create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                     collectors=lambda cfg: [Site()], contact="hello@hilite.test")
    return app.test_client()


def start(client) -> str:
    """Runs a check and returns the address the owner lands on."""
    response = client.post("/scan", data={"name": "Acme", "site": "acme.test"})
    assert response.status_code == 302
    return response.headers["Location"]


def test_the_score_is_saved_with_the_check(app_client, tmp_path):
    start(app_client)
    run = Store(tmp_path / "w.sqlite3").get_run(1)
    assert run["score"] == 95 and run["scoring"] >= 2


def test_a_first_check_has_nothing_to_compare_to(app_client):
    html = app_client.get(start(app_client)).get_data(as_text=True)
    assert "מאז הבדיקה הקודמת" not in html and "בדוק שוב" in html


def test_checking_again_scans_the_same_business_and_shows_what_changed(app_client):
    first = start(app_client)
    Site.pages, Site.site = [page("/", images_missing_alt=3)], {"has_sitemap": True}
    again = app_client.post("/runs/1/again")
    assert again.status_code == 302 and "/runs/2" in again.headers["Location"]
    assert again.headers["Location"] != first
    html = app_client.get(again.headers["Location"]).get_data(as_text=True)
    assert "מאז הבדיקה הקודמת" in html
    assert "עלה ב-2 נקודות" in html and "מ-95 ל-97" in html
    since = html.split('class="since"')[1].split("</section>")[0]
    assert "להוסיף תיאורים לדפים" in since and "לוודא שגוגל קיבל רשימת דפים" in since   # fixed
    assert "להוסיף תיאור לתמונות" in since                                         # new
    assert since.index("תוקן") < since.index("להוסיף תיאורים לדפים") < since.index("חדש")


def test_a_lower_score_and_an_unchanged_score_are_said_plainly(app_client):
    start(app_client)
    app_client.post("/runs/1/again")
    html = app_client.get("/runs/2").get_data(as_text=True)
    since = html.split('class="since"')[1].split("</section>")[0]
    assert "הציון לא השתנה" in since and "ירד" not in since and "עלה" not in since
    assert "תוקן" not in since and "חדש" not in since
    Site.pages = [page("/", description="", title="")]
    app_client.post("/runs/2/again")
    html = app_client.get("/runs/3").get_data(as_text=True)
    assert "ירד ב-9 נקודות" in html and "מ-95 ל-86" in html


def test_another_business_is_not_compared(app_client):
    start(app_client)
    app_client.post("/scan", data={"name": "Other", "site": "other.test"})
    assert "מאז הבדיקה הקודמת" not in app_client.get("/runs/2").get_data(as_text=True)


def test_checking_again_is_refused_from_another_site_and_for_a_missing_check(app_client):
    start(app_client)
    assert app_client.post("/runs/1/again", headers={"Origin": "https://evil.test"}).status_code == 403
    assert app_client.post("/runs/77/again").status_code == 404
    assert app_client.get("/runs/1/again").status_code == 405


# --- deleting a check -------------------------------------------------------------------

def test_only_the_owner_link_can_delete(app_client, tmp_path):
    owner = start(app_client)
    assert "key=" in owner
    key = owner.split("key=")[1]
    assert len(key) >= 20
    shared = app_client.get("/runs/1").get_data(as_text=True)
    assert "למחוק לצמיתות" not in shared and key not in shared          # the plain link only reads
    mine = app_client.get(owner).get_data(as_text=True)
    assert "למחוק לצמיתות" in mine and "אי אפשר לשחזר" in mine
    assert '<bdi data-share="/runs/1">http://localhost/runs/1</bdi>' in mine                  # the address to pass on

    assert app_client.post("/runs/1/delete").status_code == 403
    assert app_client.post("/runs/1/delete?key=wrong").status_code == 403
    assert app_client.post(f"/runs/1/delete?key={key}",
                           headers={"Origin": "https://evil.test"}).status_code == 403
    assert app_client.get(f"/runs/1/delete?key={key}").status_code == 405   # never by a link
    assert app_client.get("/runs/1").status_code == 200

    gone = app_client.post(f"/runs/1/delete?key={key}")
    assert gone.status_code == 302
    after = app_client.get(gone.headers["Location"]).get_data(as_text=True)
    assert "הבדיקה נמחקה" in after and "Acme" not in after
    assert app_client.get("/runs/1").status_code == 404
    db = Store(tmp_path / "w.sqlite3").db
    for table in ("runs", "facts", "findings", "failures"):
        assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_a_check_with_no_key_cannot_be_deleted_from_the_site(app_client, tmp_path):
    store = Store(tmp_path / "w.sqlite3")
    run_id = store.start_run("acme", {"name": "Acme", "site": "https://acme.test"})
    store.finish_run(run_id)
    assert app_client.post(f"/runs/{run_id}/delete?key=").status_code == 403
    assert app_client.post(f"/runs/{run_id}/delete?key=None").status_code == 403


def test_the_key_is_not_sent_to_other_sites_or_listed(app_client):
    owner = start(app_client)
    key = owner.split("key=")[1]
    assert app_client.get(owner).headers["Referrer-Policy"] == "same-origin"
    assert key not in app_client.get("/check").get_data(as_text=True)


def test_the_operator_can_delete_a_check_by_number(tmp_path):
    from click.testing import CliRunner

    from bvm.cli import main
    path = str(tmp_path / "n.sqlite3")
    store = Store(path)
    run_id = store.start_run("acme", {"name": "Acme"})
    store.close()
    done = CliRunner().invoke(main, ["delete", str(run_id), "--db", path])
    assert done.exit_code == 0 and Store(path).get_run(run_id) is None
    assert CliRunner().invoke(main, ["delete", "55", "--db", path]).exit_code != 0


# --- an address to write to -------------------------------------------------------------

def test_the_privacy_page_says_how_to_delete_and_whom_to_write_to(app_client):
    text = app_client.get("/privacy").get_data(as_text=True)
    assert "מחיקת הבדיקה" in text and "hello@hilite.test" in text
    assert 'href="mailto:hello@hilite.test"' in text
    assert "אין כרגע באתר כפתור למחיקת בדיקה" not in text


def test_without_an_address_the_page_does_not_invent_one(tmp_path, monkeypatch):
    monkeypatch.delenv("HILITE_CONTACT", raising=False)
    client = create_app(str(tmp_path / "w.sqlite3")).test_client()
    text = client.get("/privacy").get_data(as_text=True)
    assert "mailto:" not in text and "מחיקת הבדיקה" in text
    monkeypatch.setenv("HILITE_CONTACT", "team@hilite.test")
    client = create_app(str(tmp_path / "w.sqlite3")).test_client()
    assert "team@hilite.test" in client.get("/privacy").get_data(as_text=True)


# --- what a second, independent review of the above found -----------------------------

def test_a_problem_on_the_home_page_costs_the_full_points():
    rest = [page(f"/p{n}") for n in range(29)]
    home_bad = found(page("/", title="", description=""), *rest)
    assert dict((key, lost) for key, _, lost in breakdown(list(home_bad.values()))) == {
        "missing-title": 9, "missing-description": 5}
    inner_bad = found(page("/"), page("/p0", title="", description=""), *rest[1:])
    assert dict((key, lost) for key, _, lost in breakdown(list(inner_bad.values()))) == {
        "missing-title": 4, "missing-description": 2}


def test_two_pages_on_a_platform_that_differ_only_after_the_question_mark_are_two_pages(cfg):
    cfg.name, cfg.site, cfg.start_path, cfg.platform = (
        "פיצה רומא", "https://facebook.com", "/profile.php?id=1000123", "פייסבוק")
    other = place("Roma Hair", "x", "https://www.facebook.com/profile.php?id=999")
    assert best_match(cfg, [other]) == (None, "")
    mine = place("Roma", "x", "https://facebook.com/profile.php?id=1000123")
    assert best_match(cfg, [other, mine]) == (mine, "site")


def test_two_sites_on_one_shared_host_are_two_sites(cfg):
    cfg.name, cfg.site, cfg.start_path = "Pizza Roma", "https://sites.google.com", "/view/pizza-roma"
    assert best_match(cfg, [place("Hair", "x", "https://sites.google.com/view/roma-hair")]) == (None, "")
    mine = place("Hair", "x", "https://sites.google.com/view/pizza-roma/menu")
    assert best_match(cfg, [mine]) == (mine, "site")
    cfg.site, cfg.start_path = "https://www.ikea.test", "/il/he"       # a language folder
    for website in ("https://ikea.test", "https://www.ikea.test/il/he/", "http://ikea.test/il/he/stores"):
        assert best_match(cfg, [place("x", "y", website)])[1] == "site", website


def test_a_name_that_only_contains_the_other_needs_the_right_city(cfg):
    cfg.name, cfg.site, cfg.city = "Roma", "https://roma.test", "חיפה"
    salon = place("Roma Hair Salon", "דיזנגוף 5, תל אביב")
    assert best_match(cfg, [salon]) == (None, "")                      # a namesake elsewhere
    assert best_match(cfg, [place("Roma Hair Salon", "הנמל 3, חיפה")])[1] == "name"
    assert best_match(cfg, [place("Roma", "דיזנגוף 5, תל אביב")])[1] == "name"   # the very name
    cfg.city = ""
    assert best_match(cfg, [salon])[1] == "name"                       # no city to go by


def test_names_are_found_in_answers_written_the_way_assistants_write():
    names = {"own": ["Pizza Roma", "פיצה רומא"], "Roma": ["Roma"], "Napoli": ["Napoli", "נאפולי"]}
    answer = ("Here are a few options:\n\n1. **Pizza Roma** – known for thin crust.\n"
              "2. *Napoli's* (napoli.co.il) has a wood oven.\n3. \"Roma\" is a newer place.")
    found_in = mentions(answer, names)
    assert set(found_in) == {"own", "Napoli", "Roma"}
    assert answer[found_in["own"]:].startswith("Pizza Roma")
    assert answer[found_in["Roma"]:].startswith('Roma"')
    hebrew = "מומלץ: [פיצה רומא](https://pizzaroma.test), ואחריה נאפולי. בנאפולי יש גם משלוחים."
    assert mentions(hebrew, names) == {"own": hebrew.index("פיצה"), "Napoli": hebrew.index("נאפולי")}
    assert mentions("Romania and Romans love pizza; so does Napolitano.", names) == {}


def test_checks_that_measured_different_things_are_not_compared(tmp_path):
    Site.pages, Site.site = [page("/")], {}
    assistant = FakeAssistant("A", {"best widgets?": Answer("Try Globex.")})
    client = create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                        collectors=lambda cfg: [Site(), AiVisibility([assistant])]).test_client()
    client.post("/scan", data={"name": "Acme", "site": "acme.test"})          # the site alone: 100
    client.post("/scan", data={"name": "Acme", "site": "acme.test", "questions": "best widgets?"})
    html = client.get("/runs/2").get_data(as_text=True)
    assert "<b>50</b>" in html and "מאז הבדיקה הקודמת" not in html   # not "fell by 50 points"
    client.post("/runs/2/again")
    assert "הציון לא השתנה" in client.get("/runs/3").get_data(as_text=True)


def test_a_task_that_was_not_measured_again_is_not_called_fixed():
    from bvm.report import changes
    old = [Finding("technical-seo", "sitemap", "רשימת דפים", "d", "h", [], 3, 1),
           Finding("ai-gap", "ai-share", "להופיע ב-AI", "d", "h", [], 4, 4)]
    previous = {"date": "01.01.2026", "score": 60, "findings": old, "parts": {"site", "ai"}}
    assert changes(previous, 95, [], {"site"}) is None                  # the AI part did not run
    same = changes(previous, 95, [], {"site", "ai"})
    assert same["fixed"] == ["רשימת דפים", "להופיע ב-AI"] and same["delta"] == 35


def test_a_change_of_one_point_is_said_in_the_singular(app_client):
    clean = [page("/")] + [page(f"/p{n}") for n in range(19)]
    Site.pages, Site.site = clean[:-1] + [page("/b", images_missing_alt=1)], {"has_sitemap": True}
    start(app_client)
    Site.pages = clean
    app_client.post("/runs/1/again")
    since = app_client.get("/runs/2").get_data(as_text=True).split('class="since"')[1]
    assert "הציון עלה בנקודה אחת" in since and "ב-1 נקודות" not in since


def test_two_requests_that_upgrade_an_old_notebook_together_both_succeed(tmp_path, monkeypatch):
    import sqlite3

    import bvm.store as store_module
    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL,"
                     " started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL DEFAULT"
                     " 'running', config TEXT NOT NULL DEFAULT '{}');")
    db.close()
    Store(path).close()                                                 # the first one upgrades
    # The second saw the old columns a moment earlier and tries to add them again.
    monkeypatch.setattr(store_module, "_columns", lambda db, table: set())
    Store(path).close()


def test_the_address_to_pass_on_is_the_one_the_visitor_is_really_on(app_client):
    html = app_client.get(start(app_client)).get_data(as_text=True)
    assert 'data-share="/runs/1"' in html and "location.origin" in html


# --- checks made before personal links and before the new score ------------------------

def test_the_operator_can_give_an_old_check_its_personal_link(tmp_path):
    from click.testing import CliRunner

    from bvm.cli import main
    path = str(tmp_path / "w.sqlite3")
    store = Store(path)
    run_id = store.start_run("acme", {"name": "Acme", "slug": "acme", "site": "https://acme.test"})
    store.finish_run(run_id)
    store.close()
    out = CliRunner().invoke(main, ["link", str(run_id), "--db", path])
    assert out.exit_code == 0
    link = out.output.strip().split()[-1]
    assert link.startswith(f"/runs/{run_id}?key=") and len(link.split("key=")[1]) >= 20
    again = CliRunner().invoke(main, ["link", str(run_id), "--db", path])
    assert again.output.strip().split()[-1] == link                     # the same link, not a new one
    client = create_app(path, allow_private=True, background=False,
                        collectors=lambda cfg: []).test_client()
    assert "למחוק לצמיתות" in client.get(link).get_data(as_text=True)
    assert client.post(link.replace("?", "/delete?")).status_code == 302
    assert CliRunner().invoke(main, ["link", "55", "--db", path]).exit_code != 0


def test_old_checks_are_scored_again_from_what_was_collected(tmp_path):
    from click.testing import CliRunner

    from bvm.cli import main
    from bvm.scoring import SCORING
    path = str(tmp_path / "w.sqlite3")
    store = Store(path)
    cfg = ClientConfig("Acme", "acme", "https://acme.test")
    run_id = store.start_run("acme", cfg.to_dict())
    facts = [site_fact(), page("/"), *[page(f"/p{n}") for n in range(18)], page("/last", title="")]
    store.save_facts(run_id, "acme", facts)
    # As the old version saved it: no extent, the old full price, no score on the run.
    store.save_findings(run_id, "acme", [Finding("technical-seo", "missing-title", "t", "d", "h",
                                                 ["x"], 4, 1)])
    store.finish_run(run_id)
    failed = store.start_run("acme", cfg.to_dict())
    store.finish_run(failed, status="failed")
    store.close()

    out = CliRunner().invoke(main, ["rescore", "--db", path])
    assert out.exit_code == 0 and "1" in out.output
    store = Store(path)
    run = store.get_run(run_id)
    finding = store.load_findings(run_id)[0]
    assert (run["score"], run["scoring"]) == (96, SCORING)
    assert (finding.key, finding.affected, finding.of) == ("missing-title", 1, 20)
    assert store.get_run(failed)["score"] is None                        # a failed check stays so
    assert store.previous_run("acme", before=99, scoring=SCORING)["id"] == run_id
    assert "0" in CliRunner().invoke(main, ["rescore", "--db", path]).output   # nothing left to do


def test_a_mistyped_address_is_called_a_typing_mistake_not_a_forbidden_address(tmp_path):
    client = create_app(str(tmp_path / "w.sqlite3"), background=False,
                        collectors=lambda cfg: []).test_client()
    typo = client.post("/scan", data={"name": "Acme", "site": "no-such-site.invalid"})
    assert typo.status_code == 400 and "שגיאת הקלדה" in typo.get_data(as_text=True)
    inner = client.post("/scan", data={"name": "Acme", "site": "http://127.0.0.1"})
    assert inner.status_code == 400 and "אתר ציבורי" in inner.get_data(as_text=True)
    assert 'value="no-such-site.invalid"' in typo.get_data(as_text=True)   # nothing to retype
