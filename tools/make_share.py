"""Draws the picture shown when the site's link is shared (1200x630), from the site's own
fonts and wordmark. Run from the project folder; writes bvm/static/share.png."""
import pathlib

from playwright.sync_api import sync_playwright

root = pathlib.Path.cwd()
fonts = (root / "bvm/static/fonts").as_uri()
logo = (root / "bvm/static/hilite-logo.svg").read_text(encoding="utf-8")
logo = "\n".join(line for line in logo.splitlines() if "<rect" not in line).replace("#14201c", "#eef2ec")
html = f"""<!DOCTYPE html><html lang="he" dir="rtl"><head><meta charset="utf-8"><style>
@font-face {{ font-family: D; src: url({fonts}/frank-ruhl-libre-hebrew-500.woff2); unicode-range: U+0590-05FF, U+FB1D-FB4F; }}
@font-face {{ font-family: D; src: url({fonts}/frank-ruhl-libre-latin-500.woff2); }}
@font-face {{ font-family: S; src: url({fonts}/ibm-plex-sans-hebrew-hebrew-500.woff2); unicode-range: U+0590-05FF; }}
@font-face {{ font-family: S; src: url({fonts}/ibm-plex-sans-hebrew-latin-500.woff2); }}
html, body {{ margin: 0; }}
body {{ width: 1200px; height: 630px; background: #0f1a17; color: #eef2ec; box-sizing: border-box;
  padding: 64px 80px; display: flex; flex-direction: column; justify-content: space-between; }}
.logo svg {{ height: 56px; width: auto; display: block; }}
h1 {{ font: 500 104px/1.04 D; margin: 0; max-width: 9.5em; }}
mark {{ background: none; color: #14201c; position: relative; isolation: isolate; white-space: nowrap; }}
mark::before {{ content: ""; position: absolute; inset: 14% -10px 4%; background: #ffe24a; border-radius: 6px; z-index: -1; transform: rotate(-1deg); }}
p {{ font: 500 32px/1.3 S; margin: 0; color: #9daaa4; }}
</style></head><body>
<div class="logo" dir="ltr">{logo}</div>
<h1>גוגל ו-AI ממליצים על <mark>העסק שלך?</mark></h1>
<p>בדיקה קצרה. ציון אחד ושלוש משימות לתיקון.</p>
</body></html>"""
page_file = root / "bvm/static/_share.html"
page_file.write_text(html, encoding="utf-8")
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1200, "height": 630})
        page.goto(page_file.as_uri())
        page.wait_for_timeout(600)
        page.screenshot(path=str(root / "bvm/static/share.png"))
        browser.close()
finally:
    page_file.unlink()
print("ok")
