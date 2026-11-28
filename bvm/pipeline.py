"""The pipeline: prepare, collect, analyze, rank, report.

Parts never call one another. Collectors write facts to the notebook and
analyzers read them back. If one part fails, the others carry on and the
failure is shown in the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .ai_providers import providers_from_env
from .analyzers.ai_gap import AiGap
from .analyzers.buyer_questions import BuyerQuestions
from .analyzers.google_presence import GooglePresence
from .analyzers.page_experience import PageExperience
from .analyzers.technical_seo import TechnicalSeo
from .collectors.ai_visibility import AiVisibility
from .collectors.google_profile import profile_from_env
from .collectors.page_speed import page_speed_from_env
from .collectors.site_crawler import SiteCrawler
from .config import ClientConfig
from .models import Fact, Finding
from .naming import business_name
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
    speed = page_speed_from_env()  # so does the phone speed check
    if speed is not None:
        collectors.append(speed)
    if cfg.buyer_questions:  # the AI check only runs when there are questions to ask
        collectors.append(AiVisibility(providers_from_env()))
    return collectors


def default_analyzers() -> list:
    return [TechnicalSeo(), GooglePresence(), PageExperience(), BuyerQuestions(), AiGap()]


def _analyze(cfg: ClientConfig, store: Store, run_id: int, facts: list[Fact],
             analyzers: list) -> list[Finding]:
    findings: list[Finding] = []
    for analyzer in analyzers:
        try:
            findings.extend(analyzer.analyze(cfg, facts))
        except Exception as exc:
            store.save_failure(run_id, analyzer.name, f"{type(exc).__name__}: {exc}")
    findings.sort(key=lambda f: (-f.score, -f.impact, f.key))
    return findings


def rescore(store: Store) -> int:
    """Brings checks made under an older formula up to date, and says how many.

    Nothing is fetched again: the pages a check read are kept in the notebook, so its
    findings are worked out anew from them and the score follows. After this an old check
    can be compared with a new one. A check whose saved details cannot be read is left as
    it is.
    """
    done = 0
    for run_id in store.unscored(SCORING):
        run = store.get_run(run_id)
        try:
            cfg = ClientConfig.from_dict(run["config"])
        except TypeError:
            continue
        facts = store.load_facts(run_id)
        findings = _analyze(cfg, store, run_id, facts, default_analyzers())
        store.replace_findings(run_id, cfg.slug, findings)
        store.set_score(run_id, final_score(cfg, facts, findings, store.load_failures(run_id)),
                        SCORING)
        done += 1
    return done


def run(cfg: ClientConfig, store: Store, collectors=None, analyzers=None,
        run_id: int | None = None, on_step=None) -> RunResult:
    """on_step: called with a part's name as it starts, and with "analyze" before the
    findings are worked out, so a waiting screen can say what is happening now."""
    collectors = default_collectors(cfg) if collectors is None else collectors
    analyzers = default_analyzers() if analyzers is None else analyzers
    if run_id is None:
        run_id = store.start_run(cfg.slug, cfg.to_dict())

    for collector in collectors:
        if on_step:
            on_step(getattr(collector, "name", ""))
        try:
            store.save_facts(run_id, cfg.slug, collector.collect(cfg))
        except Exception as exc:  # one part down must not stop the run
            store.save_failure(run_id, collector.name, f"{type(exc).__name__}: {exc}")
        if not cfg.name:
            # The owner typed only an address. The site is read first, so its name is
            # known before the Google and AI checks, which look for the business by name.
            cfg.name = business_name(cfg, store.load_facts(run_id))
            cfg.name_guessed = bool(cfg.name)
            store.update_config(run_id, cfg.to_dict())

    if on_step:
        on_step("analyze")
    facts = store.load_facts(run_id)
    findings = _analyze(cfg, store, run_id, facts, analyzers)
    store.save_findings(run_id, cfg.slug, findings)
    failures = store.load_failures(run_id)
    store.finish_run(run_id, score=final_score(cfg, facts, findings, failures), scoring=SCORING)
    return RunResult(run_id, facts, findings, failures)
