"""technical-seo: is the website easy for search engines and AI to read?

Reads the site-crawler's facts and turns them into ranked findings.
The wording is for a business owner and is kept very short: `title` is the task
(a verb and an object), `detail` is the problem in one line, `how_to_fix` is the
fix in one line, and the technical name goes in the `tech` field.
"""
from __future__ import annotations

from collections import defaultdict
from urllib.parse import unquote, urlparse

from urllib.parse import urljoin

from ..collectors.site_crawler import _clean, is_shell
from ..config import ClientConfig
from ..models import Fact, Finding

NAME = "technical-seo"
BUILDER = "בונה האתר"
WRITER = "כותב התוכן"


def _pages(facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if f.collector == "site-crawler" and f.kind == "page"]


def _readable(page: Fact) -> bool:
    return page.data.get("status") == 200 and page.data.get("is_html", False)


def _header_hides_from_google(header: str) -> bool:
    """X-Robots-Tag may address one crawler ("otherbot: noindex"); only Google's or
    everyone's counts."""
    for part in header.split(","):
        crawler, _, directive = part.strip().rpartition(":")
        if "noindex" in directive and crawler.strip() in ("", "googlebot"):
            return True
    return False


def _noindex(page: Fact) -> bool:
    return ("noindex" in page.data.get("meta_robots", "")
            or _header_hides_from_google(page.data.get("x_robots_tag", "")))


def _identity(page: Fact) -> str:
    """The address the page itself names as its real one, else its own address."""
    canonical = page.data.get("canonical")
    try:
        return _clean(urljoin(page.subject, canonical)) if canonical else page.subject
    except ValueError:
        return page.subject


def _count(n: int, one: str, many: str) -> str:
    return one if n == 1 else f"{n} {many}"


def _nothing_read(pages: list[Fact]) -> Finding:
    """The one finding for a site the scan could not read at all."""
    first = next((p for p in pages if not p.data.get("redirected_to")), pages[0])
    status = first.data.get("status")
    target = urlparse(first.data.get("redirected_to") or "")
    elsewhere = f"{target.scheme}://{target.netloc}"
    asked_for = (urlparse(first.subject).hostname or "").removeprefix("www.")
    if target.netloc and asked_for and asked_for in unquote(target.query):
        # Sent to another host that carries our address along: an "are you a robot" page.
        return Finding(NAME, "site-blocked", "לפתוח את האתר לסורקים",
                       "האתר שלח את הבדיקה לדף אימות, ואולי גם מנועי AI.",
                       "לבקש מבונה האתר לאפשר גישה לסורקים.", [elsewhere],
                       5, 2, BUILDER, "הסורק הופנה לדף בדיקת בוטים")
    if target.netloc:
        return Finding(NAME, "site-moved", "לבדוק את הכתובת", "הכתובת מפנה לאתר אחר או לדף אימות.",
                       "לבדוק את הכתובת, או לאפשר גישה לסורקים.", [elsewhere],
                       5, 1, "בעל העסק", "הפניה לדומיין אחר, או דף בדיקת בוטים")
    if first.data.get("skipped") or status in (401, 403, 429):
        return Finding(NAME, "site-blocked", "לפתוח את האתר לסורקים",
                       "האתר חסם את הבדיקה, ואולי גם מנועי AI.",
                       "לבקש מבונה האתר לאפשר גישה לסורקים.", [first.subject],
                       5, 2, BUILDER, f"הסורק נחסם (קוד {status or 'הגבלת סורקים'})")
    return Finding(NAME, "site-unreachable", "לבדוק שהאתר עולה", "לא הצלחנו לפתוח את האתר.",
                   "לוודא שהכתובת נכונה ושהאתר פעיל.", [first.subject],
                   5, 2, BUILDER, f"אין תשובה תקינה ({first.data.get('error') or status})")


OWNER = "בעל העסק"
THIN_PAGE_WORDS = 50


def _business_page(site: Fact, pages: list[Fact], google_checked: bool) -> list[Finding]:
    """The check for a business whose address is a page on someone else's platform.

    Everything here is measured: the platform's published rules for AI engines, and what
    the page itself says. The one unmeasured task is marked as such and costs no points.
    """
    platform = site.data["profile"]
    at = ("ב-" if platform.isascii() else "ב") + platform
    page = pages[0] if pages else None
    found: list[Finding] = []

    def add(key, title, detail, how, evidence, impact, effort, owner, tech):
        found.append(Finding(NAME, key, title, detail, how, evidence, impact, effort, owner, tech))

    closed_to = site.data.get("ai_crawlers_blocked") or []
    refused = bool(page and page.data.get("skipped"))
    if closed_to or refused:
        add("profile-closed-to-ai", "לפרסם את העסק במקום פתוח",
            f"הדף {at} סגור למנועי AI, והם לא קוראים אותו.",
            "לפתוח אתר או פרופיל בגוגל עם פרטי העסק.", closed_to or [site.subject],
            5, 3, OWNER, "כללי הסורקים של הפלטפורמה חוסמים את הדף")
    if page and not refused:
        if page.data.get("status") in (401, 403, 429):
            # The platform turned our reader away. That says nothing about the business,
            # so it is a task to do by hand and costs no points.
            add("check-page-by-hand", "לבדוק את הדף ידנית", "הפלטפורמה חסמה את הבדיקה שלנו לדף.",
                "לוודא שיש בדף תיאור, שעות, טלפון וכתובת.", [page.subject], 3, 1, OWNER,
                f"הסורק נחסם (קוד {page.data['status']}), לא נכלל בציון")
        elif not _readable(page):
            add("profile-unreachable", "לבדוק את כתובת הדף", "לא הצלחנו לפתוח את דף העסק.",
                "לוודא שהכתובת נכונה ושהדף ציבורי.", [page.subject], 5, 2, OWNER,
                f"אין תשובה תקינה ({page.data.get('error') or page.data.get('status')})")
        else:
            if _noindex(page):
                add("profile-hidden", "להפוך את הדף לציבורי", "הדף מסומן כמוסתר ממנועי חיפוש.",
                    "לבדוק בהגדרות הדף שהוא פתוח לכולם.", [page.subject], 5, 2, OWNER,
                    "תג noindex בדף העסק")
            # What a platform shows a visitor and what it hands a reader like ours are
            # two things: details sit behind a button, a sign-in or a script. So what we
            # did not find is never said to be missing. It is listed to be looked at, and
            # costs no points. A Google Maps page hands a reader nothing at all, so
            # nothing is said about it.
            thin = page.data.get("word_count", 0) < THIN_PAGE_WORDS
            if platform == "גוגל מפות":
                pass
            elif thin:
                add("check-profile-thin", "לבדוק את פרטי הדף", "הצלחנו לקרוא רק מעט מהדף.",
                    "לוודא שיש בו תיאור, כתובת, שעות וטלפון.", [page.subject], 3, 1, OWNER,
                    f"{page.data.get('word_count', 0)} מילים נקראו (לא נכלל בציון)")
            else:
                if not page.data.get("has_hours"):
                    add("check-profile-hours", "לוודא שיש שעות פתיחה", "לא מצאנו בדף שעות פתיחה.",
                        "אם הן מופיעות בדף, אין מה לתקן.", [page.subject], 2, 1, OWNER,
                        "לא נמצאה שעה בטקסט הדף (לא נכלל בציון)")
                if not page.data.get("has_phone"):
                    add("check-profile-phone", "לוודא שיש טלפון בדף", "לא מצאנו בדף מספר טלפון.",
                        "אם הוא מופיע בדף, אין מה לתקן.", [page.subject], 2, 1, OWNER,
                        "לא נמצא מספר טלפון או קישור חיוג (לא נכלל בציון)")
    add("no-own-site", "לפתוח לעסק אתר משלו", f"לעסק יש רק דף {at}. למנועי AI זה מעט מדי.",
        "להקים אתר קטן עם תפריט, כתובת, שעות ומחירים.", [], 5, 4, "בונה אתרים",
        "אין דומיין ואתר בבעלות העסק")
    if platform != "גוגל מפות" and not google_checked:
        add("check-google-profile", "לוודא שיש פרופיל בגוגל",
            "לא בדקנו. הפרופיל מציג שעות, כתובת וביקורות בחיפוש.",
            "לפתוח או לעדכן פרופיל עסק בגוגל, בחינם.", [], 3, 1, OWNER,
            "Google Business Profile (לא נבדק, לא נכלל בציון)")
    return sorted(found, key=lambda f: (-f.score, -f.impact, f.key))


class TechnicalSeo:
    name = NAME

    def analyze(self, cfg: ClientConfig, facts: list[Fact]) -> list[Finding]:
        pages = _pages(facts)
        readable = [p for p in pages if _readable(p)]
        indexable = [p for p in readable if not _noindex(p)]
        site = next((f for f in facts if f.collector == "site-crawler" and f.kind == "site"), None)
        findings: list[Finding] = []
        # The page the scan started on, after any redirect ("/" that sends you to "/he").
        home = next((p for p in pages if not p.data.get("redirected_to")), None)
        if site is not None and site.data.get("profile"):
            google_checked = any(f.kind == "google_profile" for f in facts)
            return _business_page(site, pages, google_checked)

        def add(key, title, detail, how, evidence, impact, effort, owner, tech, among=None,
                hit=None):
            """among: the pages this check looked at; hit: the ones that have the problem.
            With them the finding records how much of the site it touches, and a problem on
            one inner page costs less than one on all. The home page is the page people and
            search engines meet first, so a problem there keeps its full price."""
            if evidence or evidence is None:
                found = Finding(NAME, key, title, detail, how, list(evidence or []),
                                impact, effort, owner, tech)
                if among and hit and not any(p is home for p in hit):
                    found.affected, found.of = min(len(hit), len(among)), len(among)
                findings.append(found)

        if pages and not readable:
            # Nothing could be read, so every other check would be a guess. Say why instead.
            return [_nothing_read(pages)]

        # Pages that are an empty shell until a browser runs their code. Crawlers, AI ones
        # included, do not run it, so for them these pages have no content at all.
        shells = [p for p in readable if not p.data.get("small_page")
                  and (p.data.get("empty_without_browser") or is_shell(p.data))]
        home_is_shell = home in shells
        if shells and (home_is_shell or len(shells) * 2 >= len(readable)):
            add("needs-js", "להציג תוכן גם לסורקים",
                "הדפים ריקים עד שדפדפן מריץ אותם. סורקים לא רואים תוכן.",
                "לבקש מבונה האתר להגיש את התוכן מוכן מהשרת.",
                [p.subject for p in shells], 5, 5, BUILDER,
                "האתר נבנה בדפדפן (JavaScript) ואין HTML מוכן")
            # Shells we could open in a real browser are checked like any page; the rest
            # have nothing to check.
            indexable = [p for p in indexable if p not in shells or p.data.get("rendered")]

        # Only a page that is really gone. A refusal (403), "slow down" (429), a server
        # hiccup or a timeout says something about our visit, not about the link.
        broken = [p for p in pages if p.data.get("status") in (404, 410)]
        add("broken-links", "לתקן קישורים שבורים",
            _count(len(broken), "קישור אחד מוביל", "קישורים מובילים") + " לדף שגיאה.",
            "לעדכן את הקישור לדף שקיים.",
            [f"{p.subject} (הקישור נמצא בדף {p.data.get('linked_from') or 'הבית'})"
             for p in broken],
            4, 2, WRITER, "קישורים שבורים (שגיאת 404)", among=pages, hit=broken)

        # A hidden home page is a fault. Hidden or closed inner pages are usually deliberate
        # (a cart, a login, search results), so they are listed to be looked at and cost
        # no points: their keys start with "check-".
        hidden = [p for p in readable if _noindex(p)]
        hidden_home = [p for p in hidden if p is home and not p.data.get("noindex_unsure")]
        add("noindex", "לחשוף את דף הבית", "דף הבית מוסתר מגוגל.",
            "לבטל את ההסתרה בהגדרות האתר.",
            [p.subject for p in hidden_home], 5, 1, BUILDER, "תג noindex בדף הבית")
        hidden_inner = [p for p in hidden if p not in hidden_home]
        add("check-noindex", "לבדוק דפים מוסתרים",
            _count(len(hidden_inner), "דף אחד מוסתר", "דפים מוסתרים") + " מגוגל.",
            "לבטל את ההסתרה, אם היא לא מכוונת.",
            [p.subject for p in hidden_inner], 2, 1, BUILDER, "תג noindex (לא נכלל בציון)")

        closed = [p for p in pages if p.data.get("googlebot_allowed") is False]
        closed_home = [p for p in closed if p is home]
        add("home-closed-to-google", "לפתוח את האתר לגוגל", "דף הבית חסום לגוגל.",
            "להסיר את החסימה בהגדרות האתר.",
            [p.subject for p in closed_home], 5, 1, BUILDER, "חסימת Googlebot בקובץ robots.txt")
        closed = [p for p in closed if p not in closed_home]
        add("check-robots-blocked", "לבדוק דפים חסומים",
            _count(len(closed), "דף אחד חסום", "דפים חסומים") + " לגוגל.",
            "להסיר את החסימה, אם היא לא מכוונת.",
            [p.subject for p in closed], 2, 1, BUILDER,
            "חסימה לגוגל בקובץ robots.txt (לא נכלל בציון)")

        # One page reached under two addresses (with and without a closing slash, or with
        # a tracking tail) is one page, and is counted once.
        seen_pages: set[str] = set()
        indexable = [p for p in indexable
                     if _identity(p) not in seen_pages and not seen_pages.add(_identity(p))]
        # A gap is reported only where we are sure of it. A page a browser could not
        # confirm (see SiteCrawler._confirm) is left out instead of guessed at.
        sure = [p for p in indexable if not p.data.get("unverified")]

        no_title = [p for p in sure if not p.data.get("title")]
        add("missing-title", "להוסיף כותרות לדפים",
            _count(len(no_title), "דף אחד מופיע", "דפים מופיעים") + " בגוגל בלי כותרת.",
            "לכתוב לכל דף כותרת קצרה.",
            [p.subject for p in no_title], 4, 1, WRITER, "תג title חסר", among=indexable, hit=no_title)

        no_desc = [p for p in sure if not p.data.get("description")]
        add("missing-description", "להוסיף תיאורים לדפים",
            _count(len(no_desc), "דף אחד מופיע", "דפים מופיעים") + " בגוגל בלי תיאור.",
            "לכתוב לכל דף משפט שמסביר מה יש בו.",
            [p.subject for p in no_desc], 3, 1, WRITER, "תג meta description חסר", among=indexable, hit=no_desc)

        # Two addresses that name the same real page (a list and the same list sorted)
        # are one page, so they are not "two pages with one title".
        by_title: dict[str, set[str]] = defaultdict(set)
        for p in sure:
            if p.data.get("title") and not p.data.get("title_unverified"):
                by_title[p.data["title"]].add(_identity(p))
        duplicates = [f'"{t}": {", ".join(sorted(urls))}'
                      for t, urls in by_title.items() if len(urls) > 1]
        add("duplicate-title", "לתת לכל דף כותרת משלו",
            "כמה דפים נושאים את אותה כותרת.",
            "לשנות את הכותרות כך שכל אחת תהיה שונה.",
            duplicates, 3, 2, WRITER, "תגי title כפולים", among=indexable,
            hit=[p for p in indexable if len(by_title.get(p.data.get("title"), ())) > 1])

        # Only a page with no main heading at all. Several of them on one page is a common
        # and accepted way to build a page, and search engines read it without trouble.
        bad_h1 = [p for p in sure if not p.data.get("h1")]
        add("h1", "להוסיף כותרת ראשית",
            ("בדף אחד" if len(bad_h1) == 1 else f"ב-{len(bad_h1)} דפים") + " אין כותרת ראשית.",
            "להוסיף כותרת ראשית אחת לכל דף.",
            [p.subject for p in bad_h1], 2, 2, BUILDER, "תג H1 חסר", among=indexable, hit=bad_h1)

        slow_pages = [p for p in readable if p.data.get("seconds", 0) > cfg.slow_page_seconds]
        slow = [f"{p.subject} ({p.data.get('seconds')} שניות)" for p in slow_pages]
        add("slow-pages", "לזרז דפים איטיים",
            _count(len(slow), "דף אחד נטען", "דפים נטענים") + " לאט.",
            "להקטין תמונות כבדות ולבדוק את האחסון.",
            slow, 3, 4, BUILDER, "זמן טעינה איטי", among=readable, hit=slow_pages)

        no_schema = [p.subject for p in indexable if not p.data.get("has_structured_data")]
        if indexable and len(no_schema) == len(indexable):
            add("structured-data", "להציג את העסק למנועים",
                "גוגל ו-AI לא מקבלים מהאתר את פרטי העסק.",
                "להוסיף לאתר את פרטי העסק בפורמט שמנועים קוראים.",
                no_schema[:5], 4, 3, BUILDER, "נתונים מובנים (schema, JSON-LD)")

        no_alt = [p for p in indexable if p.data.get("images_missing_alt")]
        alt = [f"{p.subject} ({p.data['images_missing_alt']} תמונות)" for p in no_alt]
        add("image-alt", "להוסיף תיאור לתמונות",
            "יש תמונות שמנועי חיפוש לא מבינים.",
            "לכתוב לכל תמונה תיאור של כמה מילים.",
            alt, 2, 2, WRITER, "תכונת alt חסרה בתמונות", among=indexable, hit=no_alt)

        if site is not None:
            if not site.data.get("has_sitemap"):
                # A list can sit at any address and be handed to Google directly, where
                # nobody else sees it. Not finding one proves nothing, so it costs nothing.
                add("check-sitemap", "לוודא שגוגל קיבל רשימת דפים",
                    "לא מצאנו רשימת דפים בכתובות המקובלות באתר.",
                    "אם הוגשה ב-Search Console, אין מה לתקן. אחרת להגיש.",
                    None, 2, 1, BUILDER, "sitemap.xml לא נמצא (לא נכלל בציון)")
            if site.data.get("https_broken"):
                add("https-broken", "לתקן את תעודת האבטחה",
                    "הכתובת המאובטחת של האתר (https) לא נפתחת. דפדפנים מציגים שם אזהרה.",
                    "לחדש או להתקין תעודת SSL אצל חברת האחסון.",
                    None, 5, 2, BUILDER, "שגיאת SSL בכתובת https; הסריקה נעשתה ב-http")
            blocked_bots = site.data.get("ai_crawlers_blocked") or []
            add("ai-crawlers-blocked", "להסיר חסימת AI",
                "האתר חוסם את מנועי ה-AI.",
                "להסיר את החסימה בהגדרות האתר.",
                blocked_bots, 5, 1, BUILDER, "חסימת סורקי AI בקובץ robots.txt")

        return sorted(findings, key=lambda f: (-f.score, -f.impact, f.key))
