"""ai-visibility: asks AI assistants the questions buyers would ask.

Brings back, for every question and assistant: the answer, whether the company
is mentioned, which competitors are, and which websites the assistant cited.
Each question costs money, so the number of questions per run is capped.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from ..config import ClientConfig
from ..models import Fact

NAME = "ai-visibility"
MAX_QUESTIONS = 25
URL_PATTERN = re.compile(r"https?://[^\s)\]>\"']+")
# Hebrew joins "and", "in", "to", "the"... to the next word: "באקמה", "ול-Acme".
HEBREW_PREFIX = r"(?:[ושהבלכמ]{1,3}[-־]?)?"


class NotConfigured(RuntimeError):
    pass


def first_mention(text: str, names: list[str]) -> int | None:
    """Character position of the earliest mention of any of the names, or None."""
    positions = []
    for name in names:
        name = name.strip()
        if not name:
            continue
        match = re.search(rf"(?<!\w){HEBREW_PREFIX}({re.escape(name)})(?!\w)", text,
                          flags=re.IGNORECASE)
        if match:
            positions.append(match.start(1))
    return min(positions) if positions else None


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


class AiVisibility:
    name = NAME

    def __init__(self, providers: list):
        self.providers = providers

    def collect(self, cfg: ClientConfig) -> list[Fact]:
        questions = [q.strip() for q in cfg.buyer_questions if q.strip()][:MAX_QUESTIONS]
        if not questions:
            return []
        if not self.providers:
            # Shown to the business owner in the report, so it is in their language.
            raise NotConfigured("במערכת הזו עוד לא הוגדר עוזר AI (חסר מפתח API).")

        own_names = [cfg.name, *cfg.aliases]
        facts: list[Fact] = []
        errors: list[str] = []
        for provider in self.providers:
            for question in questions:
                try:
                    answer = provider.ask(question, cfg.target_country)
                except Exception as exc:
                    errors.append(f"{provider.name}: {type(exc).__name__}")
                    continue
                own = first_mention(answer.text, own_names)
                rivals = {c: first_mention(answer.text, [c]) for c in cfg.competitors}
                mentioned_rivals = sorted((c for c, pos in rivals.items() if pos is not None),
                                          key=lambda c: rivals[c])
                order = sorted([pos for pos in rivals.values() if pos is not None]
                               + ([own] if own is not None else []))
                urls = [u.rstrip(".,;:!?*)]}>\"'") for u in
                        answer.citations or URL_PATTERN.findall(answer.text)]
                facts.append(Fact(NAME, "ai_answer", question, {
                    "assistant": provider.name,
                    "answer": answer.text,
                    "mentioned": own is not None,
                    # 1 = named before every competitor in this answer
                    "position": order.index(own) + 1 if own is not None else None,
                    "competitors_mentioned": mentioned_rivals,
                    "cited_domains": sorted({domain_of(u) for u in urls if domain_of(u)}),
                }))
        if errors and not facts:
            raise RuntimeError("every AI request failed: " + ", ".join(sorted(set(errors))))
        if errors:
            facts.append(Fact(NAME, "ai_errors", cfg.site, {"errors": errors}))
        return facts
