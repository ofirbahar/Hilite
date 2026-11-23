"""google-presence: is the business's Google profile there, and is it filled in?

Reads the google-profile collector's fact. Says nothing when that check did not run.
"""
from __future__ import annotations

from ..config import ClientConfig
from ..models import Fact, Finding

NAME = "google-presence"
OWNER = "בעל העסק"
FEW_REVIEWS = 10
LOW_RATING = 4.0


def profile_fact(facts: list[Fact]) -> Fact | None:
    return next((f for f in facts if f.kind == "google_profile"), None)


class GooglePresence:
    name = NAME

    def analyze(self, cfg: ClientConfig, facts: list[Fact]) -> list[Finding]:
        fact = profile_fact(facts)
        if fact is None:
            return []
        p = fact.data
        if not p.get("found"):
            return [Finding(NAME, "google-missing", "לפתוח פרופיל עסק בגוגל",
                            "לא מצאנו את העסק בגוגל מפות.",
                            "לפתוח פרופיל עסק בגוגל, בחינם, ולאמת אותו.",
                            [fact.subject], 5, 2, OWNER, "לא נמצא ב-Google Places")]
        found: list[Finding] = []
        where = [p["maps_url"]] if p.get("maps_url") else [p.get("name") or fact.subject]

        def add(key, title, detail, how, impact, effort, tech):
            found.append(Finding(NAME, key, title, detail, how, where, impact, effort, OWNER, tech))

        if p.get("status", "").startswith("CLOSED"):
            add("google-closed", "לעדכן שהעסק פתוח", "בגוגל העסק מסומן כסגור.",
                "לעדכן את הסטטוס בפרופיל העסק בגוגל.", 5, 1, f"businessStatus: {p['status']}")
        if not p.get("has_hours"):
            add("google-hours", "להוסיף שעות פתיחה בגוגל", "בפרופיל בגוגל אין שעות פתיחה.",
                "להזין שעות פתיחה בפרופיל העסק.", 4, 1, "חסר regularOpeningHours")
        if not p.get("has_phone"):
            add("google-phone", "להוסיף טלפון בגוגל", "בפרופיל בגוגל אין מספר טלפון.",
                "להזין מספר טלפון בפרופיל העסק.", 3, 1, "חסר nationalPhoneNumber")
        if not p.get("has_photos"):
            add("google-photos", "להעלות תמונות לגוגל", "בפרופיל בגוגל אין תמונות של העסק.",
                "להעלות תמונות של המקום ושל המוצרים.", 3, 2, "אין photos בפרופיל")
        reviews = p.get("reviews") or 0
        if reviews < FEW_REVIEWS:
            add("google-reviews", "לאסוף ביקורות בגוגל", f"לעסק יש {reviews} ביקורות בגוגל.",
                "לבקש מלקוחות מרוצים לכתוב ביקורת.", 4, 3, f"userRatingCount: {reviews}")
        rating = p.get("rating")
        if reviews and rating is not None and rating < LOW_RATING:
            add("google-rating", "לשפר את הדירוג בגוגל", f"הדירוג בגוגל הוא {rating} מתוך 5.",
                "לענות לביקורות ולטפל במה שחוזר בהן.", 4, 4, f"rating: {rating}")
        return found
