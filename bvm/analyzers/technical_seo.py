"""technical-seo: is the website easy for search engines and AI to read?

Reads the site-crawler's facts and turns them into ranked findings.
The wording is for a business owner and is kept very short: `title` is the task
(a verb and an object), `detail` is the problem in one line, `how_to_fix` is the
fix in one line, and the technical name goes in the `tech` field.
"""
from __future__ import annotations

from collections import defaultdict

from ..config import ClientConfig
from ..models import Fact, Finding

NAME = "technical-seo"
BUILDER = "בונה האתר"
WRITER = "כותב התוכן"


def _pages(facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if f.collector == "site-crawler" and f.kind == "page"]


def _readable(page: Fact) -> bool:
    return page.data.get("status") == 200 and page.data.get("is_html", False)


def _noindex(page: Fact) -> bool:
    return "noindex" in page.data.get("meta_robots", "") or "noindex" in page.data.get(
        "x_robots_tag", ""
    )


def _count(n: int, one: str, many: str) -> str:
    return one if n == 1 else f"{n} {many}"


class TechnicalSeo:
    name = NAME

    def analyze(self, cfg: ClientConfig, facts: list[Fact]) -> list[Finding]:
        pages = _pages(facts)
        readable = [p for p in pages if _readable(p)]
        indexable = [p for p in readable if not _noindex(p)]
        site = next((f for f in facts if f.collector == "site-crawler" and f.kind == "site"), None)
        findings: list[Finding] = []

        def add(key, title, detail, how, evidence, impact, effort, owner, tech):
            if evidence or evidence is None:
                findings.append(Finding(NAME, key, title, detail, how, list(evidence or []),
                                        impact, effort, owner, tech))

        broken = [p for p in pages if p.data.get("status", 200) >= 400 or p.data.get("status") == 0]
        add("broken-links", "לתקן קישורים שבורים",
            _count(len(broken), "קישור אחד מוביל", "קישורים מובילים") + " לדף שגיאה.",
            "לעדכן את הקישור לדף שקיים.",
            [f"{p.subject} (הקישור נמצא בדף {p.data.get('linked_from') or 'הבית'})"
             for p in broken],
            4, 2, WRITER, "קישורים שבורים (שגיאת 404)")

        blocked = [p for p in readable if _noindex(p)]
        add("noindex", "לחשוף דפים מוסתרים",
            _count(len(blocked), "דף אחד מוסתר", "דפים מוסתרים") + " מגוגל.",
            "לבטל את ההסתרה בדפים שצריכים להופיע בחיפוש.",
            [p.subject for p in blocked], 5, 1, BUILDER, "תג noindex")

        robots_blocked = [p for p in pages
                          if p.data.get("skipped") == "robots.txt"
                          or p.data.get("googlebot_allowed") is False]
        add("robots-blocked", "לפתוח דפים חסומים",
            _count(len(robots_blocked), "דף אחד חסום", "דפים חסומים") + " לגוגל.",
            "להסיר את החסימה, אם היא לא מכוונת.",
            [p.subject for p in robots_blocked], 5, 1, BUILDER, "חסימה בקובץ robots.txt")

        no_title = [p for p in indexable if not p.data.get("title")]
        add("missing-title", "להוסיף כותרות לדפים",
            _count(len(no_title), "דף אחד מופיע", "דפים מופיעים") + " בגוגל בלי כותרת.",
            "לכתוב לכל דף כותרת קצרה.",
            [p.subject for p in no_title], 4, 1, WRITER, "תג title חסר")

        no_desc = [p for p in indexable if not p.data.get("description")]
        add("missing-description", "להוסיף תיאורים לדפים",
            _count(len(no_desc), "דף אחד מופיע", "דפים מופיעים") + " בגוגל בלי תיאור.",
            "לכתוב לכל דף משפט שמסביר מה יש בו.",
            [p.subject for p in no_desc], 3, 1, WRITER, "תג meta description חסר")

        by_title: dict[str, list[str]] = defaultdict(list)
        for p in indexable:
            if p.data.get("title"):
                by_title[p.data["title"]].append(p.subject)
        duplicates = [f'"{t}": {", ".join(urls)}' for t, urls in by_title.items() if len(urls) > 1]
        add("duplicate-title", "לתת לכל דף כותרת משלו",
            "כמה דפים נושאים את אותה כותרת.",
            "לשנות את הכותרות כך שכל אחת תהיה שונה.",
            duplicates, 3, 2, WRITER, "תגי title כפולים")

        h1_issues = [f"{p.subject} ({len(p.data.get('h1', []))} כותרות ראשיות)"
                     for p in indexable if len(p.data.get("h1", [])) != 1]
        add("h1", "לסדר כותרות ראשיות",
            ("בדף אחד" if len(h1_issues) == 1 else f"ב-{len(h1_issues)} דפים")
            + " אין כותרת ראשית אחת.",
            "להשאיר כותרת ראשית אחת בכל דף.",
            h1_issues, 2, 2, BUILDER, "תג H1 חסר או כפול")

        slow = [f"{p.subject} ({p.data.get('seconds')} שניות)"
                for p in readable if p.data.get("seconds", 0) > cfg.slow_page_seconds]
        add("slow-pages", "לזרז דפים איטיים",
            _count(len(slow), "דף אחד נטען", "דפים נטענים") + " לאט.",
            "להקטין תמונות כבדות ולבדוק את האחסון.",
            slow, 3, 4, BUILDER, "זמן טעינה איטי")

        no_schema = [p.subject for p in indexable if not p.data.get("has_structured_data")]
        if indexable and len(no_schema) == len(indexable):
            add("structured-data", "להציג את העסק למנועים",
                "גוגל ו-AI לא מקבלים מהאתר את פרטי העסק.",
                "להוסיף לאתר את פרטי העסק בפורמט שמנועים קוראים.",
                no_schema[:5], 4, 3, BUILDER, "נתונים מובנים (schema, JSON-LD)")

        alt = [f"{p.subject} ({p.data['images_missing_alt']} תמונות)"
               for p in indexable if p.data.get("images_missing_alt")]
        add("image-alt", "להוסיף תיאור לתמונות",
            "יש תמונות שמנועי חיפוש לא מבינים.",
            "לכתוב לכל תמונה תיאור של כמה מילים.",
            alt, 2, 2, WRITER, "תכונת alt חסרה בתמונות")

        if site is not None:
            if not site.data.get("has_sitemap"):
                add("sitemap", "לשלוח לגוגל רשימת דפים",
                    "לאתר אין רשימת דפים למנועי חיפוש.",
                    "לייצר את הרשימה ולהגיש אותה לגוגל.",
                    None, 3, 1, BUILDER, "קובץ sitemap.xml חסר")
            blocked_bots = site.data.get("ai_crawlers_blocked") or []
            add("ai-crawlers-blocked", "להסיר חסימת AI",
                "האתר חוסם את מנועי ה-AI.",
                "להסיר את החסימה בהגדרות האתר.",
                blocked_bots, 5, 1, BUILDER, "חסימת סורקי AI בקובץ robots.txt")

        return sorted(findings, key=lambda f: (-f.score, -f.impact, f.key))
