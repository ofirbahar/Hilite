"""The business's name, read from its own page, so the owner only types an address.

Tried in order, most trustworthy first: the name the site gives itself, the business
details it publishes for search engines, the part of the page title that is a name, and
last the address. A page on a platform (Facebook, Wolt, Google Maps) is named by its own
title with the platform's name taken off, or by its address when the page cannot be read.
"""
from __future__ import annotations

import re
from urllib.parse import unquote, unquote_plus, urlparse

from .config import ClientConfig
from .models import Fact

MAX_LENGTH = 60        # longer than this is a sentence, not a name
SEPARATOR = re.compile(r"\s+[|\-–—·•:]\s+|\s*[|•]\s*")
# Title parts that say where you are, not who the business is.
GENERIC = re.compile(r"^(דף הבית|עמוד הבית|עמוד ראשי|דף ראשי|ראשי|בית|home|homepage|home page|"
                     r"welcome|index|main)$|^ברוכים הבאים|^welcome to", re.IGNORECASE)
# Words that mark a title part as the platform's own, or as its sign-in page.
PLATFORM_WORDS = re.compile(
    r"facebook|instagram|tiktok|linktree|wolt|tripadvisor|google|10bis|mishloha|easy\.co|rest\.co|"
    r"פייסבוק|אינסטגרם|טיקטוק|תן ביס|משלוחה|גוגל|log ?in|sign ?up|sign ?in|התחבר|הרשמ", re.IGNORECASE)
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "muni", "k12"}


def _tidy(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" \t\n-–—|·•:")
    return text if 0 < len(text) <= MAX_LENGTH else ""


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _domain_label(site: str) -> str:
    """"https://www.roma-pizza.co.il" -> "roma-pizza"."""
    labels = (urlparse(site).hostname or "").lower().removeprefix("www.").split(".")
    if len(labels) > 1:
        labels = labels[:-1]
    if len(labels) > 1 and labels[-1] in SECOND_LEVEL:
        labels = labels[:-1]
    return labels[-1] if labels else ""


def _from_domain(site: str) -> str:
    return " ".join(word.capitalize() for word in re.split(r"[-_]+", _domain_label(site)) if word)


def _from_title(title: str, site: str) -> str:
    parts = [part for part in (_tidy(p) for p in SEPARATOR.split(title or "")) if part]
    domain = _letters(_domain_label(site))
    for part in parts:                       # the part that spells the address is the name
        letters = _letters(part)
        if len(letters) >= 3 and domain and (letters == domain or letters in domain
                                             or domain in letters):
            return part
    parts = [part for part in parts if not GENERIC.search(part)]
    return min(parts, key=len) if parts else ""


def _from_platform_title(title: str) -> str:
    parts = [re.sub(r"\s*\(@[^)]*\)", "", part) for part in SEPARATOR.split(title or "")]
    parts = [part for part in (_tidy(p) for p in parts) if part and not PLATFORM_WORDS.search(part)]
    return parts[0] if parts else ""


def _from_platform_address(path: str) -> str:
    place = re.search(r"/maps/place/([^/@]+)", path)
    if place:
        return _tidy(unquote_plus(place.group(1)))
    last = unquote(path.split("?")[0].rstrip("/").rsplit("/", 1)[-1])
    if not last or "." in last and last.rsplit(".", 1)[-1] in ("php", "html", "aspx"):
        return ""                            # profile.php?id=123 names nobody
    # Wolt and the like write "pizza-roma"; a Facebook handle is kept as its owner wrote it.
    return _tidy(last.replace("-", " ").replace("_", " "))


def business_name(cfg: ClientConfig, facts: list[Fact]) -> str:
    """The best name we can give the business from what was read. "" only when even the
    address says nothing (a numbered profile page that could not be opened)."""
    page = next((f.data for f in facts if f.collector == "site-crawler" and f.kind == "page"
                 and f.data.get("status") == 200 and "title" in f.data), {})
    if cfg.platform:
        return (_from_platform_title(page.get("og_title", ""))
                or _from_platform_title(page.get("title", ""))
                or _from_platform_address(cfg.start_path))
    # Sites often put a slogan next to their name ("Roma - order online"), in any of these.
    host = _letters((urlparse(cfg.site).hostname or "").removeprefix("www."))
    for source in ("site_name", "data_name", "title"):
        name = _from_title(page.get(source, ""), cfg.site)
        if name and _letters(name) != host:       # "roma.co.il" as a title is an address
            return name
    return _from_domain(cfg.site)
