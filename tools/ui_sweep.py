"""Opens every page of the running site at several widths and reports what is broken:
sideways scrolling, script errors, text that is hard to read against what is behind it,
things too small to tap, and text that runs out of its box."""
import json
import pathlib
import sys

from playwright.sync_api import sync_playwright

BASE = "http://localhost:8000"
WIDTHS = [320, 390, 768, 1024, 1440]
CHECK = r"""
() => {
  const out = {overflow: document.documentElement.scrollWidth > innerWidth + 1, lowContrast: [], smallTaps: [], clipped: [], offscreen: []};
  const lum = (r, g, b) => { const f = v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const parse = c => { const m = c.match(/rgba?\(([^)]+)\)/); if (!m) return null; const p = m[1].split(/[ ,\/]+/).map(Number); return {r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1}; };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) { const s = getComputedStyle(e); const c = parse(s.backgroundColor); if (c && c.a > 0.5) return c; if (s.backgroundImage.includes('gradient') && e.tagName === 'MARK') return {r: 255, g: 226, b: 74, a: 1}; } return {r: 15, g: 26, b: 23, a: 1}; };
  const seen = new Set();
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode; const el = node.parentElement;
    if (!node.textContent.trim() || !el || seen.has(el)) continue; seen.add(el);
    if (el.closest('script,style,noscript,[hidden]')) continue;
    const s = getComputedStyle(el); const box = el.getBoundingClientRect();
    if (s.visibility === 'hidden' || s.display === 'none' || box.width === 0 || box.height === 0) continue;
    if (el.closest('details:not([open]) > :not(summary)')) continue;
    let op = 1; for (let e = el; e; e = e.parentElement) op *= parseFloat(getComputedStyle(e).opacity);
    if (op < 0.4) continue;                                        // mid-animation or deliberately dimmed
    const fg = parse(s.color), bg = bgOf(el); if (!fg) continue;
    const marked = el.classList.contains('w') && el.classList.contains('on') && el.closest('em');
    const l1 = lum(fg.r, fg.g, fg.b), l2 = marked ? lum(255, 226, 74) : lum(bg.r, bg.g, bg.b);
    const ratio = (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05);
    if (ratio < 4.4) out.lowContrast.push({text: node.textContent.trim().slice(0, 30), ratio: +ratio.toFixed(2), cls: el.className || el.tagName});
    if (box.right > innerWidth + 2 || box.left < -2) out.offscreen.push({text: node.textContent.trim().slice(0, 30), left: Math.round(box.left), right: Math.round(box.right)});
    if (el.scrollWidth > el.clientWidth + 2 && s.overflow !== 'visible' && s.overflowX !== 'visible') out.clipped.push({text: node.textContent.trim().slice(0, 30), cls: el.className || el.tagName});
  }
  document.querySelectorAll('a, button, summary, input, textarea').forEach(el => {
    const box = el.getBoundingClientRect(); const s = getComputedStyle(el);
    if (box.width === 0 || s.display === 'none' || el.closest('[hidden]') || el.closest('details:not([open]) > :not(summary)')) return;
    if (s.display === 'inline' && el.closest('p, li, span')) return;                    // a link in running text
    if (box.height < 40) out.smallTaps.push({text: (el.textContent || el.placeholder || '').trim().slice(0, 24), h: Math.round(box.height), cls: el.className || el.tagName});
  });
  return out;
}
"""


def main():
    out_dir = pathlib.Path(sys.argv[1]); out_dir.mkdir(exist_ok=True)
    report_url = sys.argv[2] if len(sys.argv) > 2 else ""
    pages = [("home", "/"), ("check", "/check"), ("privacy", "/privacy"), ("missing", "/runs/999999")]
    if report_url:
        pages.append(("report", report_url))
    problems = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width in WIDTHS:
            page = browser.new_page(viewport={"width": width, "height": 900}, reduced_motion="reduce")
            errors = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "404" not in m.text else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            for name, path in pages:
                page.goto(BASE + path); page.wait_for_timeout(500)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)"); page.wait_for_timeout(250)
                if name in ("report", "home"):
                    page.evaluate("document.querySelectorAll('details').forEach(d => d.open = true)")
                    page.wait_for_timeout(350)
                found = page.evaluate(CHECK)
                found = {k: v for k, v in found.items() if v}
                if errors:
                    found["errors"] = list(errors); errors.clear()
                if found:
                    problems[f"{name}@{width}"] = found
                if width in (320, 390, 1440):
                    page.screenshot(path=str(out_dir / f"{name}-{width}.png"), full_page=True)
            # the form's error state
            page.goto(BASE + "/check"); page.fill("#site", "not an address"); page.click("form .btn"); page.wait_for_timeout(600)
            found = {k: v for k, v in page.evaluate(CHECK).items() if v}
            if found:
                problems[f"check-error@{width}"] = found
            if width == 390:
                page.screenshot(path=str(out_dir / "check-error-390.png"))
            page.close()
        browser.close()
    print(json.dumps(problems, ensure_ascii=False, indent=1))


main()
