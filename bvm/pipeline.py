"""The pipeline: prepare, collect, analyze, rank, report.

Parts never call one another. Collectors write facts to the notebook and
analyzers read them back. If one part fails, the others carry on and the
failure is shown in the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ai_providers import providers_from_env
from .analyzers.ai_gap import AiGap
from .analyzers.google_presence import GooglePresence
from .analyzers.technical_seo import TechnicalSeo
from .collectors.ai_visibility import AiVisibility
from .collectors.google_profile import profile_from_env
from .collectors.site_crawler import SiteCrawler
from .config import ClientConfig
from .models import Fact, Finding
from .scoring import SCORING, final_score
from .store import Store


@dataclass
class RunResult:
    run_id: int
    facts: list[Fact] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)


def default_collectors(cfg: ClientConfig, public_only: bool = False) -> list:
    collectors: list = [SiteCrawler(public_only=public_only, render=True)]
    google = profile_from_env()  # the Google profile check runs only when its key is set
    if google is not None:
        collectors.append(google)
    if cfg.buyer_questions:  # the AI check only runs when there are questions to ask
        collectors.append(AiVisibility(providers_from_env()))
    return collectors


def default_analyzers() -> list:
    return [TechnicalSeo(), GooglePresence(), AiGap()]


def run(cfg: ClientConfig, store: Store, collectors=None, analyzers=None,
        run_id: int | None = None) -> RunResult:
    collectors = default_collectors(cfg) if collectors is None else collectors
    analyzers = default_analyzers() if analyzers is None else analyzers
    if run_id is None:
        run_id = store.start_run(cfg.slug, cfg.to_dict())

    for collector in collectors:
        try:
            store.save_facts(run_id, cfg.slug, collector.collect(cfg))
        except Exception as exc:  # one part down must not stop the run
            store.save_failure(run_id, collector.name, f"{type(exc).__name__}: {exc}")

    facts = store.load_facts(run_id)
    findings: list[Finding] = []
    for analyzer in analyzers:
        try:
            findings.extend(analyzer.analyze(cfg, facts))
        except Exception as exc:
            store.save_failure(run_id, analyzer.name, f"{type(exc).__name__}: {exc}")

    findings.sort(key=lambda f: (-f.score, -f.impact, f.key))
    store.save_findings(run_id, cfg.slug, findings)
    failures = store.load_failures(run_id)
    store.finish_run(run_id, score=final_score(cfg, facts, findings, failures), scoring=SCORING)
    return RunResult(run_id, facts, findings, failures)
