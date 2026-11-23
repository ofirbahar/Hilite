"""google-profile: the business's profile on Google Maps, through Google's Places API.

Every local business can be found this way, whether or not it has a website, so this is
the check that works for a pizzeria with nothing but a Facebook page. Needs a key:

    GOOGLE_PLACES_API_KEY   (Places API (New), Text Search)

Each lookup costs money on the Google account that owns the key.
"""
from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

import requests

from ..config import ClientConfig
from ..models import Fact

NAME = "google-profile"
TIMEOUT = 20
FIELDS = ",".join("places." + field for field in (
    "displayName", "formattedAddress", "rating", "userRatingCount", "nationalPhoneNumber",
    "websiteUri", "businessStatus", "regularOpeningHours", "photos", "googleMapsUri"))


CANDIDATES = 5     # how many places Google is asked for; the price of the lookup is the same
ALIKE = 0.8        # two names this similar are one name with a typo or a different spelling


def _words(name: str) -> list[str]:
    return re.sub(r"[^\w\s]", " ", name.lower()).split()


def _same_name(wanted: str, found: str) -> int:
    """2: the same name. 1: one holds the other ("Roma" and "Pizza Roma Ltd"), or nearly
    the same spelling. 0: different names."""
    a, b = _words(wanted), _words(found)
    if not a or not b:
        return 0
    if a == b:
        return 2
    if set(a) <= set(b) or set(b) <= set(a):
        return 1
    return int(SequenceMatcher(None, " ".join(a), " ".join(b)).ratio() >= ALIKE)


def _address(url: str) -> tuple[str, str]:
    parts = urlparse(url if "//" in url else "//" + url)
    return (parts.hostname or "").lower().removeprefix("www."), parts.path.rstrip("/").lower()


def _same_site(cfg: ClientConfig, website: str) -> bool:
    """Is the website on the Google profile the address that was checked?"""
    if not website:
        return False
    host, path = _address(website)
    own_host, _ = _address(cfg.site)
    if host != own_host:
        return False
    # Thousands of businesses share facebook.com, so there the whole address must match.
    return not cfg.platform or path == _address(cfg.site + cfg.start_path)[1]


def best_match(cfg: ClientConfig, places: list[dict]) -> tuple[dict | None, str]:
    """The place that is this business, and why we think so: "site" or "name".

    Google returns whatever resembles the search, so its first hit can be a namesake in
    another town. A profile that links to the address we checked is certain. Otherwise the
    name has to match, and the city decides between places with the right name.
    (None, "") when nothing is clearly this business.
    """
    ranked = []
    for index, place in enumerate(places):
        site = _same_site(cfg, place.get("websiteUri", ""))
        name = _same_name(cfg.name, (place.get("displayName") or {}).get("text", ""))
        city = bool(cfg.city) and cfg.city.strip().lower() in place.get("formattedAddress", "").lower()
        if site or name:
            ranked.append(((site, bool(name), city, name, -index), place, "site" if site else "name"))
    if not ranked:
        return None, ""
    _, place, why = max(ranked, key=lambda row: row[0])
    return place, why


class GoogleProfile:
    name = NAME

    def __init__(self, key: str, base_url: str = "https://places.googleapis.com"):
        self.key, self.base_url = key, base_url.rstrip("/")

    def collect(self, cfg: ClientConfig) -> list[Fact]:
        response = requests.post(
            f"{self.base_url}/v1/places:searchText",
            headers={"X-Goog-Api-Key": self.key, "X-Goog-FieldMask": FIELDS,
                     "Content-Type": "application/json"},
            json={"textQuery": " ".join(part for part in (cfg.name, cfg.city) if part),
                  "regionCode": cfg.target_country, "languageCode": "he", "pageSize": CANDIDATES},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        places = response.json().get("places") or []
        if not places:
            return [Fact(NAME, "google_profile", cfg.name, {"found": False})]
        place, why = best_match(cfg, places)
        if place is None:
            # Google found something, but nothing we can call this business. Its first hit
            # is kept so the owner can look, and it is marked so that nothing is scored.
            place, why = places[0], "unsure"
        return [Fact(NAME, "google_profile", cfg.name, {
            "found": True,
            "match": why,
            "name": (place.get("displayName") or {}).get("text", ""),
            "address": place.get("formattedAddress", ""),
            "rating": place.get("rating"),
            "reviews": place.get("userRatingCount") or 0,
            "has_phone": bool(place.get("nationalPhoneNumber")),
            "has_hours": bool(place.get("regularOpeningHours")),
            "has_website": bool(place.get("websiteUri")),
            "has_photos": bool(place.get("photos")),
            "status": place.get("businessStatus", ""),
            "maps_url": place.get("googleMapsUri", ""),
        })]


def profile_from_env(env: dict | None = None) -> GoogleProfile | None:
    env = os.environ if env is None else env
    key = env.get("GOOGLE_PLACES_API_KEY")
    return GoogleProfile(key, env.get("BVM_PLACES_URL") or "https://places.googleapis.com") if key else None
