"""The report for a business owner: one score, three tasks, everything else one tap away."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .analyzers.ai_gap import ai_answers, summarize
from .config import ClientConfig
from .models import Finding
from .pipeline import RunResult
from .scoring import (CAP, CAPPED, CORE_PARTS, NOT_READ, PENALTY, SCORED,  # noqa: F401
                      breakdown, final_score, grade, measured, parts, points, score,
                      site_score)

TOP_N = 3
PART_NAMES = {
    "site-crawler": "סריקת האתר",
    "ai-visibility": "בדיקת ה-AI",
    "technical-seo": "ניתוח האתר",
    "google-profile": "בדיקת הפרופיל בגוגל",
    "google-presence": "ניתוח הפרופיל בגוגל",
    "ai-gap": "ניתוח תשובות ה-AI",
    "pipeline": "הבדיקה",
}
_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)


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


def changes(previous: dict | None, value: int | None, findings: list[Finding],
            measured_now: set[str]) -> dict | None:
    """What moved since the last check of this business: the score, what was fixed, what is new.

    `previous` is {"date", "score", "findings", "parts"}. Nothing is said when either check
    has no score, or when the two did not measure the same things (see scoring.parts): a
    task that is missing because its check did not run this time was not fixed.
    """
    if previous is None or value is None or previous.get("score") is None:
        return None
    if previous.get("parts") != measured_now:
        return None
    now = {f.key for f in findings}
    before = {f.key for f in previous["findings"]}
    return {
        "date": previous["date"],
        "before": previous["score"],
        "after": value,
        "delta": value - previous["score"],
        "fixed": [f.title for f in previous["findings"] if f.key not in now],
        "new": [f.title for f in findings if f.key not in before],
    }


def render_report(cfg: ClientConfig, result: RunResult, today: date | None = None,
                  back_url: str = "", home_url: str = "", previous: dict | None = None,
                  again_url: str = "", delete_url: str = "", share_url: str = "") -> str:
    """previous: the last check of the same business, for the "what changed" lines:
    {"date", "score", "findings", "parts"}.
    again_url, delete_url: where the report's two buttons post to; empty hides the button.
    share_url: the report's plain address, shown to the owner as the one to pass on."""
    scanned = measured(result.facts, result.findings)
    ai = summarize(cfg, result.facts)
    value = final_score(cfg, result.facts, result.findings, result.failures)
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
        extent={f.key: (f.affected, f.of) for f in result.findings if f.of},
        since=changes(previous, value, result.findings, parts(result.facts)),
        again_url=again_url,
        delete_url=delete_url,
        share_url=share_url,
        share_path=urlparse(share_url).path if share_url else "",
        site_part=site_score(result.findings),
        capped=any(f.key in CAPPED for f in result.findings),
        scanned=scanned,
        questions=ai_questions(result),
        top=result.findings[:TOP_N],
        rest=result.findings[TOP_N:],
        failures=failures,
        back_url=back_url,
        home_url=home_url,
    )


def write_report(cfg: ClientConfig, result: RunResult, out_dir: str | Path) -> Path:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{cfg.slug}-{date.today().isoformat()}.html"
    path.write_text(render_report(cfg, result), encoding="utf-8")
    return path
