"""The website: a landing page, the check form, the waiting screen and the report.

There are no user accounts yet, so run it on your own machine only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import deque
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, abort, g, redirect, render_template, request, send_from_directory,
                   url_for)
from markupsafe import escape
from werkzeug.exceptions import HTTPException

from .config import (ClientConfig, ConfigError, exists, is_public_address, platform_of,
                     split_site_address)
from .models import Fact, Finding
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
MAX_PER_VISITOR = 10         # checks one address on the internet may start...
VISITOR_WINDOW_SECONDS = 600  # ...in this long. Each check costs us time and may cost money.
MAX_SLUG = 80
MAX_FORM_BYTES = 64_000      # the form is a few short fields; nothing larger is read
# What a browser may load on our pages: only what we serve ourselves. The pages carry their
# styles inline, so those are allowed. A script runs only if it carries the mark made for
# this one answer ({nonce}), so text that came from a scanned site or from an AI assistant
# can never run as a script, even if it were ever shown unescaped.
POLICY = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
          "script-src 'self' 'nonce-{nonce}'; object-src 'none'; frame-ancestors 'none'; "
          "base-uri 'self'; form-action 'self'")
# The pages use none of these, so no script on them may ask for them.
NO_DEVICE_ACCESS = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
# Questions people ask before they try it. Every answer repeats what the site already
# says elsewhere. Shown on the home page and handed to search engines in the same words.
FAQ = [
    ("צריך להירשם?",
     "לא. אין הרשמה, ולא מבקשים אימייל, טלפון או סיסמה. מזינים כתובת ומקבלים דוח."),
    ("מי יכול לראות את הדוח?",
     "רק מי שיש לו אחד משני הקישורים של הדוח: הקישור האישי, או קישור לקריאה בלבד שאפשר "
     "להעביר הלאה. מהקישור האישי אפשר גם למחוק את הבדיקה."),
    ("איך מחושב הציון?",
     "מתחילים מ-100, וכל בעיה שנמדדה באתר מורידה נקודות לפי החומרה שלה. כשיש בדיקת AI, "
     "הציון הסופי הוא הממוצע של ציון האתר ושל חלק התשובות שמזכירות את העסק."),
]
DESCRIPTION = ("בדיקה קצרה: איך האתר שלך נראה לגוגל, ומה עוזרי ה-AI עונים כששואלים על התחום "
               "שלך. ציון אחד ושלוש משימות לתיקון.")
ERRORS = {
    400: ("הבקשה לא תקינה", "משהו בבקשה חסר או שגוי. אפשר להתחיל בדיקה חדשה."),
    403: ("אין הרשאה לפעולה הזו", "רק הקישור האישי של הבדיקה יכול לעשות את זה."),
    404: ("הדף לא נמצא", "ייתכן שהכתובת שגויה, שהבדיקה נמחקה, או שזה לא הקישור של הדוח."),
    405: ("אי אפשר לפתוח את הכתובת הזו כך", "הכתובת הזו פועלת רק מתוך כפתור באתר."),
    409: ("הבדיקה עדיין רצה", "אפשר למחוק אותה כשהיא מסתיימת."),
    429: ("יותר מדי בדיקות בזמן קצר", "אפשר לנסות שוב בעוד כמה דקות."),
    500: ("משהו השתבש", "התקלה אצלנו. אפשר לנסות שוב בעוד רגע."),
}


class HideKeys(logging.Filter):
    """Keeps the secrets in personal and read-only links out of the server's log."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        hidden = re.sub(r"\b(key|v)=[\w-]+", r"\1=…", message)
        if hidden != message:
            record.msg, record.args = hidden, ()
        return True


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
    slug = re.sub(r"[^a-z0-9]+", "-", (host + page).lower()).strip("-") or "site"
    if len(slug) > MAX_SLUG:       # a long page address: keep its start, tell the rest apart
        slug = slug[:MAX_SLUG - 13] + "-" + hashlib.sha1((host + page).encode()).hexdigest()[:12]
    return slug


def create_app(db_path: str = "bvm.sqlite3", allow_private: bool = False,
               background: bool = True, collectors=None, contact: str | None = None,
               public_url: str | None = None) -> Flask:
    """collectors: optional function (cfg) -> list, used by tests to avoid real requests.
    contact: the address people write to about their data, shown on the privacy page.
    Taken from HILITE_CONTACT when not given; the page shows none rather than a made-up one.
    public_url: the address the site is reached at (https://hilite.example), for the links
    search engines are given. Taken from HILITE_URL; without one, the address the visitor
    asked for is used.
    """
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_FORM_BYTES
    app.config["MAX_FORM_MEMORY_SIZE"] = MAX_FORM_BYTES
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400   # fonts and the logo: a day in the browser
    public_url = os.environ.get("HILITE_URL", "") if public_url is None else public_url
    public_url = public_url.strip().rstrip("/")
    # allow_private is development mode: scans of this machine are allowed, reports open by
    # their number and the form lists every check. For real visitors a report opens only
    # with one of its two secret links, and nobody's checks are listed.
    open_reports = allow_private
    visitors: dict[str, deque] = {}
    werkzeug_log = logging.getLogger("werkzeug")
    if not any(isinstance(f, HideKeys) for f in werkzeug_log.filters):
        werkzeug_log.addFilter(HideKeys())
    contact = (os.environ.get("HILITE_CONTACT", "") if contact is None else contact).strip()
    app.config["TEMPLATES_AUTO_RELOAD"] = True  # a template edit shows on the next request
    active: set[int] = set()  # scans this process is working on right now

    def store() -> Store:
        return Store(db_path)

    def nonce() -> str:
        """The mark this answer's own scripts carry (see POLICY). Made once per request."""
        if "nonce" not in g:
            g.nonce = secrets.token_urlsafe(16)
        return g.nonce

    @app.context_processor
    def for_every_page() -> dict:
        return {"nonce": nonce()}

    def absolute(path: str) -> str:
        """A full address of one of our own pages, for search engines."""
        return (public_url or request.url_root.rstrip("/")) + path

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
            run_id = notebook.start_run(cfg.slug, cfg.to_dict(), key=key,
                                        view=secrets.token_urlsafe(18))
        finally:
            notebook.close()
        active.add(run_id)
        if background:
            threading.Thread(target=work, args=(run_id, cfg), daemon=True).start()
        else:
            work(run_id, cfg)
        return redirect(url_for("show_run", run_id=run_id, key=key))

    def form_page(form, error: str = "", status: int = 200, deleted: int = 0):
        """The check form. Everyone's checks are listed only in development; a real
        visitor sees the list their own browser keeps (see check.html)."""
        return render_template("check.html", runs=recent_runs() if open_reports else [],
                               own_list=not open_reports, form=form, error=error,
                               deleted=deleted), status

    def busy(form) -> tuple[str, int]:
        return form_page(form, "כבר רצות כמה בדיקות. נסו שוב בעוד דקה.", 429)

    def too_many() -> bool:
        """Has this visitor started more checks than one person needs? Counts this one."""
        if allow_private:
            return False
        now = time.monotonic()
        mine = visitors.setdefault(request.remote_addr or "?", deque())
        while mine and now - mine[0] > VISITOR_WINDOW_SECONDS:
            mine.popleft()
        if len(mine) >= MAX_PER_VISITOR:
            return True
        mine.append(now)
        if len(visitors) > 10_000:                     # forget visitors who went quiet
            for address in [a for a, times in visitors.items()
                            if not times or now - times[-1] > VISITOR_WINDOW_SECONDS]:
                del visitors[address]
        return False

    def limited(form) -> tuple[str, int]:
        return form_page(form, "התחלת הרבה בדיקות בזמן קצר. אפשר לנסות שוב בעוד כמה דקות.", 429)

    def _matches(given: str, secret: str | None) -> bool:
        return bool(secret) and hmac.compare_digest(given.encode(), secret.encode())

    def is_owner(run: dict) -> bool:
        return _matches(request.args.get("key", ""), run.get("key"))

    def can_read(run: dict) -> bool:
        return (open_reports or is_owner(run)
                or _matches(request.args.get("v", ""), run.get("view")))

    def link_of(run: dict) -> dict:
        """The secret this visitor came with, to carry on to the next address."""
        if is_owner(run):
            return {"key": run["key"]}
        return {"v": run["view"]} if _matches(request.args.get("v", ""), run.get("view")) else {}

    def as_form(cfg: ClientConfig) -> dict:
        return {"name": "" if cfg.name_guessed else cfg.name,
                "site": cfg.site + cfg.start_path.rstrip("/"), "city": cfg.city,
                "competitors": ", ".join(cfg.competitors),
                "questions": "\n".join(cfg.buyer_questions)}

    @app.after_request
    def protect(response):
        headers = response.headers
        # The owner's link holds their secret; it must not travel to any other site.
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("X-Frame-Options", "DENY")          # no "delete" button in a frame
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Content-Security-Policy", POLICY.format(nonce=nonce()))
        headers.setdefault("Permissions-Policy", NO_DEVICE_ACCESS)
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if request.is_secure:                                  # keep returning visitors on https
            headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        if request.path.startswith(("/runs", "/check", "/scan")):
            headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        if request.path.startswith("/runs"):
            headers["Cache-Control"] = "no-store"              # a report is one person's
        return response

    @app.errorhandler(HTTPException)
    def explain(error: HTTPException):
        title, text = ERRORS.get(error.code, ERRORS[400])
        return render_template("error.html", title=title, text=text), error.code

    @app.errorhandler(500)
    def crashed(error):
        title, text = ERRORS[500]
        return render_template("error.html", title=title, text=text), 500

    @app.get("/robots.txt")
    def robots():
        rules = ("User-agent: *\nDisallow: /runs/\nDisallow: /scan\nDisallow: /check\n"
                 f"\nSitemap: {absolute('/sitemap.xml')}\n")
        return rules, 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.get("/sitemap.xml")
    def sitemap():
        """The pages meant to be found. Reports and the form are private and are left out."""
        pages = "".join(f"<url><loc>{escape(absolute(path))}</loc></url>"
                        for path in ("/", "/privacy"))
        body = ('<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{pages}</urlset>\n')
        return body, 200, {"Content-Type": "application/xml; charset=utf-8"}

    @app.get("/llms.txt")
    def llms():
        """What the site is, in plain text, for AI assistants that read such a file."""
        return (render_template("llms.txt", home=absolute("/"), privacy=absolute("/privacy"),
                                check=absolute("/check"), faq=FAQ),
                200, {"Content-Type": "text/plain; charset=utf-8"})

    @app.get("/favicon.ico")
    def favicon():
        return send_from_directory(app.static_folder, "hilite-icon.svg", mimetype="image/svg+xml")

    @app.get("/")
    def home():
        address = absolute("/")
        # The same facts the page shows, in the form search engines and AI read.
        about = {"@context": "https://schema.org", "@graph": [
            {"@type": "WebSite", "@id": address + "#site", "url": address, "name": "Hilite",
             "inLanguage": "he", "description": DESCRIPTION},
            {"@type": "WebApplication", "@id": address + "#app", "name": "Hilite",
             "url": absolute("/check"), "applicationCategory": "BusinessApplication",
             "operatingSystem": "Web", "inLanguage": "he", "description": DESCRIPTION},
            {"@type": "FAQPage", "@id": address + "#faq", "inLanguage": "he", "mainEntity": [
                {"@type": "Question", "name": question,
                 "acceptedAnswer": {"@type": "Answer", "text": answer}}
                for question, answer in FAQ]}]}
        return render_template("landing.html", canonical=address, about=about, faq=FAQ,
                               description=DESCRIPTION,
                               share_image=absolute(url_for("static", filename="share.png")))

    @app.get("/privacy")
    def privacy():
        return render_template("privacy.html", contact=contact, canonical=absolute("/privacy"))

    @app.get("/sample")
    def sample():
        """A whole report to read before checking a site of one's own (tools/make_sample.py)."""
        data = json.loads((Path(__file__).parent / "sample.json").read_text(encoding="utf-8"))
        result = RunResult(0, [Fact(**f) for f in data["facts"]], [Finding(**f) for f in data["findings"]])
        return render_report(ClientConfig(**data["config"]), result, back_url=url_for("check"),
                             home_url=url_for("home"), nonce=nonce(), sample=True)

    @app.get("/check")
    def check():
        deleted = request.args.get("deleted", "")
        return form_page({}, deleted=int(deleted) if deleted.isdigit() else 0)

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
            return form_page(form, error, 400)
        if len(active) >= MAX_ACTIVE:
            return busy(form)
        if too_many():
            return limited(form)

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
        if run is None or not can_read(run):
            abort(404)                 # a stranger must not learn that the check exists
        cfg = ClientConfig.from_dict(run["config"])
        if not allow_private and not is_public_address(cfg.site):
            abort(400)
        if len(active) >= MAX_ACTIVE:
            return busy(as_form(cfg))
        if too_many():
            return limited(as_form(cfg))
        return begin(cfg)

    @app.post("/runs/<int:run_id>/delete")
    def delete_run(run_id: int):
        """Removes a check for good. Only the link the check was started from can do it."""
        own_form_only()
        notebook = store()
        try:
            run = notebook.get_run(run_id)
            if run is None or not can_read(run):
                abort(404)
            if not is_owner(run):
                abort(403)
            if run_id in active:     # the scan is still writing; deleting now would leave scraps
                abort(409)
            notebook.delete_run(run_id)
        finally:
            notebook.close()
        # The number tells the visitor's browser which entry of its own list to drop.
        return redirect(url_for("check", deleted=run_id))

    @app.get("/runs/<int:run_id>")
    def show_run(run_id: int):
        notebook = store()
        try:
            run = notebook.get_run(run_id)
            if run is None or not can_read(run):
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
            owner = is_owner(run)
            if owner and not run.get("view"):      # a check from before read-only links
                run["view"] = notebook.ensure_view(run_id)
        finally:
            notebook.close()
        day = _day(run["started_at"])
        return render_report(
            cfg, result, today=day, back_url=url_for("check"),
            home_url=url_for("home"), previous=earlier,
            again_url=url_for("check_again", run_id=run_id, **link_of(run)),
            delete_url=url_for("delete_run", run_id=run_id, key=run["key"]) if owner else "",
            share_url=(url_for("show_run", run_id=run_id, v=run["view"], _external=True)
                       if owner else ""),
            # The owner's browser keeps its own list of checks (see check.html).
            remember={"id": run_id, "name": cfg.name or cfg.site,
                      "date": day.strftime("%d.%m.%Y")} if owner and not open_reports else None,
            nonce=nonce())

    return app
