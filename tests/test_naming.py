"""The owner types one thing, the address. The business's name is read from the page."""
from __future__ import annotations

import pytest

from bvm.collectors.site_crawler import parse_page
from bvm.config import ClientConfig
from bvm.models import Fact
from bvm.naming import business_name
from bvm.pipeline import run
from bvm.store import Store
from bvm.web import create_app
from tests.test_accuracy import site_fact
from tests.test_ai_and_web import Answer, FakeAssistant


def named(html: str, site: str = "https://roma-pizza.test", path: str = "/", platform: str = "",
          status: int = 200) -> str:
    cfg = ClientConfig("", "x", site, start_path=path, platform=platform)
    url = site + path
    data = {"status": status, "is_html": True, **(parse_page(url, html) if status == 200 else {})}
    return business_name(cfg, [Fact("site-crawler", "page", url, data)])


def page_with(head: str = "", title: str = "") -> str:
    return f"<html><head><title>{title}</title>{head}</head><body><p>hi</p></body></html>"


# --- a site of the business's own -------------------------------------------------------

def test_the_name_the_site_gives_itself_is_used():
    assert named(page_with('<meta property="og:site_name" content=" פיצה רומא ">',
                           "תפריט | משלוחים")) == "פיצה רומא"


def test_a_slogan_next_to_the_name_is_left_out():
    assert named(page_with('<meta property="og:site_name" content="רמי לוי - קניות אונליין">'),
                 site="https://www.rami-levy.co.il") == "רמי לוי"


def test_the_business_details_for_search_engines_come_next():
    ld = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
          '{"@type":"WebPage","name":"Home"},{"@type":"Restaurant","name":"Pizza Roma"}]}</script>')
    assert named(page_with(ld, "Home")) == "Pizza Roma"
    broken = '<script type="application/ld+json">{not json</script>'
    assert named(page_with(broken, "Pizza Roma")) == "Pizza Roma"      # bad data is skipped


@pytest.mark.parametrize("title, expected", [
    ("Roma Pizza | הפיצה הכי טעימה בחיפה", "Roma Pizza"),          # the part that is the address
    ("דף הבית - Roma Pizza", "Roma Pizza"),
    ("פיצה רומא | דף הבית", "פיצה רומא"),                          # not "home page"
    ("ברוכים הבאים לאתר – פיצה רומא", "פיצה רומא"),
    ("פיצה רומא", "פיצה רומא"),
])
def test_otherwise_the_part_of_the_title_that_is_a_name(title, expected):
    assert named(page_with(title=title)) == expected


def test_with_nothing_on_the_page_the_address_names_the_business():
    assert named(page_with()) == "Roma Pizza"
    assert named("", site="https://www.zap.co.il", status=0) == "Zap"   # the site did not open
    assert named(page_with(title="x" * 300)) == "Roma Pizza"            # a title that is no name
    assert named(page_with(title="roma-pizza.test")) == "Roma Pizza"    # nor is the address


# --- a page on a platform ---------------------------------------------------------------

def test_a_business_page_is_named_by_its_own_title_without_the_platform():
    html = page_with('<meta property="og:title" content="פיצה רומא | Facebook">',
                     "פיצה רומא | Facebook")
    assert named(html, "https://facebook.com", "/pizza.roma", "פייסבוק") == "פיצה רומא"
    insta = page_with(title="Pizza Roma (@pizza.roma) • Instagram photos and videos")
    assert named(insta, "https://instagram.com", "/pizza.roma", "אינסטגרם") == "Pizza Roma"


def test_a_page_that_could_not_be_read_is_named_from_its_address():
    assert named("", "https://facebook.com", "/DominosPizzaIsrael", "פייסבוק", 0) == "DominosPizzaIsrael"
    assert named("", "https://google.com", "/maps/place/%D7%A4%D7%99%D7%A6%D7%94+%D7%A8%D7%95%D7%9E%D7%90/@32.07,34.78,15z",
                 "גוגל מפות", 0) == "פיצה רומא"
    assert named("", "https://wolt.com", "/he/isr/haifa/restaurant/pizza-roma", "Wolt", 0) == "pizza roma"
    login = page_with(title="Log in to Facebook")                       # the platform's own page
    assert named(login, "https://facebook.com", "/pizza.roma", "פייסבוק") == "pizza.roma"


# --- the whole way through ----------------------------------------------------------------

class Site:
    name = "site-crawler"

    def collect(self, cfg):
        html = page_with('<meta property="og:site_name" content="Acme">', "Acme | Widgets")
        return [site_fact(), Fact("site-crawler", "page", cfg.site + "/",
                                  {"status": 200, "is_html": True, "linked_from": "",
                                   **parse_page(cfg.site + "/", html)})]


def test_the_name_is_known_before_the_checks_that_need_it(tmp_path):
    seen = []

    class Later:
        name = "google-profile"

        def collect(self, cfg):
            seen.append(cfg.name)
            return []

    store = Store(tmp_path / "n.sqlite3")
    cfg = ClientConfig("", "acme", "https://acme.test")
    result = run(cfg, store, collectors=[Site(), Later()], analyzers=[])
    assert seen == ["Acme"] and cfg.name == "Acme" and cfg.name_guessed
    assert store.get_run(result.run_id)["config"]["name"] == "Acme"     # saved for the report


def test_a_name_the_owner_typed_is_never_replaced(tmp_path):
    cfg = ClientConfig("My Shop", "acme", "https://acme.test")
    run(cfg, Store(tmp_path / "n.sqlite3"), collectors=[Site()], analyzers=[])
    assert cfg.name == "My Shop" and not cfg.name_guessed


def app(tmp_path, collectors):
    return create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                      collectors=collectors).test_client()


def test_the_address_alone_is_enough(tmp_path):
    client = app(tmp_path, lambda cfg: [Site()])
    started = client.post("/scan", data={"site": "acme.test"})
    assert started.status_code == 302
    html = client.get(started.headers["Location"]).get_data(as_text=True)
    assert '<span class="who">Acme</span>' in html and "<title>הדוח של Acme</title>" in html
    assert "השם זוהה מהדף" in html
    assert "Acme" in client.get("/check").get_data(as_text=True)        # and in the list


def test_a_typed_name_is_shown_without_the_note(tmp_path):
    client = app(tmp_path, lambda cfg: [Site()])
    started = client.post("/scan", data={"site": "acme.test", "name": "My Shop"})
    html = client.get(started.headers["Location"]).get_data(as_text=True)
    assert '<span class="who">My Shop</span>' in html and "השם זוהה מהדף" not in html


def test_the_found_name_is_what_ai_answers_are_searched_for(tmp_path):
    assistant = FakeAssistant("A", {"best widgets?": Answer("Acme is the best.")})
    from bvm.collectors.ai_visibility import AiVisibility
    client = app(tmp_path, lambda cfg: [Site(), AiVisibility([assistant])])
    started = client.post("/scan", data={"site": "acme.test", "questions": "best widgets?"})
    html = client.get(started.headers["Location"]).get_data(as_text=True)
    assert "1 מתוך 1" in html and '<span class="yes">הוזכרת</span>' in html


def test_the_form_shows_one_field_and_keeps_the_rest_folded(tmp_path):
    form = app(tmp_path, lambda cfg: []).get("/check").get_data(as_text=True)
    main, extra = form.split("<details")[0], form.split("<details")[1].split("</details>")[0]
    assert 'name="site"' in main and 'name="name"' not in main and 'name="city"' not in main
    for field in ("name", "city", "competitors", "questions"):
        assert f'name="{field}"' in extra
    assert 'name="name" required' not in form and "לא חובה" in extra
    assert " open" not in form.split("<details")[1].split(">")[0]       # folded until asked for


def test_while_checking_the_address_stands_in_for_the_name(tmp_path):
    store = Store(tmp_path / "w.sqlite3")
    store.start_run("acme", ClientConfig("", "acme", "https://acme.test").to_dict(), view="readme")
    store.close()
    html = create_app(str(tmp_path / "w.sqlite3")).test_client().get("/runs/1?v=readme").get_data(as_text=True)
    assert "בודקים את <bdi>acme.test</bdi>" in html


# --- an address that only opens without the lock ---------------------------------------

def test_a_site_whose_secure_address_is_broken_is_still_checked_and_told_so(cfg, monkeypatch):
    from bvm.analyzers.technical_seo import TechnicalSeo
    from bvm.collectors.site_crawler import SiteCrawler
    plain = cfg.site                                   # the fake site speaks http only
    cfg.site = plain.replace("http://", "https://")    # the owner typed no "http", so https
    real = SiteCrawler._get

    def get(self, url, obey=True):
        if url.startswith("https://"):
            return None, 0.0, "SSLError"               # a certificate that does not hold
        return real(self, url, obey)

    monkeypatch.setattr(SiteCrawler, "_get", get)
    facts = SiteCrawler().collect(cfg)
    site = next(f for f in facts if f.kind == "site")
    read = [f for f in facts if f.kind == "page" and f.data.get("status") == 200]
    assert site.data["https_broken"] and len(read) > 1 and read[0].subject.startswith("http://")
    found = {f.key: f for f in TechnicalSeo().analyze(cfg, facts)}
    assert "https-broken" in found and found["https-broken"].impact == 5
    assert "site-unreachable" not in found


def test_a_site_that_opens_nowhere_is_still_unreachable(cfg, monkeypatch):
    from bvm.analyzers.technical_seo import TechnicalSeo
    from bvm.collectors.site_crawler import SiteCrawler
    cfg.site = cfg.site.replace("http://", "https://")
    monkeypatch.setattr(SiteCrawler, "_get", lambda self, url, obey=True: (None, 0.0, "SSLError"))
    keys = [f.key for f in TechnicalSeo().analyze(cfg, SiteCrawler().collect(cfg))]
    assert keys == ["site-unreachable"]


def test_a_healthy_site_is_not_told_its_lock_is_broken(cfg):
    from bvm.analyzers.technical_seo import TechnicalSeo
    from bvm.collectors.site_crawler import SiteCrawler
    facts = SiteCrawler().collect(cfg)
    assert "https-broken" not in [f.key for f in TechnicalSeo().analyze(cfg, facts)]
