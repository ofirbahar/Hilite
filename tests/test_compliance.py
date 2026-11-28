"""Privacy, honesty and accessibility of the site itself."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from bvm.web import create_app

TEMPLATES = Path(__file__).parent.parent / "bvm" / "templates"


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "w.sqlite3"), allow_private=True, background=False,
                     collectors=lambda cfg: [])
    client = app.test_client()
    client.post("/scan", data={"name": "Acme", "site": "example.com"})   # makes /runs/1
    return client


PAGES = ["/", "/check", "/privacy", "/runs/1", "/sample"]


# --- third parties and data -------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_no_page_calls_another_company(client, path):
    html = client.get(path).get_data(as_text=True)
    outside = set(re.findall(r"""(?:src|href|action)=["'](?:https?:)?//([^/"']+)""", html))
    outside.discard("localhost")                     # the page naming its own address
    assert outside == set(), outside                 # no font service, tracker or widget


def test_the_fonts_come_from_this_site(client):
    css = client.get("/static/fonts/fonts.css")
    assert css.status_code == 200 and "fonts.gstatic.com" not in css.get_data(as_text=True)
    files = re.findall(r"url\((/static/fonts/[^)]+)\)", css.get_data(as_text=True))
    assert files and all(client.get(f).status_code == 200 for f in set(files))


@pytest.mark.parametrize("path", PAGES)
def test_no_cookies_are_set(client, path):
    assert "Set-Cookie" not in client.get(path).headers


def test_the_form_asks_only_for_what_the_check_needs(client):
    form = client.get("/check").get_data(as_text=True)
    assert set(re.findall(r'<(?:input|textarea)[^>]*name="([^"]+)"', form)) == {
        "name", "site", "city", "competitors", "questions"}
    for personal in ("email", "phone", "tel", "password"):
        assert f'type="{personal}"' not in form


# --- consent and privacy ----------------------------------------------------------

def test_the_form_says_what_pressing_the_button_does(client):
    form = client.get("/check").get_data(as_text=True)
    assert "נסרוק את הכתובת" in form and "נשמור" in form and 'href="/privacy"' in form


def test_the_privacy_page_says_what_is_kept_who_gets_it_and_who_it_is_for(client):
    page = client.get("/privacy")
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    for must_say in ("מה נשמר", "עוגיות", "עוזרי AI", "גוגל", "18", "ילדים"):
        assert must_say in text, must_say
    assert 'href="/privacy"' in client.get("/").get_data(as_text=True)


# --- honest claims ----------------------------------------------------------------

def test_the_site_makes_no_claim_it_cannot_back():
    copy = "".join((TEMPLATES / name).read_text(encoding="utf-8")
                   for name in ("landing.html", "check.html", "waiting.html"))
    for claim in ("בדיקה של דקה", "עד דקה", "מקום ראשון", "מובטח", "הטוב ביותר", "100%"):
        assert claim not in copy, claim
    listed = re.search(r"<h3>(\d+) בדיקות באתר</h3>\s*<ul[^>]*>(.*?)</ul>", copy, re.S)
    assert int(listed.group(1)) == listed.group(2).count("<li>")   # the number matches the list
    assert f"{listed.group(1)} בדיקות באתר" in copy.split("</section>")[0]   # and the hero


def test_there_are_no_reviews_prices_or_pressure_on_the_site():
    copy = "".join(p.read_text(encoding="utf-8") for p in TEMPLATES.glob("*.html"))
    for pattern in ("לקוחות מרוצים", "ממליצים עלינו", "כוכבים", "₪", "מבצע", "נשארו רק", "מהרו",
                    "checked", "countdown"):
        assert pattern not in copy, pattern


# --- accessibility ----------------------------------------------------------------

@pytest.mark.parametrize("path", PAGES)
def test_keyboard_users_can_skip_to_the_content(client, path):
    html = client.get(path).get_data(as_text=True)
    assert html.index('class="skip"') < html.index("<header")       # the first thing Tab reaches
    assert 'href="#main"' in html and html.count('id="main"') == 1


@pytest.mark.parametrize("path", PAGES)
def test_every_picture_has_a_description_or_is_marked_decorative(client, path):
    html = client.get(path).get_data(as_text=True)
    for img in re.findall(r"<img[^>]*>", html):
        assert "alt=" in img, img
    for svg in re.findall(r"<svg[^>]*>", html):
        assert 'aria-hidden="true"' in svg or "aria-label=" in svg, svg
    for field in re.findall(r"<(?:input|textarea)[^>]*>", html):
        ident = re.search(r'id="([^"]+)"', field)
        assert ident and f'for="{ident.group(1)}"' in html, field   # every field has a label


def _luminance(colour: str) -> float:
    parts = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in parts]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_text_colours_are_readable():
    css = (TEMPLATES / "_style.html").read_text(encoding="utf-8")
    token = lambda name: re.search(rf"--{name}: (#[0-9a-f]{{6}})", css).group(1)  # noqa: E731
    placeholder = re.search(r"::placeholder \{ color: (#[0-9a-f]{6})", css).group(1)
    pairs = [(token("ink"), token("paper")), (token("muted"), token("paper")),
             (token("muted"), token("fill")), (placeholder, token("card")),
             (token("accent"), token("paper")), (token("paper"), token("ink")),   # a button
             (token("on-mark"), token("mark")),                                   # marked text
             (token("night-muted"), token("night")), (token("good"), token("good-tint")),
             (token("warn"), token("warn-tint")), (token("bad"), token("bad-tint")),
             (token("good"), token("card")), (token("bad"), token("card")),
             ("#ffffff", "#c22f28"), ("#ffffff", "#a3251f")]
    for foreground, background in pairs:
        assert contrast(foreground, background) >= 4.5, (foreground, background)
    border = re.search(r"input, textarea \{[^}]*border: 1px solid (#[0-9a-f]{6})", css).group(1)
    assert contrast(border, token("paper")) >= 3                     # a field's edge must be visible
    landing = (TEMPLATES / "_landing_style.html").read_text(encoding="utf-8")
    dimmed = re.search(r"\.say-text \.w \{ opacity: ([\d.]+)", landing)
    assert dimmed is None or float(dimmed.group(1)) >= 0.5          # no text waits, dimmed, to be read
