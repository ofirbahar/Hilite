"""Builds a sample report and home page from the fake website, with a stand-in AI assistant.

    python -m tests.demo OUT_DIR
"""
from __future__ import annotations

import re
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

from bvm.ai_providers import Answer
from bvm.collectors.ai_visibility import AiVisibility
from bvm.collectors.site_crawler import SiteCrawler
from bvm.store import Store
from bvm.web import create_app
from tests.conftest import Handler
from tests.test_ai_and_web import FakeAssistant

QUESTIONS = [
    "מה התוכנה הכי טובה לניהול מלאי לעסק קטן?",
    "איזו תוכנת מלאי מתחברת לחנות אונליין?",
    "תוכנת מלאי זולה למתחילים",
    "מה החלופות ל-Globex?",
]
ANSWERS = {
    QUESTIONS[0]: Answer("ההמלצות המובילות: Globex, Initech ואחריהן Acme.",
                         ["https://www.reviews.test/best", "https://blog.test/x"]),
    QUESTIONS[1]: Answer("Globex ו-Initech מתחברות שתיהן.", ["https://www.reviews.test/shops"]),
    QUESTIONS[2]: Answer("Globex מציעה תוכנית חינמית.", ["https://compare.test/cheap"]),
    QUESTIONS[3]: Answer("כדאי לבדוק את Initech.", ["https://www.reviews.test/alt"]),
}


# The demo is one file with no server behind it, so its fonts cannot come from /static.
FONTS = ('<link href="https://fonts.googleapis.com/css2?family=Heebo:wght@100..900&display=swap" '
         'rel="stylesheet">')

SCRIPT = """
<script>
(function () {
  var views = document.querySelectorAll('[data-view]');
  var timer = null;
  function show(name) {
    clearTimeout(timer);
    views.forEach(function (v) { v.hidden = v.dataset.view !== name; });
    window.scrollTo(0, 0);
  }
  function route() {
    var name = (location.hash || '#home').slice(1);
    show(['home', 'check', 'report'].indexOf(name) >= 0 ? name : 'home');
  }
  window.addEventListener('hashchange', route);
  document.querySelectorAll('form').forEach(function (form) { form.addEventListener('submit', function (event) {
    event.preventDefault();
    var name = document.getElementById('name').value.trim() || 'העסק שלך';
    var site = form.querySelector('[name="site"]').value.trim();
    document.querySelectorAll('[data-biz]').forEach(function (el) { el.textContent = name; });
    if (site) document.querySelectorAll('[data-site]').forEach(function (el) { el.textContent = site; });
    show('waiting');
    timer = setTimeout(function () { location.hash = 'report'; route(); }, 2200);
  }); });
  route();
})();
</script>
"""


def _body(html: str) -> str:
    """What a page shows under the product bar."""
    return html.split("</header>", 1)[1].split("</body>", 1)[0]


def main(out_dir: str) -> None:
    """Writes sample-report.html and site-demo.html (one clickable page with every screen)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{server.server_port}"
    db = out / "demo.sqlite3"
    db.unlink(missing_ok=True)
    app = create_app(
        str(db), allow_private=True, background=False,
        collectors=lambda cfg: [SiteCrawler(), AiVisibility([FakeAssistant("עוזר AI", ANSWERS)])],
    )
    client = app.test_client()
    landing = client.get("/").get_data(as_text=True)
    check = client.get("/check").get_data(as_text=True)
    client.post("/scan", data={"name": "Acme", "site": site, "competitors": "Globex, Initech",
                               "questions": "\n".join(QUESTIONS)})
    report = client.get("/runs/1").get_data(as_text=True).replace(site, "https://acme-demo.test")
    report = report.replace("נבדק ב-", "דוח לדוגמה עם נתונים מפוברקים. נבדק ב-")
    (out / "sample-report.html").write_text(report.replace('href="/check"', 'href="#"')
                                            .replace('href="/"', 'href="#"'), encoding="utf-8")

    store = Store(str(db))
    waiting_id = store.start_run("acme", {"name": "Acme", "site": "https://acme-demo.test"})
    store.close()
    waiting = client.get(f"/runs/{waiting_id}").get_data(as_text=True)

    head = landing.split("</head>", 1)[0]
    # The demo is a file with no address of its own, so it names none to search engines.
    head = re.sub(r'<link rel="canonical"[^>]*>\s*|<meta property="og:url"[^>]*>\s*'
                  r'|<script type="application/ld\+json">.*?</script>', "", head, flags=re.S)
    bar = landing.split("<body>", 1)[1].split("</header>", 1)[0] + "</header>"
    report = re.sub(r'<section class="after">.*?</section>', "", report, flags=re.S)
    report_body = (_body(report)
                   .replace('<span class="who">Acme</span>', '<span class="who" data-biz>Acme</span>')
                   .replace("<bdi>https://acme-demo.test</bdi>", "<bdi data-site>https://acme-demo.test</bdi>", 1)
                   .replace("<span>Acme</span>", "<span data-biz>Acme</span>")
                   .replace('<div class="ring"', '<span class="site">הדגמה. הציון והמשימות הם נתוני דוגמה.</span>\n    <div class="ring"', 1))
    # The demo has no server to ask, so the waiting screen's polling script is left out.
    waiting_body = re.sub(r"<script[^>]*>.*?</script>", "", _body(waiting), flags=re.S)
    waiting_body = waiting_body.replace("בודקים את Acme", 'בודקים את <span data-biz>Acme</span>')
    # Nothing is scanned here, and every visitor gets the same sample report. Say so where
    # it cannot be missed, so the fixed score is never taken for a real result.
    note = ('<p role="note" style="margin:0;padding:12px 20px;background:var(--mark);color:var(--ink);'
            'font-weight:600;text-align:center">זו הדגמה בלבד. שום אתר לא נסרק כאן, והציון והמשימות '
            'קבועים לכל עסק.</p>')
    page = (head + "</head>\n<body>\n" + bar
            + '\n<div data-view="home">' + _body(landing) + "</div>"
            + '\n<div data-view="check" hidden>' + note + _body(check) + "</div>"
            + '\n<div data-view="waiting" hidden>' + note + waiting_body + "</div>"
            + '\n<div data-view="report" hidden>' + note + report_body + "</div>"
            + SCRIPT + "</body>\n</html>\n")
    page = page.replace('href="/check"', 'href="#check"').replace('href="/"', 'href="#home"')
    page = page.replace('href="/privacy"', 'href="#home"')
    page = page.replace('<link rel="stylesheet" href="/static/fonts/fonts.css">', FONTS)
    page = re.sub(r'<link rel="preload"[^>]*>\n?', "", page)      # the demo has no /static folder
    (out / "site-demo.html").write_text(page, encoding="utf-8")
    server.shutdown()
    db.unlink(missing_ok=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo-out")
