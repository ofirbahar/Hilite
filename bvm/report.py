"""The report for a business owner: one score, three tasks, everything else one tap away."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .analyzers.ai_gap import ai_answers, summarize
from .config import ClientConfig
from .models import Finding
from .pipeline import RunResult

TOP_N = 3
EFFORT_LABELS = {1: "5 דק׳", 2: "שעה", 3: "יום", 4: "כמה ימים", 5: "שבועות"}
PART_NAMES = {
    "site-crawler": "סריקת האתר",
    "ai-visibility": "בדיקת ה-AI",
    "technical-seo": "ניתוח האתר",
    "google-profile": "בדיקת הפרופיל בגוגל",
    "google-presence": "ניתוח הפרופיל בגוגל",
    "ai-gap": "ניתוח תשובות ה-AI",
    "pipeline": "הבדיקה",
}
# How many points one site problem costs, by its impact (5 = critical).
PENALTY = {5: 15, 4: 9, 3: 5, 2: 3, 1: 1}
# Whose findings count: what we measured on the web. A task whose key starts with "check-"
# is advice we could not verify, so it never costs points.
SCORED = ("technical-seo", "google-presence")
# A home page Google cannot show is never "good", however tidy the rest is.
CAP = 49
CAPPED = ("noindex", "home-closed-to-google")
# The site could not be read at all, so it has no site score.
NOT_READ = ("site-blocked", "site-unreachable", "site-moved")
# When one of these parts failed, the findings are incomplete and no score is honest.
CORE_PARTS = ("site-crawler", "technical-seo", "pipeline")

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


def breakdown(findings: list[Finding]) -> list[tuple[str, str, int]]:
    """(key, title, points lost) for every finding that counts, biggest first."""
    rows = [(f.key, f.title, PENALTY[f.impact]) for f in findings
            if f.analyzer in SCORED and not f.key.startswith("check-")]
    return sorted(rows, key=lambda row: (-row[2], row[0]))


def site_score(findings: list[Finding]) -> int:
    value = max(0, 100 - sum(points for _, _, points in breakdown(findings)))
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


def grade(value: int | None) -> tuple[str, str]:
    """(status, label) for a score."""
    if value is None:
        return "unknown", "לא נבדק"
    if value >= 80:
        return "good", "מצב טוב"
    if value >= 50:
        return "warn", "אפשר לשפר"
    return "bad", "דורש טיפול"


def ai_questions(result: RunResult) -> list[dict]:
    """Each question with who every assistant mentioned."""
    grouped: dict[str, list[dict]] = {}
    for fact in ai_answers(result.facts):
        grouped.setdefault(fact.subject, []).append({
            "assistant": fact.data.get("assistant", ""),
            "mentioned": bool(fact.data.get("mentioned")),
            "rivals": fact.data.get("competitors_mentioned", []),
        })
    return [{"question": q, "answers": rows} for q, rows in grouped.items()]


def render_report(cfg: ClientConfig, result: RunResult, today: date | None = None,
                  back_url: str = "", home_url: str = "") -> str:
    pages = [f for f in result.facts if f.collector == "site-crawler" and f.kind == "page"]
    # What was measured: pages read, a business page on a platform (its rules are always
    # readable), or a Google profile lookup. Any of them is enough for a score.
    scanned = sum(1 for p in pages if p.data.get("status") == 200 and p.data.get("is_html"))
    scanned += sum(1 for f in result.facts
                   if f.kind == "google_profile" or (f.kind == "site" and f.data.get("profile")))
    if any(f.key in NOT_READ for f in result.findings):
        scanned = 0
    ai = summarize(cfg, result.facts)
    cut_off = any(part in CORE_PARTS for part, _ in result.failures)
    value = None if cut_off else score(result.findings, ai, scanned)
    status, label = grade(value)
    failures = [(PART_NAMES.get(part, part), error.removeprefix("NotConfigured: "))
                for part, error in result.failures]
    for fact in result.facts:                      # assistants that answered only in part
        if fact.kind == "ai_errors":
            names = sorted({error.split(":")[0] for error in fact.data.get("errors", [])})
            failures.append((PART_NAMES["ai-visibility"],
                             f"{', '.join(names)} לא ענה על חלק מהשאלות."))
    return _env.get_template("report.html").render(
        client=cfg,
        today=(today or date.today()).strftime("%d.%m.%Y"),
        score=value,
        status=status,
        label=label,
        ai=ai,
        lost=breakdown(result.findings) if scanned else [],
        site_part=site_score(result.findings),
        capped=any(f.key in CAPPED for f in result.findings),
        scanned=scanned,
        questions=ai_questions(result),
        top=result.findings[:TOP_N],
        rest=result.findings[TOP_N:],
        failures=failures,
        effort_labels=EFFORT_LABELS,
        back_url=back_url,
        home_url=home_url,
    )


def write_report(cfg: ClientConfig, result: RunResult, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{cfg.slug}-{date.today().isoformat()}.html"
    path.write_text(render_report(cfg, result), encoding="utf-8")
    return path
