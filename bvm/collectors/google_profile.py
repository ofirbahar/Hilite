"""google-profile: the business's profile on Google Maps, through Google's Places API.

Every local business can be found this way, whether or not it has a website, so this is
the check that works for a pizzeria with nothing but a Facebook page. Needs a key:

    GOOGLE_PLACES_API_KEY   (Places API (New), Text Search)

Each lookup costs money on the Google account that owns the key.
"""
from __future__ import annotations

import os

import requests

from ..config import ClientConfig
from ..models import Fact

NAME = "google-profile"
TIMEOUT = 20
FIELDS = ",".join("places." + field for field in (
    "displayName", "formattedAddress", "rating", "userRatingCount", "nationalPhoneNumber",
    "websiteUri", "businessStatus", "regularOpeningHours", "photos", "googleMapsUri"))


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
                  "regionCode": cfg.target_country, "languageCode": "he", "pageSize": 1},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        places = response.json().get("places") or []
        if not places:
            return [Fact(NAME, "google_profile", cfg.name, {"found": False})]
        place = places[0]
        return [Fact(NAME, "google_profile", cfg.name, {
            "found": True,
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
