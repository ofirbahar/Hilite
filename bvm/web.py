"""The website: a landing page, the check form, the waiting screen and the report.

There are no user accounts yet, so run it on your own machine only.
"""
from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
from datetime import date, datetime, timezone
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for

from .config import (ClientConfig, ConfigError, exists, is_public_address, platform_of,
                     split_site_address)
from .pipeline import RunResult, default_collectors
from .pipeline import run as run_pipeline
from .report import render_report
from .scoring import SCORING, parts
from .store import Store

MAX_COMPETITORS = 5
MAX_QUESTIONS = 10
DEFAULT_COUNTRY = "IL"  # the interface is in Hebrew, so buyers are assumed to be in Israel
# A scan nobody in this process is working on, and this old, was cut off by a restart.
STALE_SECONDS = 600
MAX_ACTIVE = 3   # scans at once; each one crawls, may open a browser and may cost money


def _age_seconds(started_at: str) -> float:
    return (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds()


def _day(started_at: str) -> date:
    """The day a check ran, on the clock of the machine that shows the report."""
    return datetime.fromisoformat(started_at).astimezone().date()


def _split(text: str, pattern: str, limit: int) -> list[str]:
    return [part.strip() for part in re.split(pattern, text or "") if part.strip()][:limit]


def _slug(site: str, page: str = "") -> str:
    """A short key for the business. `page` tells apart businesses that share a platform."""
    host = (urlparse(site).hostname or "site").removeprefix("www.")
    return re.sub(r"[^a-z0-9]+", "-", (host + page).lower()).strip("-") or "site"


def create_app(db_path: str = "bvm.sqlite3", allow_private: bool = False,
               background: bool = True, collectors=None, contact: str | None = None) -> Flask:
    """collectors: optional function (cfg) -> list, used by tests to avoid real requests.
    contact: the address people write to about their data, shown on the privacy page.
    Taken from HILITE_CONTACT when not given; the page shows none rather than a made-up one.
    """
    app = Flask(__name__)
    contact = (os.environ.get("HILITE_CONTACT", "") if contact is None else contact).strip()
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

    def own_form_only() -> None:
        """A form on some other site must not be able to act in the visitor's name."""
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)

    def begin(cfg: ClientConfig):
        """Starts a check and sends the visitor to it, on their own link.

        The link carries a secret made for this check. It is the only thing that allows
        deleting it, so a report can be shared by its plain address without that risk.
        """
        key = secrets.token_urlsafe(24)
        notebook = store()
        try:
            run_id = notebook.start_run(cfg.slug, cfg.to_dict(), key=key)
        finally:
            notebook.close()
        active.add(run_id)
        if background:
            threading.Thread(target=work, args=(run_id, cfg), daemon=True).start()
        else:
            work(run_id, cfg)
        return redirect(url_for("show_run", run_id=run_id, key=key))

    def busy(form) -> tuple[str, int]:
        return render_template("check.html", runs=recent_runs(), form=form,
                               error="כבר רצות כמה בדיקות. נסו שוב בעוד דקה."), 429

    def is_owner(run: dict) -> bool:
        given = request.args.get("key", "")
        return bool(run.get("key")) and hmac.compare_digest(given.encode(), run["key"].encode())

    @app.after_request
    def keep_links_private(response):
        # The owner's link holds their secret; it must not travel to any other site.
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.get("/")
    def home():
        return render_template("landing.html")

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html", contact=contact)

    @app.get("/check")
    def check():
        return render_template("check.html", runs=recent_runs(), error="", form={},
                               deleted="deleted" in request.args)

    @app.post("/scan")
    def scan():
        own_form_only()
        form = request.form
        name = form.get("name", "").strip()[:80]      # optional: read from the page if empty
        error = ""
        site, start_path = "", "/"
        try:
            site, start_path = split_site_address(form.get("site", ""))
        except ConfigError:
            error = "זו לא כתובת של אתר. כותבים אותה כך: www.example.co.il"
        if not error and not allow_private and not exists(site):
            error = "לא מצאנו אתר בכתובת הזו. כדאי לבדוק שאין בה שגיאת הקלדה."
        if not error and not allow_private and not is_public_address(site):
            error = "אי אפשר לסרוק את הכתובת הזו. צריך אתר ציבורי שזמין באינטרנט."
        if error:
            return render_template("check.html", runs=recent_runs(), error=error, form=form), 400
        if len(active) >= MAX_ACTIVE:
            return busy(form)

        platform = platform_of(site, start_path)
        cfg = ClientConfig(
            name=name, slug=_slug(site, start_path if platform else ""), site=site,
            start_path=start_path, platform=platform, city=form.get("city", "").strip()[:60],
            competitors=_split(form.get("competitors", ""), r"[\n,]", MAX_COMPETITORS),
            buyer_questions=_split(form.get("questions", ""), r"\n", MAX_QUESTIONS),
            target_country=DEFAULT_COUNTRY,
            max_pages=30,
        )
        return begin(cfg)

    @app.post("/runs/<int:run_id>/again")
    def check_again(run_id: int):
        """Checks the same business once more, with what was entered the first time."""
        own_form_only()
        notebook = store()
        try:
            run = notebook.get_run(run_id)
        finally:
            notebook.close()
        if run is None:
            abort(404)
        cfg = ClientConfig.from_dict(run["config"])
        if not allow_private and not is_public_address(cfg.site):
            abort(400)
        if len(active) >= MAX_ACTIVE:
            return busy({"name": cfg.name, "site": cfg.site + cfg.start_path.rstrip("/"),
                         "city": cfg.city, "competitors": ", ".join(cfg.competitors),
                         "questions": "\n".join(cfg.buyer_questions)})
        return begin(cfg)

    @app.post("/runs/<int:run_id>/delete")
    def delete_run(run_id: int):
        """Removes a check for good. Only the link the check was started from can do it."""
        own_form_only()
        notebook = store()
        try:
            run = notebook.get_run(run_id)
            if run is None:
                abort(404)
            if not is_owner(run):
                abort(403)
            if run_id in active:     # the scan is still writing; deleting now would leave scraps
                abort(409)
            notebook.delete_run(run_id)
        finally:
            notebook.close()
        return redirect(url_for("check", deleted=1))

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
            # Only a check scored by the same formula is compared, so a change we made to
            # the formula is never shown to the owner as their own progress.
            earlier = notebook.previous_run(run["client"], before=run_id, scoring=SCORING)
            if earlier is not None and run.get("scoring") == SCORING:
                earlier = {"date": _day(earlier["started_at"]).strftime("%d.%m.%Y"),
                           "score": earlier["score"],
                           "findings": notebook.load_findings(earlier["id"]),
                           "parts": parts(notebook.load_facts(earlier["id"]))}
            else:
                earlier = None
        finally:
            notebook.close()
        owner = is_owner(run)
        return render_report(
            cfg, result, today=_day(run["started_at"]), back_url=url_for("check"),
            home_url=url_for("home"), previous=earlier,
            again_url=url_for("check_again", run_id=run_id),
            delete_url=url_for("delete_run", run_id=run_id, key=run["key"]) if owner else "",
            share_url=url_for("show_run", run_id=run_id, _external=True) if owner else "")

    return app
