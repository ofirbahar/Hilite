"""robots.txt, read the way Google documents it (RFC 9309).

Python's own parser takes the first rule that matches, in file order. Search engines take
the most specific one, so a file such as "Disallow: *" followed by "Allow: /he" means the
opposite to each. Findings about what Google and AI crawlers may read have to follow the
search engines' reading, so this module does:

- a crawler obeys the group that names it, and falls back to "*" only when none does
- among the rules that match an address the longest pattern wins; a tie goes to Allow
- "*" matches any run of characters and "$" anchors the end of the address
"""
from __future__ import annotations

import re
from urllib.parse import quote, unquote, urlparse


def looks_like_robots(text: str) -> bool:
    """True for a real rules file, False for an error page served at the same address."""
    return bool(re.search(r"(?im)^\s*(user-agent|disallow|allow|sitemap)\s*:", text or ""))


def _spelling(path: str) -> str:
    """One spelling for a path, so a rule written in Hebrew matches the encoded address."""
    return quote(unquote(path), safe="/*$?&=:@+,;!~'()-._")


class _Pattern:
    """One Allow or Disallow path. `match` says whether an address starts with it.

    The file comes from the site being checked, so it is matched by walking the address
    once. A regular expression built from it can be made to run for hours by a line such
    as "/*a*a*a*a*a*a*a*a$z", and one such site would then hold a scan for good.
    """

    def __init__(self, path: str):
        self.anchored = path.endswith("$")
        self.first, *self.rest = path.rstrip("$").split("*")

    def match(self, path: str) -> bool:
        if not path.startswith(self.first):
            return False
        if not self.rest:
            return not self.anchored or len(path) == len(self.first)
        at = len(self.first)
        for part in self.rest[:-1]:           # each piece at the first place it fits
            found = path.find(part, at)
            if found < 0:
                return False
            at = found + len(part)
        last = self.rest[-1]
        if self.anchored:
            return len(path) - len(last) >= at and path.endswith(last)
        return path.find(last, at) >= 0


class Rules:
    def __init__(self, text: str):
        self.groups: dict[str, list[tuple[bool, str, _Pattern]]] = {}
        self.sitemaps: list[str] = []
        agents: list[str] = []
        open_group = False            # still reading the "User-agent" lines of a group
        for raw in (text or "").splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            field, value = (part.strip() for part in line.split(":", 1))
            field = field.lower()
            if field == "user-agent":
                if not open_group:
                    agents = []
                open_group = True
                if value:             # a nameless group belongs to no crawler
                    agents.append(value.lower())
                    self.groups.setdefault(value.lower(), [])
            elif field in ("allow", "disallow"):
                open_group = False
                if value:             # an empty Disallow means "nothing is disallowed"
                    value = _spelling(value)
                    for agent in agents:
                        self.groups[agent].append((field == "allow", value, _Pattern(value)))
            elif field == "sitemap":
                self.sitemaps.append(value)

    def site_maps(self) -> list[str]:
        return list(self.sitemaps)

    def _group(self, user_agent: str) -> list:
        token = re.split(r"[/\s(]", user_agent.strip().lower(), maxsplit=1)[0]
        named = [agent for agent in self.groups if agent != "*" and agent in token]
        if named:
            return self.groups[max(named, key=len)]
        return self.groups.get("*", [])

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parsed = urlparse(url)
        path = _spelling((parsed.path or "/") + ("?" + parsed.query if parsed.query else ""))
        best_length, allowed = -1, True
        for allow, text, pattern in self._group(user_agent):
            if pattern.match(path):
                length = len(text)
                if length > best_length or (length == best_length and allow):
                    best_length, allowed = length, allow
        return allowed
