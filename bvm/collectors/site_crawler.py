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
from ..robots import Rules, looks_like_robots

NAME = "site-crawler"
USER_AGENT = "BrandVisibilityMonitor/0.1 (site audit bot)"
AI_CRAWLERS = ["GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended"]
TIMEOUT = 15
MAX_BYTES = 8_000_000      # never read more than this of one page; shop pages run to megabytes
MAX_REDIRECTS = 5
MAX_RENDERED = 12          # pages opened in a real browser per scan; each takes seconds
MAX_WAIT_SECONDS = 10      # the longest we wait when a site asks us to slow down
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/wp-sitemap.xml")
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


def _meta(soup, prop: str) -> str:
    tag = soup.find("meta", attrs={"property": prop})
    return (tag.get("content") or "").strip() if tag else ""


def parse_page(url: str, html: str | bytes, charset: str | None = None) -> dict:
    """Pull the facts an editor would want out of one HTML page.

    Given bytes, the page's own <meta charset> is honoured when the server names none.
    """
    if isinstance(html, bytes):
        soup = BeautifulSoup(html, "lxml", from_encoding=charset)
    else:
        soup = BeautifulSoup(html, "lxml")
    description = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "description"})
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
    return {
        "has_phone": bool(dial_link or PHONE.search(body_text)),
        "has_hours": bool(CLOCK_TIME.search(body_text)),
        # The page's own title; an <svg><title> in the body is a picture's label.
        "title": _text(soup.head.find("title")) if soup.head else "",
        "description": (description.get("content") or "").strip() if description else "",
        "h1": [_text(h) for h in soup.find_all("h1")],
        # What the page calls itself, for naming the business when the owner did not.
        "site_name": _meta(soup, "og:site_name"),
        "og_title": _meta(soup, "og:title"),
        "data_name": _data_name(soup),
        "canonical": (canonical.get("href") or "").strip() if canonical else "",
        "meta_robots": (robots.get("content") or "").lower() if robots else "",
        "lang": (soup.html.get("lang") or "") if soup.html else "",
        "has_structured_data": bool(soup.find("script", type="application/ld+json")),
        "scripts": len(soup.find_all("script")),
        "images": len(images),
        # alt="" is the correct way to mark a decorative picture; only a missing alt is a gap.
        "images_missing_alt": sum(1 for i in images if i.get("alt") is None),
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
        self._rules_by_origin: dict[str, Rules | None] = {}

    def _render(self, url: str) -> str | None:
        """The page after its scripts ran, or None when no browser can be used."""
        if not self.render or self._rendered >= MAX_RENDERED or not render.available():
            return None
        if self._browser is None:
            try:
                self._browser = render.Browser(USER_AGENT, self.public_only).__enter__()
            except Exception:  # noqa: BLE001 - no browser on this machine: scan without one
                self.render = False
                return None
        self._rendered += 1
        return self._browser.html(url)

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
                        if len(body) >= MAX_BYTES:
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
        self._rules_by_origin = {}
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
                data.update(parse_page(response.url, response.body, response.charset))
                if is_shell(data):
                    data["empty_without_browser"] = True       # what a crawler gets
                    html = self._render(response.url)
                    if html:
                        in_browser = parse_page(response.url, html)
                        data.update(in_browser, rendered=True)
                        if is_shell(in_browser):
                            # Just as small in a real browser: a short page, not a hidden one.
                            data["small_page"] = True
                for link in data["links"]:
                    enqueue(link, url)
            facts.append(Fact(NAME, "page", url, data))
            if cfg.delay_seconds:
                time.sleep(cfg.delay_seconds)

        return facts
