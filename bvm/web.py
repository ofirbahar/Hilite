"""The website: a landing page, the check form, the waiting screen and the report.

There are no user accounts yet, so run it on your own machine only.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for

from .config import (ClientConfig, ConfigError, is_public_address, platform_of,
                     split_site_address)
from .pipeline import RunResult, default_collectors
from .pipeline import run as run_pipeline
from .report import render_report
from .store import Store

MAX_COMPETITORS = 5
MAX_QUESTIONS = 10
DEFAULT_COUNTRY = "IL"  # the interface is in Hebrew, so buyers are assumed to be in Israel
# A scan nobody in this process is working on, and this old, was cut off by a restart.
STALE_SECONDS = 600


def _age_seconds(started_at: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds()


def _split(text: str, pattern: str, limit: int) -> list[str]:
    return [part.strip() for part in re.split(pattern, text or "") if part.strip()][:limit]


def _slug(site: str, page: str = "") -> str:
    """A short key for the business. `page` tells apart businesses that share a platform."""
    host = (urlparse(site).hostname or "site").removeprefix("www.")
    return re.sub(r"[^a-z0-9]+", "-", (host + page).lower()).strip("-") or "site"


def create_app(db_path: str = "bvm.sqlite3", allow_private: bool = False,
               background: bool = True, collectors=None) -> Flask:
    """collectors: optional function (cfg) -> list, used by tests to avoid real requests."""
    app = Flask(__name__)
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # a template edit shows on the next request
    active: set[int] = set()  # scans this process is working on right now

    def store() -> Store:
        return Store(db_path)

    def work(run_id: int, cfg: ClientConfig) -> None:
        notebook = store()
        try:
            # Outside development, the crawler re-checks every address and every redirect.
            parts = (collectors(cfg) if collectors
                     else default_collectors(cfg, public_only=not allow_private))
            run_pipeline(cfg, notebook, run_id=run_id, collectors=parts)
        except Exception as exc:  # never leave a scan stuck on "running"
            notebook.save_failure(run_id, "pipeline", f"{type(exc).__name__}: {exc}")
            notebook.finish_run(run_id, status="failed")
        finally:
            notebook.close()
            active.discard(run_id)

    def recent_runs() -> list[dict]:
        notebook = store()
        try:
            return notebook.list_runs()
        finally:
            notebook.close()

    @app.get("/")
    def home():
        return render_template("landing.html")

    @app.get("/check")
    def check():
        return render_template("check.html", runs=recent_runs(), error="", form={})

    @app.post("/scan")
    def scan():
        form = request.form
        name = form.get("name", "").strip()
        error = ""
        site, start_path = "", "/"
        try:
            site, start_path = split_site_address(form.get("site", ""))
        except ConfigError:
            error = "זו לא כתובת של אתר. כותבים אותה כך: www.example.co.il"
        if not error and not name:
            error = "חסר שם העסק"
        if not error and not allow_private and not is_public_address(site):
            error = "אי אפשר לסרוק את הכתובת הזו. צריך אתר ציבורי שזמין באינטרנט."
        if error:
            return render_template("check.html", runs=recent_runs(), error=error, form=form), 400

        platform = platform_of(site, start_path)
        cfg = ClientConfig(
            name=name, slug=_slug(site, start_path if platform else ""), site=site,
            start_path=start_path, platform=platform, city=form.get("city", "").strip()[:60],
            competitors=_split(form.get("competitors", ""), r"[\n,]", MAX_COMPETITORS),
            buyer_questions=_split(form.get("questions", ""), r"\n", MAX_QUESTIONS),
            target_country=DEFAULT_COUNTRY,
            max_pages=30,
        )
        notebook = store()
        try:
            run_id = notebook.start_run(cfg.slug, cfg.to_dict())
        finally:
            notebook.close()
        active.add(run_id)
        if background:
            threading.Thread(target=work, args=(run_id, cfg), daemon=True).start()
        else:
            work(run_id, cfg)
        return redirect(url_for("show_run", run_id=run_id))

    @app.get("/runs/<int:run_id>")
    def show_run(run_id: int):
        notebook = store()
        try:
            run = notebook.get_run(run_id)
            if run is None:
                abort(404)
            if (run["status"] == "running" and run_id not in active
                    and _age_seconds(run["started_at"]) > STALE_SECONDS):
                notebook.save_failure(run_id, "pipeline", "הבדיקה נקטעה לפני שהסתיימה")
                notebook.finish_run(run_id, status="failed")
                run["status"] = "failed"
            if run["status"] == "running":
                return render_template("waiting.html", run=run)
            cfg = ClientConfig.from_dict(run["config"])
            result = RunResult(run_id, notebook.load_facts(run_id),
                               notebook.load_findings(run_id), notebook.load_failures(run_id))
        finally:
            notebook.close()
        return render_report(cfg, result, back_url=url_for("check"), home_url=url_for("home"))

    return app
