"""page-experience: is the site fast and steady on a phone, by Google's own measurements?

Reads the page-speed collector's fact. Says nothing when that check did not run.

Two kinds of number arrive. What real visitors had over the last weeks is what Google
ranks by, so it is judged by Google's published limits. One test run on a simulated phone
moves from run to run, so on its own it is reported only when it is clearly poor: a
borderline result would come and go between two checks of an unchanged site.
"""
from __future__ import annotations

from ..config import ClientConfig
from ..models import Fact, Finding

NAME = "page-experience"
BUILDER = "בונה האתר"
# Google's limits: up to the first is good, past the second is poor.
LCP_MS = (2500, 4000)      # main content shown
CLS = (0.1, 0.25)          # how much the page jumps while loading
INP_MS = (200, 500)        # answer to a tap, from real visitors only
TBT_POOR_MS = 600          # the test run's stand-in for a slow answer to a tap


def speed_fact(facts: list[Fact]) -> Fact | None:
    return next((f for f in facts if f.kind == "page_speed"), None)


def _seconds(ms: float) -> str:
    return f"{ms / 1000:.1f}"


class PageExperience:
    name = NAME

    def analyze(self, cfg: ClientConfig, facts: list[Fact]) -> list[Finding]:
        fact = speed_fact(facts)
        if fact is None:
            return []
        data = fact.data
        visitors = {k: v for k, v in (data.get("visitors") or {}).items() if v is not None}
        found: list[Finding] = []

        def add(key, title, detail, how, impact, effort, tech):
            found.append(Finding(NAME, key, title, detail, how, [fact.subject],
                                 impact, effort, BUILDER, tech))

        def judge(real, test, limits):
            """(value, impact, where it came from) or None when there is nothing to say."""
            good, poor = limits
            if real is not None:
                return (real, 4 if real > poor else 3, "משתמשים אמיתיים") if real > good else None
            if test is not None and test > poor:
                return test, 3, "בדיקת מעבדה"
            return None

        slow = judge(visitors.get("lcp_ms"), data.get("lcp_ms"), LCP_MS)
        if slow:
            value, impact, source = slow
            add("mobile-slow", "לזרז את האתר בנייד",
                f"בנייד התוכן הראשי מופיע אחרי {_seconds(value)} שניות.",
                "להקטין תמונות, לדחות סקריפטים ולבדוק את האחסון.", impact, 4,
                f"LCP {_seconds(value)} שניות, {source} (גוגל: עד 2.5)")

        jumpy = judge(visitors.get("cls"), data.get("cls"), CLS)
        if jumpy:
            value, impact, source = jumpy
            add("layout-shift", "לייצב את הדף בטעינה",
                "חלקים בדף קופצים בזמן הטעינה בנייד.",
                "לקבוע מראש גודל לתמונות, לסרטונים ולמודעות.", impact - 1, 3,
                f"CLS {value:.2f}, {source} (גוגל: עד 0.1)")

        tap = visitors.get("inp_ms")
        if tap is not None and tap > INP_MS[0]:
            add("mobile-unresponsive", "לקצר את זמן התגובה בנייד",
                "הדף מגיב לאט ללחיצות בנייד.",
                "לצמצם סקריפטים כבדים שרצים בדף.", 3 if tap > INP_MS[1] else 2, 4,
                f"INP {round(tap)} אלפיות שנייה, משתמשים אמיתיים (גוגל: עד 200)")
        elif tap is None and (data.get("tbt_ms") or 0) > TBT_POOR_MS:
            add("mobile-unresponsive", "לקצר את זמן התגובה בנייד",
                "סקריפטים כבדים מעכבים את הדף בנייד.",
                "לצמצם סקריפטים כבדים שרצים בדף.", 2, 4,
                f"TBT {round(data['tbt_ms'])} אלפיות שנייה, בדיקת מעבדה")
        return found
