"""The score: 100, minus points for every problem that was measured.

Kept apart from the report so the pipeline can save the score with the check, which is
what lets a later check say how much changed.
"""
from __future__ import annotations

from .analyzers.ai_gap import summarize
from .config import ClientConfig
from .models import Fact, Finding

# Raised whenever the formula changes. Two checks are compared only when they were scored
# the same way, so a change here is never shown to an owner as their own progress.
SCORING = 5
# How many points one site problem costs at most, by its impact (5 = critical).
PENALTY = {5: 15, 4: 9, 3: 5, 2: 3, 1: 1}
# A problem on a single page costs this share of the full points. The cost grows with the
# share of pages it touches and reaches the full points when half the site has it.
FLOOR, FULL_AT = 0.4, 0.5
# Whose findings count: what we measured on the web. A task whose key starts with "check-"
# is advice we could not verify, so it never costs points.
SCORED = ("technical-seo", "google-presence", "page-experience")
# A home page Google cannot show is never "good", however tidy the rest is.
CAP = 49
CAPPED = ("noindex", "home-closed-to-google")
# The site could not be read at all, so it has no site score.
NOT_READ = ("site-blocked", "site-unreachable", "site-moved")
# When one of these parts failed, the findings are incomplete and no score is honest.
CORE_PARTS = ("site-crawler", "technical-seo", "pipeline")


def points(finding: Finding) -> int:
    """What this finding costs. A finding that does not say how many pages it touches is
    about the whole site (or was saved before extents were recorded) and costs it all."""
    full = PENALTY[finding.impact]
    if not finding.of:
        return full
    share = min(1.0, finding.affected / finding.of / FULL_AT)
    return max(1, round(full * (FLOOR + (1 - FLOOR) * share)))


def breakdown(findings: list[Finding]) -> list[tuple[str, str, int]]:
    """(key, title, points lost) for every finding that counts, biggest first."""
    rows = [(f.key, f.title, points(f)) for f in findings
            if f.analyzer in SCORED and not f.key.startswith("check-")]
    return sorted(rows, key=lambda row: (-row[2], row[0]))


def site_score(findings: list[Finding]) -> int:
    value = max(0, 100 - sum(lost for _, _, lost in breakdown(findings)))
    return min(value, CAP) if any(f.key in CAPPED for f in findings) else value


def score(findings: list[Finding], ai: dict | None, scanned: int) -> int | None:
    """0 to 100. Half is the site's health, half is how often AI mentions the business.

    Without an AI check the score is the site's health alone. None when nothing was read.
    """
    if any(f.key in NOT_READ for f in findings):
        scanned = 0
    if not scanned and not ai:
        return None
    site = site_score(findings)
    if not ai:
        return site
    ai_part = round(100 * ai["mentioned"] / ai["total"])
    return round((site + ai_part) / 2) if scanned else ai_part


def measured(facts: list[Fact], findings: list[Finding]) -> int:
    """How many things were really measured: pages read, a business page on a platform
    (its rules are always readable), or a Google profile we are sure is the business's."""
    if any(f.key in NOT_READ for f in findings):
        return 0
    count = 0
    for fact in facts:
        data = fact.data
        if fact.collector == "site-crawler" and fact.kind == "page":
            count += data.get("status") == 200 and bool(data.get("is_html"))
        elif fact.kind == "site":
            count += bool(data.get("profile"))
        elif fact.kind == "google_profile":
            count += data.get("match") != "unsure"
    return count


def parts(facts: list[Fact]) -> set[str]:
    """Which of the measurements this check really has: "site", "google", "speed", "ai".

    Two checks are compared only when they have the same ones. Otherwise the score moves
    because of what was measured, not because of anything the owner did, and a task that
    was simply not measured again would look fixed.
    """
    have = set()
    for fact in facts:
        if fact.collector == "site-crawler" and fact.kind in ("page", "site"):
            have.add("site")
        elif fact.kind == "google_profile" and fact.data.get("match") != "unsure":
            have.add("google")
        elif fact.kind == "page_speed":
            have.add("speed")
        elif fact.collector == "ai-visibility" and fact.kind == "ai_answer":
            have.add("ai")
    return have


def final_score(cfg: ClientConfig, facts: list[Fact], findings: list[Finding],
                failures: list[tuple[str, str]]) -> int | None:
    """The number the report shows. None when the check was cut off or read nothing."""
    if any(part in CORE_PARTS for part, _ in failures):
        return None
    return score(findings, summarize(cfg, facts), measured(facts, findings))


def grade(value: int | None) -> tuple[str, str]:
    """(status, label) for a score."""
    if value is None:
        return "unknown", "לא נבדק"
    if value >= 80:
        return "good", "מצב טוב"
    if value >= 50:
        return "warn", "אפשר לשפר"
    return "bad", "דורש טיפול"
