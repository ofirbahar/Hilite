"""Does the site say anything about what buyers ask?

A buyer's question is reduced to the words that carry its meaning, and each page records
which of them it holds. The comparison is deliberately generous: a word counts as found
when its core appears anywhere in the page, inside a longer word too. So "we did not find
an answer" is said only when most of a question's words appear nowhere on the site.
"""
from __future__ import annotations

import re

# Words that shape a question and say nothing about its subject.
FILLER = frozenset("""
מה מי איך איפה למה מתי כמה איזה איזו אילו האם הכי יותר פחות טוב טובה טובים טובות כדאי
צריך אפשר יש אין של עם על את זה זו זאת הוא היא הם אני אנחנו לי לנו שלי שלנו או גם רק כל
בין לפי אצל ליד כמו עוד כבר מאוד באזור בארץ בישראל מומלץ מומלצת מומלצים המלצה המלצות
the a an of in on at to for and or is are was be do does did can how what which who where
when why best good top near me my your with from about that this
""".split())
PREFIXES = "ושהבלכמ"   # Hebrew joins "and", "that", "the", "in", "to", "like", "from" to a word
MIN_CORE = 3
ANSWERED = 0.5         # at least this share of a question's words appears on the site


def _core(word: str) -> str:
    """The part of a word that survives its prefixes and a plural ending."""
    for _ in range(2):
        if len(word) > MIN_CORE and word[0] in PREFIXES:
            word = word[1:]
    for ending in ("ים", "ות"):
        if word.endswith(ending) and len(word) - 2 >= MIN_CORE:
            word = word[:-2]
    return word


def cores(question: str) -> list[str]:
    """The words of a question that carry its meaning, each reduced to its core."""
    found: list[str] = []
    for word in re.findall(r"[^\W\d_]+", (question or "").lower()):
        if word in FILLER or len(word) < MIN_CORE:
            continue
        core = _core(word)
        if core not in FILLER and len(core) >= MIN_CORE and core not in found:
            found.append(core)
    return found


def all_cores(questions: list[str]) -> frozenset[str]:
    return frozenset(core for question in questions for core in cores(question))


def held(text: str, look_for: frozenset[str]) -> list[str]:
    """Which of the words looked for appear in a page's text."""
    text = (text or "").lower()
    return sorted(core for core in look_for if core in text)


def unanswered(questions: list[str], pages_words: list[list[str]]) -> list[str]:
    """The questions most of whose words appear on none of the pages read."""
    on_site = {word for words in pages_words for word in words}
    missing = []
    for question in questions:
        words = cores(question)
        if len(words) < 2:
            continue                      # too little to go on; say nothing
        if sum(word in on_site for word in words) < len(words) * ANSWERED:
            missing.append(question)
    return missing
