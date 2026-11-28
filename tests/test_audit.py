"""What an audit of the whole site found: who can read a report, what a stranger can do,
what happens when things go wrong, and what was left over from earlier versions."""
from __future__ import annotations

import logging
import re

import pytest

from bvm.collectors.site_crawler import USER_AGENT
from bvm.store import Store
from bvm.web import create_app
from tests.test_accuracy import page, site_fact


class Site:
    name = "site-crawler"

    def collect(self, cfg):
        return [site_fact(), page("/")]


def public(tmp_path, **more):
    """The site as it runs for real: not in development mode."""
    app = create_app(str(tmp_path / "w.sqlite3"), background=False,
                     collectors=lambda cfg: [Site()], **more)
    return app.test_client()


def start(client, site="example.com", **data) -> str:
    response = client.post("/scan", data={"site": site, **data})
    assert response.status_code == 302, response.get_data(as_text=True)[:300]
    return response.headers["Location"]


# --- who can read a report --------------------------------------------------------------

def test_a_report_cannot_be_opened_by_guessing_its_number(tmp_path):
    client = public(tmp_path)
    owner = start(client)
    assert client.get(owner).status_code == 200
    assert client.get("/runs/1").status_code == 404
    assert client.get("/runs/1?v=guess").status_code == 404
    assert client.get("/runs/1?key=guess").status_code == 404
    assert client.post("/runs/1/again").status_code == 404        # nor copied by a stranger


def test_the_owner_gets_a_read_only_address_to_pass_on(tmp_path):
    client = public(tmp_path)
    mine = client.get(start(client)).get_data(as_text=True)
    share = re.search(r'data-share="([^"]+)"', mine).group(1).replace("&amp;", "&")
    assert re.fullmatch(r"/runs/1\?v=[\w-]{20,}", share)
    shared = client.get(share)
    assert shared.status_code == 200
    text = shared.get_data(as_text=True)
    assert "למחוק לצמיתות" not in text and "key=" not in text       # reads, cannot delete
    key = re.search(r"key=([\w-]+)", mine).group(1)
    assert key not in text
    again = client.post(re.search(r'action="(/runs/1/again[^"]*)"', text).group(1).replace("&amp;", "&"))
    assert again.status_code == 302                                # a reader may check again


def test_other_people_s_checks_are_not_listed(tmp_path):
    client = public(tmp_path)
    start(client, name="Secret Bakery")
    listing = client.get("/check").get_data(as_text=True)
    assert "Secret Bakery" not in listing and "/runs/1" not in listing
    assert 'id="my-checks"' in listing                             # this browser's own list


def test_the_browser_remembers_its_own_checks_and_forgets_a_deleted_one(tmp_path):
    client = public(tmp_path)
    owner = start(client)
    report = client.get(owner).get_data(as_text=True)
    assert "localStorage" in report and "hilite-checks" in report
    shared = client.get(re.search(r'data-share="([^"]+)"', report).group(1).replace("&amp;", "&"))
    assert "hilite-checks" not in shared.get_data(as_text=True)    # a reader's list is not touched
    gone = client.post(owner.replace("?", "/delete?"))
    assert gone.headers["Location"].endswith("/check?deleted=1")   # which check to forget


def test_in_development_reports_open_by_number_as_before(tmp_path):
    app = create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                     collectors=lambda cfg: [Site()])
    client = app.test_client()
    start(client, name="Acme")
    assert client.get("/runs/1").status_code == 200
    assert "Acme" in client.get("/check").get_data(as_text=True)


def test_a_check_made_before_this_can_be_given_both_links(tmp_path):
    from click.testing import CliRunner

    from bvm.cli import main
    path = str(tmp_path / "w.sqlite3")
    store = Store(path)
    run_id = store.start_run("acme", {"name": "Acme", "slug": "acme", "site": "https://acme.test"})
    store.finish_run(run_id)
    store.close()
    out = CliRunner().invoke(main, ["link", str(run_id), "--db", path]).output
    personal = re.search(r"/runs/1\?key=[\w-]+", out).group(0)
    reading = re.search(r"/runs/1\?v=[\w-]+", out).group(0)
    client = create_app(path).test_client()
    assert client.get(personal).status_code == 200 and client.get(reading).status_code == 200
    assert client.get("/runs/1").status_code == 404


# --- what the site tells browsers and search engines --------------------------------------

def test_every_page_refuses_to_be_framed_and_limits_what_may_load(tmp_path):
    client = public(tmp_path)
    for path in ("/", "/check", "/privacy", start(client)):
        headers = client.get(path).headers
        assert headers["X-Frame-Options"] == "DENY", path
        assert headers["X-Content-Type-Options"] == "nosniff"
        policy = headers["Content-Security-Policy"]
        assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy
        assert "form-action 'self'" in policy


def test_reports_are_kept_out_of_search_engines_and_shared_caches(tmp_path):
    client = public(tmp_path)
    report = client.get(start(client))
    assert "noindex" in report.headers["X-Robots-Tag"]
    assert "no-store" in report.headers["Cache-Control"]
    assert "noindex" in client.get("/check").headers["X-Robots-Tag"]
    assert "X-Robots-Tag" not in client.get("/").headers            # the home page is public
    robots = client.get("/robots.txt")
    assert robots.status_code == 200 and "Disallow: /runs/" in robots.get_data(as_text=True)
    assert client.get("/favicon.ico").status_code == 200


def test_the_home_page_has_a_preview_for_sharing(tmp_path):
    home = public(tmp_path).get("/").get_data(as_text=True)
    for tag in ('property="og:title"', 'property="og:description"', 'property="og:type"'):
        assert tag in home, tag


# --- when things go wrong -------------------------------------------------------------------

@pytest.mark.parametrize("path, status", [("/nope", 404), ("/runs/999", 404), ("/runs/abc", 404)])
def test_a_wrong_address_gets_a_page_in_hebrew_with_a_way_on(tmp_path, path, status):
    response = public(tmp_path).get(path)
    text = response.get_data(as_text=True)
    assert response.status_code == status
    assert 'dir="rtl"' in text and "הדף לא נמצא" in text and 'href="/check"' in text
    assert "Not Found" not in text


def test_a_refused_action_and_a_wrong_method_are_also_explained(tmp_path):
    client = public(tmp_path)
    owner = start(client)
    wrong = client.post("/runs/1/delete?key=wrong")
    assert wrong.status_code == 404                     # a stranger learns nothing, not even "exists"
    method = client.get("/scan")
    assert method.status_code == 405 and 'dir="rtl"' in method.get_data(as_text=True)
    assert client.get(owner).status_code == 200


def test_a_crash_shows_a_plain_page_and_no_details(tmp_path):
    app = create_app(str(tmp_path / "w.sqlite3"))
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.get("/boom")
    def boom():
        raise RuntimeError("secret detail")

    response = app.test_client().get("/boom")
    text = response.get_data(as_text=True)
    assert response.status_code == 500 and "משהו השתבש" in text and "secret detail" not in text


def test_a_second_press_cannot_start_a_second_check(tmp_path):
    client = public(tmp_path)
    form = client.get("/check").get_data(as_text=True)
    assert "data-once" in form and "disabled = true" in form       # the button locks on submit
    report = client.get(start(client)).get_data(as_text=True)
    assert report.count("data-once") >= 2 and "disabled = true" in report


def test_one_visitor_cannot_start_checks_without_end(tmp_path):
    import bvm.web as web
    client = public(tmp_path)
    for _ in range(web.MAX_PER_VISITOR):
        start(client)
    refused = client.post("/scan", data={"site": "example.com"})
    assert refused.status_code == 429
    text = refused.get_data(as_text=True)
    assert 'role="alert"' in text and 'value="example.com"' in text  # nothing typed is lost
    other = client.post("/scan", data={"site": "example.com"},
                        environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert other.status_code == 302                                 # someone else is not affected


def test_checking_again_counts_toward_the_same_limit(tmp_path):
    import bvm.web as web
    client = public(tmp_path)
    owner = start(client)
    again = owner.replace("?", "/again?")
    for _ in range(web.MAX_PER_VISITOR - 1):
        assert client.post(again).status_code == 302
    assert client.post(again).status_code == 429


# --- secrets stay out of the log -------------------------------------------------------------

def test_personal_links_are_not_written_to_the_server_log():
    from bvm.web import HideKeys
    record = logging.LogRecord("werkzeug", logging.INFO, "", 0,
                               '127.0.0.1 - - "GET /runs/12?key=AbC-123_xyz&v=Qq9 HTTP/1.1" 200 -',
                               None, None)
    HideKeys().filter(record)
    assert "AbC-123_xyz" not in record.getMessage() and "Qq9" not in record.getMessage()
    assert "/runs/12?key=…" in record.getMessage()
    with_args = logging.LogRecord("werkzeug", logging.INFO, "", 0, "%s", (
        'POST /runs/3/delete?key=SECRETKEY HTTP/1.1',), None)
    HideKeys().filter(with_args)
    assert "SECRETKEY" not in with_args.getMessage()


# --- leftovers from earlier versions -----------------------------------------------------------

def test_the_scanner_introduces_itself_by_the_product_s_name():
    assert USER_AGENT.startswith("HiliteBot/") and "BrandVisibility" not in USER_AGENT


def test_a_very_long_address_gets_a_short_key(tmp_path):
    client = public(tmp_path)
    long_path = "/maps/place/" + "x" * 900
    start(client, site="https://www.google.com" + long_path)
    start(client, site="https://www.google.com" + long_path + "y")
    store = Store(tmp_path / "w.sqlite3")
    first, second = store.get_run(1)["client"], store.get_run(2)["client"]
    assert len(first) <= 80 and len(second) <= 80 and first != second   # short, still distinct


def test_a_very_long_name_does_not_break_the_page(tmp_path):
    client = public(tmp_path)
    html = client.get(start(client, name="א" * 500)).get_data(as_text=True)
    assert "א" * 81 not in html and "א" * 80 in html


def test_the_privacy_page_says_what_the_browser_keeps_and_who_can_read_a_report(tmp_path):
    text = public(tmp_path).get("/privacy").get_data(as_text=True)
    assert "במכשיר שלך" in text and "הבדיקות האחרונות מופיעות בעמוד הבדיקה" not in text
    assert "מי שיש לו את כתובת הדוח יכול לפתוח אותו" not in text
    assert "קישור לקריאה" in text


# --- only our own scripts run, and the site has what it asks of others ---------------------

def test_a_script_runs_only_with_the_mark_made_for_that_answer(tmp_path):
    client = public(tmp_path)
    marks = set()
    for path in ("/", "/check", "/privacy", start(client)):
        answer = client.get(path)
        policy = answer.headers["Content-Security-Policy"]
        script_rule = re.search(r"script-src ([^;]+)", policy).group(1)
        assert "unsafe-inline" not in script_rule and "object-src 'none'" in policy, path
        mark = re.search(r"'nonce-([\w-]{16,})'", script_rule).group(1)
        marks.add(mark)
        # every script the page carries has the mark; the details for search engines are data
        scripts = re.findall(r"<script([^>]*)>", answer.get_data(as_text=True))
        assert scripts and all(f'nonce="{mark}"' in s or "application/ld+json" in s
                               for s in scripts), path
        assert "camera=()" in answer.headers["Permissions-Policy"]
        assert answer.headers["Cross-Origin-Opener-Policy"] == "same-origin"
        assert "Strict-Transport-Security" not in answer.headers       # plain http here
    assert len(marks) == 4                                             # a new one every time
    secure = client.get("/", base_url="https://localhost")
    assert "max-age" in secure.headers["Strict-Transport-Security"]


def test_a_form_far_larger_than_ours_is_refused(tmp_path):
    client = public(tmp_path)
    assert client.post("/scan", data={"site": "example.com", "questions": "x" * 200_000}).status_code == 413


def test_the_site_has_what_it_checks_other_sites_for(tmp_path):
    import json

    client = public(tmp_path, public_url="https://hilite.example/")
    home = client.get("/").get_data(as_text=True)
    assert '<link rel="canonical" href="https://hilite.example/">' in home
    data = json.loads(re.search(r'<script type="application/ld\+json">(.*?)</script>', home, re.S).group(1))
    kinds = {node["@type"]: node for node in data["@graph"]}
    assert set(kinds) == {"WebSite", "WebApplication", "FAQPage"}
    asked = kinds["FAQPage"]["mainEntity"]
    assert len(asked) >= 3
    for question in asked:                       # what search engines are told is on the page
        assert question["name"] in home and question["acceptedAnswer"]["text"] in home
    assert 'rel="canonical" href="https://hilite.example/privacy"' in client.get("/privacy").get_data(as_text=True)

    robots = client.get("/robots.txt").get_data(as_text=True)
    assert "Sitemap: https://hilite.example/sitemap.xml" in robots
    sitemap = client.get("/sitemap.xml")
    assert sitemap.headers["Content-Type"].startswith("application/xml")
    listed = re.findall(r"<loc>([^<]+)</loc>", sitemap.get_data(as_text=True))
    assert listed == ["https://hilite.example/", "https://hilite.example/privacy"]   # no reports
    llms = client.get("/llms.txt")
    assert llms.headers["Content-Type"].startswith("text/plain")
    text = llms.get_data(as_text=True)
    assert text.startswith("# Hilite") and "https://hilite.example/privacy" in text
    assert "&#" not in text and "&amp;" not in text                    # plain text, not HTML


def test_our_own_home_page_passes_our_own_check(tmp_path):
    """Hilite's home page, read by Hilite's own reader: nothing it reports as a gap."""
    from bvm.collectors.site_crawler import parse_page

    home = public(tmp_path).get("/").get_data(as_text=True)
    facts = parse_page("https://hilite.example/", home)
    assert facts["title"] and facts["description"] and len(facts["h1"]) == 1
    assert facts["has_structured_data"] and facts["data_name"] == "Hilite"
    assert facts["images_missing_alt"] == 0 and facts["lang"] == "he"
    assert facts["canonical"] and "noindex" not in facts["meta_robots"]


def test_the_pages_carry_nothing_a_visitor_does_not_need(tmp_path):
    """No notes to ourselves in what is sent, no moving backdrop, and a picture for sharing."""
    client = public(tmp_path, public_url="https://hilite.example")
    for path in ("/", "/check", "/privacy", start(client)):
        html = client.get(path).get_data(as_text=True)
        assert "/*" not in html and "{#" not in html, path          # our notes stay in the source
        assert "<canvas" not in html and "backdrop-filter" not in html, path
    home = client.get("/").get_data(as_text=True)
    assert '<meta property="og:image" content="https://hilite.example/static/share.png">' in home
    picture = client.get("/static/share.png")
    assert picture.status_code == 200 and picture.data[1:4] == b"PNG"


def test_a_first_visit_shows_no_empty_list_of_checks(tmp_path):
    """The list of this browser's checks is hidden until there is one. A style that sets how
    the section is laid out must not bring the empty heading back."""
    html = public(tmp_path).get("/check").get_data(as_text=True)
    assert 'id="my-checks" hidden' in html
    assert "[hidden] { display: none !important; }" in html


def test_a_check_can_be_started_from_the_first_screen(tmp_path):
    """The home page's own field starts a real check, with the same notice as the form."""
    client = public(tmp_path)
    home = client.get("/").get_data(as_text=True)
    hero = home.split('class="hero"', 1)[1].split("</section>", 1)[0]
    assert 'method="post" action="/scan"' in hero and 'name="site"' in hero
    assert '<label for="hero-site">' in hero and "נסרוק את הכתובת" in hero and 'href="/privacy"' in hero
    assert start(client, site="example.com").startswith("/runs/1?key=")
