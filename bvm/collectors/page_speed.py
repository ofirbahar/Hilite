"""page-speed: how fast the site is on a phone, as Google measures it.

Google ranks pages partly by three measurements it calls Core Web Vitals: how long the
main content takes to show, how much the page jumps while it loads, and how long it takes
to answer a tap. Google's PageSpeed service returns them for any public address, both as
real visitors experienced them over the last weeks (when the site has enough visitors)
and as one test run on a simulated phone. Needs a key:

    GOOGLE_PAGESPEED_API_KEY   (PageSpeed Insights API; free within Google's quota)

The address that was entered is sent to Google, which then loads the page itself.
"""
from __future__ import annotations

import os

import requests

from ..config import ClientConfig
from ..models import Fact

NAME = "page-speed"
TIMEOUT = 90     # Google loads the page on a simulated phone before it answers


def _number(node: dict | None, key: str) -> float | None:
    value = (node or {}).get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class PageSpeed:
    name = NAME

    def __init__(self, key: str, base_url: str = "https://www.googleapis.com"):
        self.key, self.base_url = key, base_url.rstrip("/")

    def collect(self, cfg: ClientConfig) -> list[Fact]:
        if cfg.platform:
            return []           # a page on Facebook or Wolt is as fast as they made it
        address = cfg.site + (cfg.start_path or "/")
        response = requests.get(
            f"{self.base_url}/pagespeedonline/v5/runPagespeed",
            params={"url": address, "strategy": "mobile", "category": "performance",
                    "key": self.key},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        lab = body.get("lighthouseResult") or {}
        audits = lab.get("audits") or {}
        score = _number((lab.get("categories") or {}).get("performance"), "score")
        visitors = (body.get("loadingExperience") or {}).get("metrics") or {}
        shift = _number(visitors.get("CUMULATIVE_LAYOUT_SHIFT_SCORE"), "percentile")
        return [Fact(NAME, "page_speed", address, {
            "score": round(score * 100) if score is not None else None,
            # one test run on a simulated phone
            "lcp_ms": _number(audits.get("largest-contentful-paint"), "numericValue"),
            "cls": _number(audits.get("cumulative-layout-shift"), "numericValue"),
            "tbt_ms": _number(audits.get("total-blocking-time"), "numericValue"),
            # what real visitors had; empty for a site with too few of them
            "visitors": {
                "lcp_ms": _number(visitors.get("LARGEST_CONTENTFUL_PAINT_MS"), "percentile"),
                "inp_ms": _number(visitors.get("INTERACTION_TO_NEXT_PAINT"), "percentile"),
                "cls": shift / 100 if shift is not None else None,   # Google sends it times 100
            },
        })]


def page_speed_from_env(env: dict | None = None) -> PageSpeed | None:
    env = os.environ if env is None else env
    key = env.get("GOOGLE_PAGESPEED_API_KEY")
    return PageSpeed(key, env.get("BVM_PAGESPEED_URL") or "https://www.googleapis.com") if key else None
