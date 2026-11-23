from __future__ import annotations

import ipaddress
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml


class ConfigError(ValueError):
    pass


@dataclass
class ClientConfig:
    """The one settings file every part reads from."""

    name: str
    slug: str
    site: str
    aliases: list[str] = field(default_factory=list)
    competitors: list[str] = field(default_factory=list)
    target_country: str = "US"
    city: str = ""            # where the business is; narrows the Google profile lookup
    buyer_questions: list[str] = field(default_factory=list)
    approved_description: str = ""
    start_path: str = "/"     # where the scan begins, e.g. "/il/he/" for a site in a folder
    platform: str = ""        # set when the address is a page on someone else's platform
    max_pages: int = 50
    delay_seconds: float = 0.5
    slow_page_seconds: float = 2.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ClientConfig":
        return cls(**data)


def _names(items) -> list[str]:
    """Competitors may be written as plain names or as {name: ..., site: ...}."""
    names = []
    for item in items or []:
        name = item.get("name", "") if isinstance(item, dict) else str(item)
        if name.strip():
            names.append(name.strip())
    return names


def check_site_address(site: str) -> str:
    """Return the cleaned address, or raise ConfigError if it is not a full http(s) one."""
    site = (site or "").strip()
    parsed = urlparse(site)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConfigError(f"'site' must be a full http(s) address, got {site!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def split_site_address(site: str) -> tuple[str, str]:
    """What a person types, as (site, start path). "ikea.com/il/he" is a fine answer.

    A missing https:// is added. Raises ConfigError when no real host name is left.
    """
    site = (site or "").strip()
    if "://" not in site:
        site = "https://" + site
    parsed = urlparse(site)
    try:
        host, _ = parsed.hostname or "", parsed.port
    except ValueError:
        host = ""
    looks_real = "." in host or host == "localhost"
    if parsed.scheme not in ("http", "https") or not looks_real or " " in parsed.netloc:
        raise ConfigError(f"not a website address: {site!r}")
    # The query stays: on some platforms it is what names the page (profile.php?id=...).
    start = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    return f"{parsed.scheme}://{parsed.netloc}", start


# Places where a business without its own site keeps a page: host -> (name, path it must
# start with). A page there is the business's address; the rest of the host is not theirs.
PLATFORMS = {
    "facebook.com": ("פייסבוק", "/"),
    "instagram.com": ("אינסטגרם", "/"),
    "tiktok.com": ("טיקטוק", "/"),
    "linktr.ee": ("Linktree", "/"),
    "wolt.com": ("Wolt", "/"),
    "10bis.co.il": ("תן ביס", "/"),
    "mishloha.co.il": ("משלוחה", "/"),
    "easy.co.il": ("easy", "/"),
    "rest.co.il": ("rest", "/"),
    "tripadvisor.com": ("Tripadvisor", "/"),
    "tripadvisor.co.il": ("Tripadvisor", "/"),
    "google.com": ("גוגל מפות", "/maps"),
    "google.co.il": ("גוגל מפות", "/maps"),
    "maps.app.goo.gl": ("גוגל מפות", "/"),
    "g.page": ("גוגל מפות", "/"),
}


def platform_of(site: str, path: str) -> str:
    """The platform's name when the address is a business page on one, otherwise ""."""
    host = (urlparse(site).hostname or "").lower()
    for prefix in ("www.", "m."):
        host = host.removeprefix(prefix)
    if not (path or "").strip("/"):
        return ""                              # the platform's own front page
    for known, (name, must_start_with) in PLATFORMS.items():
        if (host == known or host.endswith("." + known)) and path.startswith(must_start_with):
            return name
    return ""


def is_public_address(site: str) -> bool:
    """True only if the host resolves to public internet addresses.

    The web interface scans whatever address a visitor types, so it must refuse
    addresses that point back into the server's own network.
    """
    host = urlparse(site).hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


def exists(site: str) -> bool:
    """Does the address lead anywhere at all? False for a name no server answers to,
    which for a business owner nearly always means a typing mistake."""
    try:
        return bool(socket.getaddrinfo(urlparse(site).hostname or "", None))
    except (socket.gaierror, UnicodeError):
        return False


def load_config(path: str | Path) -> ClientConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for required in ("name", "slug", "site"):
        if not raw.get(required):
            raise ConfigError(f"settings file {path} is missing '{required}'")
    site = check_site_address(raw["site"])
    crawl = raw.get("crawl") or {}
    return ClientConfig(
        name=raw["name"],
        slug=raw["slug"],
        site=site,
        aliases=list(raw.get("aliases") or []),
        competitors=_names(raw.get("competitors")),
        target_country=raw.get("target_country", "US"),
        city=str(raw.get("city") or "").strip(),
        buyer_questions=list(raw.get("buyer_questions") or []),
        approved_description=raw.get("approved_description", ""),
        start_path=split_site_address(raw["site"])[1],
        platform=platform_of(site, split_site_address(raw["site"])[1]),
        max_pages=int(crawl.get("max_pages", 50)),
        delay_seconds=float(crawl.get("delay_seconds", 0.5)),
        slow_page_seconds=float(crawl.get("slow_page_seconds", 2.0)),
    )
