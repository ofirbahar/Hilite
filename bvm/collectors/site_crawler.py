"""site-crawler: visits the company's own website like a very fast, polite visitor.

It identifies itself honestly, obeys robots.txt, stays on the client's own host
and brings back raw facts only. Deciding what matters is the analyzers' job.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from ..config import ClientConfig, is_public_address
from ..models import Fact

NAME = "site-crawler"
USER_AGENT = "BrandVisibilityMonitor/0.1 (site audit bot)"
AI_CRAWLERS = ["GPTBot", "ClaudeBot", "PerplexityBot", "Google-Extended"]
TIMEOUT = 15
MAX_BYTES = 2_000_000      # never read more than this of one page
MAX_REDIRECTS = 5
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml")
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


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _same_host(url: str, root: str) -> bool:
    """example.com and www.example.com are the same site."""
    return _host(url) == _host(root)


def _clean(url: str) -> str:
    """One spelling per page: no #fragment, no tracking tags, lower-case host."""
    url, _ = urldefrag(url)
    p = urlparse(url)
    query = "&".join(part for part in p.query.split("&")
                     if part and not part.lower().startswith(TRACKING))
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.params, query, ""))


def _text(node) -> str:
    return node.get_text(" ", strip=True) if node else ""


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
        links.append(_clean(urljoin(url, href)))
    body_text = _text(soup.body) if soup.body else ""
    return {
        "title": _text(soup.title),
        "description": (description.get("content") or "").strip() if description else "",
        "h1": [_text(h) for h in soup.find_all("h1")],
        "canonical": (canonical.get("href") or "").strip() if canonical else "",
        "meta_robots": (robots.get("content") or "").lower() if robots else "",
        "lang": (soup.html.get("lang") or "") if soup.html else "",
        "has_structured_data": bool(soup.find("script", type="application/ld+json")),
        "images": len(images),
        "images_missing_alt": sum(1 for i in images if not (i.get("alt") or "").strip()),
        "word_count": len(body_text.split()),
        "links": sorted(set(links)),
    }


class SiteCrawler:
    name = NAME

    def __init__(self, session: requests.Session | None = None, public_only: bool = False):
        """public_only: refuse any address, including one a redirect leads to, that is
        not on the public internet. The web interface switches this on."""
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.public_only = public_only

    def _get(self, url: str) -> tuple[Fetched | None, float, str]:
        started = time.monotonic()
        try:
            fetched, error = self._fetch(url)
        except requests.RequestException as exc:
            fetched, error = None, type(exc).__name__
        return fetched, time.monotonic() - started, error

    def _fetch(self, url: str) -> tuple[Fetched | None, str]:
        """Follow redirects by hand so every hop is checked before it is requested."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if urlparse(current).scheme not in ("http", "https"):
                return None, "BlockedAddress"
            if self.public_only and not is_public_address(current):
                return None, "BlockedAddress"
            with self.session.get(current, timeout=TIMEOUT, allow_redirects=False,
                                  stream=True) as response:
                if response.is_redirect:
                    current = urljoin(current, response.headers["Location"])
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
                ), ""
        return None, "TooManyRedirects"

    def _is_file(self, url: str) -> bool:
        """True if the address answers with a real file, not an HTML "not found" page."""
        fetched, _, _ = self._get(url)
        return bool(fetched is not None and fetched.status == 200
                    and "html" not in fetched.content_type)

    def _site_facts(self, cfg: ClientConfig) -> tuple[Fact, RobotFileParser | None]:
        response, _, _ = self._get(cfg.site + "/robots.txt")
        rules = None
        has_robots = bool(response is not None and response.status == 200
                          and "html" not in response.content_type)
        if has_robots:
            rules = RobotFileParser()
            rules.parse(response.body.decode("utf-8", errors="replace").splitlines())
        # A sitemap named in robots.txt counts, whatever its file name.
        declared = bool(rules and rules.site_maps())
        data = {
            "has_robots_txt": has_robots,
            "has_sitemap": declared or any(self._is_file(cfg.site + p) for p in SITEMAP_PATHS),
            "ai_crawlers_blocked": [
                bot for bot in AI_CRAWLERS if rules and not rules.can_fetch(bot, cfg.site + "/")
            ],
        }
        return Fact(NAME, "site", cfg.site, data), rules

    def collect(self, cfg: ClientConfig) -> list[Fact]:
        site_fact, rules = self._site_facts(cfg)
        facts = [site_fact]
        start = _clean(cfg.site + "/")
        queue = deque([start])
        linked_from: dict[str, str] = {start: ""}
        seen = {start}

        def enqueue(link: str, source: str) -> None:
            if _same_host(link, cfg.site) and link not in seen:
                seen.add(link)
                linked_from[link] = source
                queue.append(link)

        while queue and len(facts) - 1 < cfg.max_pages:
            url = queue.popleft()
            if rules and not rules.can_fetch(USER_AGENT, url):
                facts.append(Fact(NAME, "page", url, {
                    "skipped": "robots.txt", "linked_from": linked_from[url]}))
                continue

            response, elapsed, error = self._get(url)
            data: dict = {
                "linked_from": linked_from[url],
                "seconds": round(elapsed, 3),
                "googlebot_allowed": rules.can_fetch("Googlebot", url) if rules else True,
            }
            if response is None:
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

            data["status"] = response.status
            data["x_robots_tag"] = response.x_robots_tag
            is_html = "html" in response.content_type
            data["is_html"] = is_html
            if response.status == 200 and is_html:
                data.update(parse_page(response.url, response.body, response.charset))
                for link in data["links"]:
                    enqueue(link, url)
            facts.append(Fact(NAME, "page", url, data))
            if cfg.delay_seconds:
                time.sleep(cfg.delay_seconds)

        return facts
