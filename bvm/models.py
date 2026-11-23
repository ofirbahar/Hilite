from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Fact:
    """One raw observation a collector brought back."""

    collector: str
    kind: str          # e.g. "page", "site"
    subject: str       # e.g. the URL the fact is about
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    """One problem or opportunity an analyzer decided matters."""

    analyzer: str
    key: str
    title: str
    detail: str
    how_to_fix: str
    evidence: list[str] = field(default_factory=list)
    impact: int = 3    # 1 (minor) .. 5 (critical)
    effort: int = 3    # 1 (minutes) .. 5 (weeks)
    owner: str = ""    # who usually does the fix, in the reader's words
    tech: str = ""     # the technical name of the problem, for whoever fixes it

    @property
    def score(self) -> int:
        # High impact first; among equals, the cheaper fix wins.
        return self.impact * 2 - self.effort
