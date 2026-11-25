"""buyer-questions: does the site say anything about what buyers ask?

An AI assistant can only name a business for a question when something it can read
connects the two. The owner's own site is the one place they control, so the questions
they entered are compared with the pages that were read (see bvm.questions).

The comparison is by words, not by meaning, so it cannot prove a question is unanswered.
The task is therefore one to look at: its key starts with "check-" and it costs no points.
"""
from __future__ import annotations

from ..config import ClientConfig
from ..models import Fact, Finding
from ..questions import unanswered

NAME = "buyer-questions"


class BuyerQuestions:
    name = NAME

    def analyze(self, cfg: ClientConfig, facts: list[Fact]) -> list[Finding]:
        questions = [q.strip() for q in cfg.buyer_questions if q.strip()]
        # A page on someone else's platform hands a reader like ours little of what it
        # shows a visitor, so nothing is said about what it lacks.
        if any(f.kind == "site" and f.data.get("profile") for f in facts):
            return []
        # Only pages read by a scan that looked for the words; older checks did not. A page
        # that stayed empty because no browser could open it was not really read.
        pages = [f.data["words_found"] for f in facts
                 if f.collector == "site-crawler" and f.kind == "page"
                 and f.data.get("status") == 200 and "words_found" in f.data
                 and not f.data.get("unverified")
                 and not (f.data.get("empty_without_browser") and not f.data.get("rendered"))]
        if not questions or not pages:
            return []
        missing = unanswered(questions, pages)
        if not missing:
            return []
        count = "לאחת מהשאלות" if len(missing) == 1 else f"ל-{len(missing)} מהשאלות"
        return [Finding(
            NAME, "check-questions-unanswered", "לוודא שהאתר עונה לקונים",
            f"לא מצאנו באתר תשובה {count} שהזנת.",
            "לכתוב לכל שאלה פסקה שעונה עליה, במילים של הלקוח.",
            missing, 3, 3, "כותב התוכן",
            f"מילות השאלה לא נמצאו ב-{len(pages)} הדפים שנקראו (לא נכלל בציון)")]
