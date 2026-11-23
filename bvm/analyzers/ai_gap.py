"""ai-gap: where do AI assistants recommend competitors and not us, and whom do they trust?

Covers the guide's "gap" and "citation-source" questions for AI answers.
"""
from __future__ import annotations

from collections import Counter

from ..collectors.ai_visibility import domain_of
from ..config import ClientConfig
from ..models import Fact, Finding

NAME = "ai-gap"


def ai_answers(facts: list[Fact]) -> list[Fact]:
    return [f for f in facts if f.collector == "ai-visibility" and f.kind == "ai_answer"]


def summarize(cfg: ClientConfig, facts: list[Fact]) -> dict | None:
    """Numbers for the top of the report. None when no AI check ran."""
    answers = ai_answers(facts)
    if not answers:
        return None
    rivals = Counter()
    for a in answers:
        rivals.update(a.data.get("competitors_mentioned", []))
    return {
        "total": len(answers),
        "mentioned": sum(1 for a in answers if a.data.get("mentioned")),
        "competitors": [(name, rivals.get(name, 0)) for name in cfg.competitors],
        "assistants": sorted({a.data.get("assistant", "") for a in answers}),
    }


class AiGap:
    name = NAME

    def analyze(self, cfg: ClientConfig, facts: list[Fact]) -> list[Finding]:
        summary = summarize(cfg, facts)
        if summary is None:
            return []
        answers = ai_answers(facts)
        total, mentioned = summary["total"], summary["mentioned"]
        findings: list[Finding] = []

        if mentioned < total:
            share = mentioned / total
            findings.append(Finding(
                NAME, "ai-share", "להופיע בתשובות של AI",
                f"ה-AI מזכיר אותך ב-{mentioned} מתוך {total} תשובות.",
                "לפרסם באתר תשובות לשאלות שקונים שואלים.",
                [f"{a.subject} ({a.data.get('assistant')})"
                 for a in answers if not a.data.get("mentioned")],
                5 if share == 0 else 4 if share < 0.5 else 3, 4,
                "כותב התוכן", "נתח אזכורים בתשובות AI"))

        gaps = [a for a in answers
                if not a.data.get("mentioned") and a.data.get("competitors_mentioned")]
        if gaps:
            findings.append(Finding(
                NAME, "ai-competitor-gap", "לענות על שאלות של קונים",
                ("בתשובה אחת" if len(gaps) == 1 else f"ב-{len(gaps)} תשובות")
                + " ה-AI ממליץ רק על מתחרים.",
                "לכתוב דף באתר שעונה על כל שאלה.",
                [f"{a.subject} ({a.data.get('assistant')}): "
                 f"{', '.join(a.data['competitors_mentioned'])}" for a in gaps],
                4, 3, "כותב התוכן", "פער מול מתחרים בתשובות AI"))

        own = domain_of(cfg.site)
        cited = Counter()
        for a in answers:
            cited.update(d for d in a.data.get("cited_domains", []) if d != own)
        if cited:
            top = cited.most_common(8)
            findings.append(Finding(
                NAME, "ai-citation-sources", "להופיע באתרים שה-AI מצטט",
                "ה-AI בונה תשובות מכמה אתרים קבועים.",
                "לפנות לאתרים האלה ולבקש להיכלל בהם.",
                [f"{domain} ({count} תשובות)" for domain, count in top],
                3, 4, "השיווק", "מקורות מצוטטים בתשובות AI"))

        return findings
