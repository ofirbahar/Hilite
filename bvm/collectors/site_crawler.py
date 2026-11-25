"""site-crawler: visits the company's own website like a very fast, polite visitor.

It identifies itself honestly, obeys robots.txt, stays on the client's own host
and brings back raw facts only. Deciding what matters is the analyzers' job.
"""
from __future__ import annotations

import json
import re
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import quote, unquote, urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .. import render
from ..config import ClientConfig, is_public_address
from ..models import Fact
from ..questions import all_cores, held
from ..robots import Rules, looks_like_robots

NAME = "site-crawler"
USER_AGENT = "HiliteBot/1.0 (site check; identifies itself and obeys robots.txt)"
# The crawlers an AI assistant sends to find and read pages for its answers. A site that
# closes itself to these is left out of the answers.
AI_SEARCH = ["OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Perplexity-User",
             "Claude-SearchBot", "Claude-User"]
# The crawlers that collect text to train models on. Closing a site to these is a choice
# many owners make on purpose, and it does not keep the site out of the answers.
AI_TRAINING = ["GPTBot", "ClaudeBot", "Google-Extended"]
AI_CRAWLERS = AI_SEARCH + AI_TRAINING
TIMEOUT = 15
MAX_BYTES = 8_000_000      # never read more than this of one page; shop pages run to megabytes
MAX_READ_SECONDS = 30      # nor for longer: a server that sends a page drop by drop is cut off
MAX_REDIRECTS = 5
MAX_RENDERED = 20          # pages opened in a real browser per scan; each takes seconds
MAX_WAIT_SECONDS = 10      # the longest we wait when a site asks us to slow down
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml",
                 "/sitemap.xml.gz", "/sitemaps.xml", "/sitemap/sitemap.xml", "/sitemap/",
                 "/sitemap1.xml", "/post-sitemap.xml", "/page-sitemap.xml", "/sitemap.txt")
TRACKING = ("utm_", "fbclid=", "gclid=")  # the same page under a different address


@dataclass
class Fetched:
    """One answer from the site. The body is read only for text, and only up to MAX_BYTES."""

    status: int
    url: str                    # the address after following redirects
    content_type: str = ""
    x_robots_tag: str = ""
    body: bytes = b""
    charset: str | None = None  # only when the server states one
    redirected_to: str = ""     # set when the address sent us somewhere else
    refused: bool = False       # the redirect led to a page the host closes to crawlers
    seconds: float = 0.0        # how long the final answer took, without our own waits


DEFAULT_PORTS = {"http": 80, "https": 443}


def _netloc(parsed) -> str:
    """Host and port, without a port that only repeats the default one."""
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    return host if port in (None, DEFAULT_PORTS.get(parsed.scheme)) else f"{host}:{port}"


def _host(url: str) -> str:
    return _netloc(urlparse(url)).removeprefix("www.")


def _same_host(url: str, root: str) -> bool:
    """example.com and www.example.com are the same site."""
    return _host(url) == _host(root)


def _related(url: str, root: str) -> bool:
    """One host is a subdomain of the other, as with a shop that moved to shop.example.com."""
    a, b = _host(url), _host(root)
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def _clean(url: str) -> str:
    """One spelling per page: no #fragment, no tracking tags, lower-case host."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    query = "&".join(part for part in p.query.split("&")
                     if part and not part.lower().startswith(TRACKING))
    # /%d7%9e, /%D7%9E and the same letters typed out are one page.
    path = quote(unquote(p.path or "/"), safe="/:@!$&'()*+,;=-._~")
    return urlunparse((p.scheme.lower(), _netloc(p), path, p.params, query, ""))


# A phone number as people write it (03-555-1234, +972 50 555 1234) and a clock time (10:00).
PHONE = re.compile(
    r"(?<![\d-])(?:"
    r"\*\d{4}"                                              # *2700
    r"|\+\d{1,3}[-\s.]?(?:\(0\)[-\s.]?)?\d{1,3}(?:[-\s.]?\d{2,4}){2,3}"   # +972 (0)3 555 1234
    r"|\(?0\d{1,2}\)?[-\s.]?\d{3}[-\s.]?(?:\d{4}|\d{2}[-\s.]?\d{2})"     # (03) 555-1234
    r"|1[-\s]?[78]00[-\s]?\d{3}[-\s]?\d{3}"                   # 1-700-700-123
    r")(?![\d-])")
CLOCK_TIME = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?!\d)")


def is_shell(page: dict) -> bool:
    """A page that is all scripts and no content: it is built in the visitor's browser."""
    return (page.get("scripts", 0) >= 3 and page.get("word_count", 0) < 50
            and not page.get("links"))


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


# Structured data about these is about a page or a thing on it, not about the business.
NOT_A_BUSINESS = {"WebPage", "BreadcrumbList", "ListItem", "ImageObject", "SearchAction",
                  "Article", "BlogPosting", "NewsArticle", "Product", "Person", "FAQPage",
                  "Question", "Answer", "VideoObject", "Offer", "CollectionPage", "ItemList",
                  "SiteNavigationElement", "WPHeader", "WPFooter", "PostalAddress", "Review"}


def _data_name(soup) -> str:
    """The business's name from the details the page publishes for search engines."""
    business, website = "", ""

    def walk(node) -> None:
        nonlocal business, website
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kinds = node.get("@type") or []
            kinds = {kinds} if isinstance(kinds, str) else {k for k in kinds if isinstance(k, str)}
            name = node.get("name")
            if kinds and isinstance(name, str) and name.strip():
                if kinds == {"WebSite"}:
                    website = website or name.strip()
                elif not kinds & NOT_A_BUSINESS:
                    business = business or name.strip()
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(script.string or ""))
        except (ValueError, RecursionError):
            continue                    # one broken block must not hide the others
    return business or website


def _business_data(soup) -> dict:
    """What the page's JSON-LD says about the business itself: whether it gives a phone,
    an address and opening hours. Empty when the page carries no readable JSON-LD, so
    "it has none" is never confused with "we could not tell"."""
    has = {"phone": False, "address": False, "hours": False}
    read = False

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            kinds = node.get("@type") or []
            kinds = {kinds} if isinstance(kinds, str) else {k for k in kinds if isinstance(k, str)}
            if kinds and kinds != {"WebSite"} and not kinds & NOT_A_BUSINESS:
                has["phone"] |= bool(node.get("telephone"))
                has["address"] |= bool(node.get("address"))
                has["hours"] |= bool(node.get("openingHours")
                                     or node.get("openingHoursSpecification"))
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(script.string or ""))
            read = True
        except (ValueError, RecursionError):
            continue
    return has if read else {}


BOUND_ALT = (":alt", "v-bind:alt", "[alt]", "[attr.alt]", "x-bind:alt", "ng-attr-alt", "bind:alt")
# A page the browser was shown that is this much shorter than the one we read is not the
# same page: it is a "prove you are human" or "access denied" screen.
SAME_PAGE = 0.5
RENDER_SECONDS = 45    # all the browser time one scan may spend


ICON_PIXELS = 32
NOT_CONTENT = re.compile(r"loader|loading|spinner|spacer|blank\.|pixel|placeholder")


def _small(size) -> bool:
    digits = re.match(r"\s*(\d+)", str(size or ""))
    return bool(digits) and int(digits.group(1)) <= ICON_PIXELS


def _needs_description(img) -> bool:
    """A picture a visitor sees and that has no description. Pictures nobody sees (a
    tracking dot, a fallback inside <noscript>, a hidden or purely decorative image) are
    correct without one."""
    if img.get("alt") is not None or img.has_attr("hidden"):
        return False
    if any((img.get(name) or "").strip() for name in ("aria-label", "aria-labelledby", "title")):
        return False                    # described another way
    # A picture in a page template gets its description, and its file, when the page's
    # script fills it in: <img :alt="product.title" :src="product.image">.
    if any(img.has_attr(name) for name in BOUND_ALT):
        return False
    if not any(img.get(name) for name in ("src", "srcset", "data-src", "data-srcset", "data-lazy-src")):
        return False
    if img.find_parent(["noscript", "template"]) is not None:
        return False
    if (img.get("aria-hidden") or "").lower() == "true":
        return False
    if (img.get("role") or "").lower() in ("presentation", "none"):
        return False
    # A search-box magnifier or a menu arrow is part of the page's controls, not a picture
    # of the business, and a loading ring is not content.
    if _small(img.get("width")) or _small(img.get("height")):
        return False
    source = " ".join(str(img.get(name) or "") for name in ("src", "data-src", "class")).lower()
    if NOT_CONTENT.search(source):
        return False
    for node in (img, *img.parents):           # hidden itself, or inside something hidden
        style = (node.get("style") or "").replace(" ", "").lower()
        if ("display:none" in style or "visibility:hidden" in style or node.has_attr("hidden")
                or (node.get("aria-hidden") or "").lower() == "true"):
            return False
    return True


def _has_business_details(soup) -> bool:
    """Details for search engines, in any of the three formats they read: JSON-LD,
    microdata and RDFa."""
    if soup.find("script", type="application/ld+json"):
        return True
    if soup.find(attrs={"itemscope": True, "itemtype": lambda v: v and "schema.org" in v}):
        return True
    typed = soup.find(attrs={"typeof": True})
    if typed is None:
        return False
    return (bool(soup.find(attrs={"vocab": lambda v: v and "schema.org" in v}))
            or typed["typeof"].startswith("schema:"))


def _meta(soup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop})
    return (tag.get("content") or "").strip() if tag else ""


def parse_page(url: str, html: str | bytes, charset: str | None = None,
               look_for: frozenset[str] = frozenset()) -> dict:
    """Pull the facts an editor would want out of one HTML page.

    Given bytes, the page's own <meta charset> is honoured when the server names none.
    look_for: the words of the buyers' questions (see bvm.questions); the page records
    which of them it holds.
    """
    if isinstance(html, bytes):
        soup = BeautifulSoup(html, "lxml", from_encoding=charset)
    else:
        soup = BeautifulSoup(html, "lxml")
    description = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "description"})
    viewport = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "viewport"})
    robots = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "robots"})
    canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
    images = soup.find_all("img")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")) or not href:
            continue
        try:
            links.append(_clean(urljoin(url, href)))
        except ValueError:
            continue                    # a mistyped link must not cost the whole scan
    body_text = _text(soup.body) if soup.body else ""
    dial_link = soup.find("a", href=lambda v: v and v.strip().lower().startswith("tel:"))
    described = (description.get("content") or "").strip() if description else ""
    return {
        # Without this line a phone shows the page as a shrunken desktop page.
        "viewport": bool(viewport and (viewport.get("content") or "").strip()),
        "business": _business_data(soup),
        "words_found": held(" ".join((_text(soup.head.find("title")) if soup.head else "",
                                      described, body_text)), look_for),
        "has_phone": bool(dial_link or PHONE.search(body_text)),
        "has_hours": bool(CLOCK_TIME.search(body_text)),
        # The page's own title; an <svg><title> in the body is a picture's label.
        "title": _text(soup.head.find("title")) if soup.head else "",
        "description": described,
        "h1": [_text(h) for h in soup.find_all("h1")],
        # What the page calls itself, for naming the business when the owner did not.
        "site_name": _meta(soup, "og:site_name"),
        "og_title": _meta(soup, "og:title"),
        "data_name": _data_name(soup),
        "canonical": (canonical.get("href") or "").strip() if canonical else "",
        "meta_robots": (robots.get("content") or "").lower() if robots else "",
        "lang": (soup.html.get("lang") or "") if soup.html else "",
        "has_structured_data": _has_business_details(soup),
        "scripts": len(soup.find_all("script")),
        "images": len(images),
        # alt="" is the correct way to mark a decorative picture; only a missing alt is a gap.
        "images_missing_alt": sum(1 for i in images if _needs_description(i)),
        "word_count": len(body_text.split()),
        "links": sorted(set(links)),
    }


class SiteCrawler:
    name = NAME

    def __init__(self, session: requests.Session | None = None, public_only: bool = False,
                 render: bool = False):
        """public_only: refuse any address, including one a redirect leads to, that is
        not on the public internet. The web interface switches this on.
        render: open pages that are empty without a browser in a real one (needs Playwright)."""
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.public_only = public_only
        self.render = render
        self._browser = None
        self._rendered = 0
        self._render_seconds = 0.0
        self._rules_by_origin: dict[str, Rules | None] = {}
        self._look_for: frozenset[str] = frozenset()

    def _render(self, url: str) -> str | None:
        """The page after its scripts ran, or None when no browser can be used."""
        if (not self.render or self._rendered >= MAX_RENDERED or not render.available()
                or self._render_seconds >= RENDER_SECONDS):
            return None
        if self._browser is None:
            try:
                self._browser = render.Browser(USER_AGENT, self.public_only).__enter__()
            except Exception:  # noqa: BLE001 - no browser on this machine: scan without one
                self.render = False
                return None
        self._rendered += 1
        started = time.monotonic()
        try:
            return self._browser.html(url)
        finally:
            self._render_seconds += time.monotonic() - started

    def _confirm(self, url: str, data: dict) -> None:
        """Look at the page in a real browser before anything on it is called missing.

        Many sites add their description, main heading or business details with scripts.
        Google runs those scripts, so what counts is the page after they ran. When a
        browser exists but this page could not be opened in it, the page is marked
        unverified and its gaps are not reported: a guess is worse than silence.
        """
        html = self._render(url)
        if html is None:
            if self.render and render.available():
                data["unverified"] = True
            return
        seen = parse_page(url, html, look_for=self._look_for)
        if seen["word_count"] < data.get("word_count", 0) * SAME_PAGE:
            data["unverified"] = True       # the browser was turned away; this is not the page
            return
        data["viewport"] = data.get("viewport") or seen["viewport"]
        data["words_found"] = sorted({*data.get("words_found", []), *seen["words_found"]})
        if seen["business"]:                # scripts may add the business details too
            mine = data.get("business") or {}
            data["business"] = {key: bool(mine.get(key) or value)
                                for key, value in seen["business"].items()}
        for key in ("description", "h1", "site_name", "og_title", "data_name"):
            if not data.get(key) and seen.get(key):
                data[key] = seen[key]
        data["title"] = seen["title"] or data.get("title", "")
        data["has_structured_data"] = data.get("has_structured_data") or seen["has_structured_data"]
        data["images"], data["images_missing_alt"] = seen["images"], seen["images_missing_alt"]
        data["checked_in_browser"] = True

    def _confirm_titles(self, facts: list[Fact]) -> None:
        """Two pages that share a title as they arrive may get their own once scripts run."""
        by_title: dict[str, list[Fact]] = {}
        for fact in facts:
            if fact.kind == "page" and fact.data.get("status") == 200 and fact.data.get("title"):
                by_title.setdefault(fact.data["title"], []).append(fact)
        for same in by_title.values():
            if len(same) < 2:
                continue
            for fact in same:
                data = fact.data
                if not data.get("scripts") or data.get("checked_in_browser") or data.get("rendered"):
                    continue
                html = self._render(data.get("final_url") or fact.subject)
                if html is None:
                    if self.render and render.available():
                        data["title_unverified"] = True
                    continue
                seen = parse_page(fact.subject, html)
                if seen["word_count"] < data.get("word_count", 0) * SAME_PAGE:
                    data["title_unverified"] = True
                    continue
                data["title"] = seen["title"] or data["title"]
                data["checked_in_browser"] = True

    def _close_browser(self) -> None:
        if self._browser is not None:
            self._browser.__exit__()
            self._browser = None

    def _get(self, url: str, obey: bool = True) -> tuple[Fetched | None, float, str]:
        """(answer, seconds the answer took, error). obey=False is for robots.txt itself."""
        started = time.monotonic()
        try:
            fetched, error = self._fetch(url, obey)
        except (requests.RequestException, ValueError) as exc:
            fetched, error = None, type(exc).__name__
        seconds = fetched.seconds if fetched is not None else time.monotonic() - started
        return fetched, seconds, error

    def _may_fetch(self, url: str) -> bool:
        """What the host of this address asks of crawlers. Each host is asked once a scan."""
        parsed = urlparse(url)
        rules = self._rules(f"{parsed.scheme}://{parsed.netloc}")
        return rules is None or rules.can_fetch(USER_AGENT, url)

    def _fetch(self, url: str, obey: bool) -> tuple[Fetched | None, str]:
        """Follow redirects by hand so every hop is checked before it is requested."""
        current = url
        waited = False
        for _ in range(MAX_REDIRECTS + 2):
            if urlparse(current).scheme not in ("http", "https"):
                return None, "BlockedAddress"
            if self.public_only and not is_public_address(current):
                return None, "BlockedAddress"
            started = time.monotonic()
            with self.session.get(current, timeout=TIMEOUT, allow_redirects=False,
                                  stream=True) as response:
                if response.is_redirect:
                    target = response.headers["Location"]
                    try:        # servers send Hebrew addresses as raw UTF-8 bytes
                        target = target.encode("latin-1").decode("utf-8")
                    except UnicodeError:
                        pass
                    target = urljoin(current, target)
                    if obey and not self._may_fetch(target):
                        # The site closes that page to crawlers: stop here, do not read it.
                        return Fetched(status=response.status_code, url=current,
                                       redirected_to=target, refused=True), ""
                    current = target
                    continue
                pause = response.headers.get("Retry-After", "")
                if response.status_code in (429, 503) and pause.isdigit() and not waited:
                    waited = True                  # the site asked for a pause: give it one
                    time.sleep(min(int(pause), MAX_WAIT_SECONDS))
                    continue
                ctype = response.headers.get("Content-Type", "").lower()
                body = b""
                if any(kind in ctype for kind in ("html", "text", "xml")):
                    for chunk in response.iter_content(65536):
                        body += chunk
                        if len(body) >= MAX_BYTES or time.monotonic() - started > MAX_READ_SECONDS:
                            break
                return Fetched(
                    status=response.status_code, url=current, content_type=ctype,
                    x_robots_tag=response.headers.get("X-Robots-Tag", "").lower(),
                    body=body[:MAX_BYTES],
                    charset=response.encoding if "charset=" in ctype else None,
                    redirected_to="" if _clean(current) == _clean(url) else current,
                    seconds=time.monotonic() - started,
                ), ""
        return None, "TooManyRedirects"

    def _is_sitemap(self, url: str) -> bool:
        """True if the address holds a real sitemap. Many sites answer 200 to anything."""
        fetched, _, _ = self._get(url, obey=False)
        if fetched is None or fetched.status != 200:
            return False
        opening = fetched.body[:4000].lower()
        return b"<urlset" in opening or b"<sitemapindex" in opening

    def _rules(self, site: str) -> Rules | None:
        """What the host asks of crawlers, or None when it publishes no rules."""
        if site not in self._rules_by_origin:
            self._rules_by_origin[site] = None      # also stops a redirect loop asking again
            response, _, _ = self._get(site + "/robots.txt", obey=False)
            if response is not None and response.status == 200:
                text = response.body.decode("utf-8-sig", errors="replace")   # drops a BOM
                if looks_like_robots(text):
                    self._rules_by_origin[site] = Rules(text)
        return self._rules_by_origin[site]

    def _site_facts(self, site: str) -> tuple[Fact, Rules | None]:
        rules = self._rules(site)
        has_robots = rules is not None
        # A sitemap named in robots.txt counts, whatever its file name.
        declared = bool(rules and rules.site_maps())
        data = {
            "has_robots_txt": has_robots,
            "has_sitemap": declared or any(self._is_sitemap(site + p) for p in SITEMAP_PATHS),
            "ai_crawlers_blocked": [
                bot for bot in AI_CRAWLERS if rules and not rules.can_fetch(bot, site + "/")
            ],
        }
        return Fact(NAME, "site", site, data), rules

    def collect(self, cfg: ClientConfig) -> list[Fact]:
        try:
            return self._collect(cfg)
        finally:
            self._close_browser()

    def _collect_profile(self, cfg: ClientConfig) -> list[Fact]:
        """A business page on someone else's platform: read that one page and nothing else.

        The rest of the host belongs to other people, so no link is followed.
        """
        url = _clean(cfg.site + cfg.start_path)
        rules = self._rules(cfg.site)
        site_fact = Fact(NAME, "site", cfg.site, {
            "profile": cfg.platform, "has_robots_txt": rules is not None, "has_sitemap": False,
            # The platform's own rules, applied to this business's page.
            "ai_crawlers_blocked": [bot for bot in AI_CRAWLERS
                                    if rules and not rules.can_fetch(bot, url)]})
        data: dict = {"linked_from": "", "profile": cfg.platform}
        if rules and not rules.can_fetch(USER_AGENT, url):
            data["skipped"] = "robots.txt"
            return [site_fact, Fact(NAME, "page", url, data)]
        response, elapsed, error = self._get(url)
        data["seconds"] = round(elapsed, 3)
        if response is not None and response.refused:
            data["skipped"] = "robots.txt"            # a short link to a page closed to crawlers
        elif response is None:
            data.update(status=0, error=error)
        else:
            is_html = "html" in response.content_type
            data.update(status=response.status, final_url=response.url, is_html=is_html)
            if response.status == 200 and is_html:
                data.update(parse_page(response.url, response.body, response.charset))
                if is_shell(data):
                    html = self._render(response.url)
                    if html:
                        data.update(parse_page(response.url, html), rendered=True)
        return [site_fact, Fact(NAME, "page", url, data)]

    def _collect(self, cfg: ClientConfig) -> list[Fact]:
        self._rendered = 0
        self._render_seconds = 0.0
        self._rules_by_origin = {}
        self._look_for = all_cores(cfg.buyer_questions)
        if cfg.platform:
            return self._collect_profile(cfg)
        site = cfg.site
        start = _clean(site + (cfg.start_path or "/"))
        moved: list[Fact] = []
        # Where does the address really lead? A business that moved to its own subdomain
        # (example.com -> shop.example.com) is scanned there; anything else is not followed.
        site_fact, rules = self._site_facts(site)      # the site's rules come first
        landing = self._get(start) if not rules or rules.can_fetch(USER_AGENT, start) else None
        https_broken = False
        if (landing and landing[0] is None and landing[2] == "SSLError"
                and site.startswith("https://")):
            # The secure address does not hold (an expired or wrong certificate). Visitors
            # get a browser warning there. Many such sites still open without the lock, so
            # the check goes on at the plain address and the report names the broken lock.
            plain_site, plain_start = "http://" + site[8:], "http://" + start[8:]
            plain_fact, plain_rules = self._site_facts(plain_site)
            plain = (self._get(plain_start)
                     if not plain_rules or plain_rules.can_fetch(USER_AGENT, plain_start) else None)
            if plain and plain[0] is not None and not plain[0].redirected_to.startswith("https://"):
                site, start, landing = plain_site, plain_start, plain
                site_fact, rules, https_broken = plain_fact, plain_rules, True
        target = landing[0].redirected_to if landing and landing[0] is not None else ""
        if target and not _same_host(target, site) and _related(target, site):
            moved.append(Fact(NAME, "page", start, {
                "linked_from": "", "status": 301, "is_html": False, "redirected_to": target}))
            parsed = urlparse(target)
            site, start, landing = f"{parsed.scheme}://{parsed.netloc}", _clean(target), None
            site_fact, rules = self._site_facts(site)
        if https_broken:
            site_fact.data["https_broken"] = True
        confirmed_details = 0          # pages whose lack of business details was confirmed
        facts = [site_fact, *moved]
        read = 0                       # pages actually fetched; redirects and skips are free
        queue = deque([start])
        linked_from: dict[str, str] = {start: ""}
        seen = {start}

        def enqueue(link: str, source: str) -> None:
            if _same_host(link, site) and link not in seen:
                seen.add(link)
                linked_from[link] = source
                queue.append(link)

        while queue and read < cfg.max_pages and len(facts) <= cfg.max_pages * 3:
            url = queue.popleft()
            if rules and not rules.can_fetch(USER_AGENT, url):
                facts.append(Fact(NAME, "page", url, {
                    "skipped": "robots.txt", "linked_from": linked_from[url],
                    "googlebot_allowed": rules.can_fetch("Googlebot", url)}))
                continue

            if url == start and landing is not None:
                response, elapsed, error = landing        # already fetched above
            else:
                response, elapsed, error = self._get(url)
            if (response is not None and not response.redirected_to
                    and elapsed > cfg.slow_page_seconds):
                # One slow answer can be the network or a cold cache. Ask once more and
                # keep the faster of the two, so only a page that is slow twice is slow.
                again = self._get(url)
                if again[0] is not None:
                    elapsed = min(elapsed, again[1])
            data: dict = {
                "linked_from": linked_from[url],
                "seconds": round(elapsed, 3),
                "googlebot_allowed": rules.can_fetch("Googlebot", url) if rules else True,
            }
            if response is None:
                read += 1
                data.update(status=0, error=error)
                facts.append(Fact(NAME, "page", url, data))
                continue

            data["final_url"] = response.url
            if response.redirected_to:
                # The real page is read under its own address, so it is counted once.
                data.update(status=301, is_html=False, redirected_to=response.redirected_to)
                enqueue(_clean(response.redirected_to), linked_from[url] or url)
                facts.append(Fact(NAME, "page", url, data))
                continue

            read += 1
            data["status"] = response.status
            data["x_robots_tag"] = response.x_robots_tag
            is_html = "html" in response.content_type
            data["is_html"] = is_html
            if response.status == 200 and is_html:
                data.update(parse_page(response.url, response.body, response.charset,
                                       look_for=self._look_for))
                if is_shell(data):
                    data["empty_without_browser"] = True       # what a crawler gets
                    html = self._render(response.url)
                    if html:
                        in_browser = parse_page(response.url, html, look_for=self._look_for)
                        data.update(in_browser, rendered=True)
                        if is_shell(in_browser):
                            # Just as small in a real browser: a short page, not a hidden one.
                            data["small_page"] = True
                if read == 1 and "noindex" in data.get("meta_robots", ""):
                    # "Hidden from Google" on the home page is the gravest thing the report
                    # can say. Some sites send that mark only now and then, or only to
                    # readers that are not Google. It is said only if every answer has it.
                    for _ in range(2):
                        again = self._get(url)[0]
                        if (again is not None and again.status == 200 and "noindex" not in
                                parse_page(again.url, again.body, again.charset)["meta_robots"]):
                            data["noindex_unsure"] = True
                            break
                if data.get("empty_without_browser"):
                    pass                                       # already opened in a browser
                elif data["scripts"]:
                    no_details = not data["has_structured_data"] and confirmed_details < 3
                    if not (data["title"] and data["description"] and data["h1"]) or no_details:
                        confirmed_details += no_details
                        self._confirm(response.url, data)
                for link in data["links"]:
                    enqueue(link, url)
            facts.append(Fact(NAME, "page", url, data))
            if cfg.delay_seconds:
                time.sleep(cfg.delay_seconds)

        self._confirm_titles(facts)
        return facts
