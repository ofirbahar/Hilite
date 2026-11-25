"""robots.txt is read the way Google documents it, not the way Python's parser does."""
from __future__ import annotations

from bvm.robots import Rules, looks_like_robots


def rules(text: str) -> Rules:
    return Rules(text)


def test_the_most_specific_rule_wins_whatever_the_order():
    r = rules("User-agent: *\nDisallow: *\nAllow: /\nAllow: /he\nDisallow: /he/shop\n")
    assert r.can_fetch("AnyBot", "https://x.test/he/about")       # Allow: /he beats Disallow: *
    assert not r.can_fetch("AnyBot", "https://x.test/he/shop/1")  # the longer Disallow wins
    assert r.can_fetch("AnyBot", "https://x.test/")

    r = rules("User-agent: *\nAllow: /shop/public\nDisallow: /shop\n")
    assert r.can_fetch("b", "https://x.test/shop/public/1") and not r.can_fetch("b", "https://x.test/shop/x")


def test_a_tie_goes_to_allow_and_an_empty_disallow_allows_everything():
    assert rules("User-agent: *\nDisallow: /a\nAllow: /a\n").can_fetch("b", "https://x.test/a")
    assert rules("User-agent: *\nDisallow:\n").can_fetch("b", "https://x.test/anything")
    assert rules("").can_fetch("b", "https://x.test/anything")


def test_wildcards_and_end_anchor():
    r = rules("User-agent: *\nDisallow: /*.pdf$\nDisallow: /search?*q=\n")
    assert not r.can_fetch("b", "https://x.test/files/menu.pdf")
    assert r.can_fetch("b", "https://x.test/files/menu.pdf.html")
    assert not r.can_fetch("b", "https://x.test/search?lang=he&q=pizza")
    assert r.can_fetch("b", "https://x.test/search")


def test_a_hostile_rules_file_cannot_hold_a_scan():
    """A site's own robots.txt must not be able to keep the check busy for hours."""
    import time
    r = rules("User-agent: *\nDisallow: /" + "*a" * 40 + "$z\n")
    started = time.monotonic()
    assert r.can_fetch("b", "https://x.test/" + "a" * 5000)       # ends in "a", not in "z"
    assert time.monotonic() - started < 1
    # the same reading as before for patterns with several wildcards
    r = rules("User-agent: *\nDisallow: /a*b*c$\nDisallow: /x**y\nDisallow: /p*$\n")
    assert not r.can_fetch("b", "https://x.test/a1b2c") and r.can_fetch("b", "https://x.test/a1b2cd")
    assert r.can_fetch("b", "https://x.test/acb")                 # the pieces come in order
    assert not r.can_fetch("b", "https://x.test/abcbc")           # the end may be a later "c"
    assert not r.can_fetch("b", "https://x.test/xy") and not r.can_fetch("b", "https://x.test/x-y-")
    assert not r.can_fetch("b", "https://x.test/p") and not r.can_fetch("b", "https://x.test/pq")


def test_a_bot_follows_its_own_group_and_only_that_one():
    r = rules("User-agent: *\nDisallow: /private\n\nUser-agent: GPTBot\nUser-agent: ClaudeBot\n"
              "Disallow: /\n\nUser-agent: Googlebot\nAllow: /\n")
    assert not r.can_fetch("GPTBot", "https://x.test/") and not r.can_fetch("ClaudeBot", "https://x.test/")
    assert r.can_fetch("Googlebot", "https://x.test/private")     # its own group has no such rule
    assert not r.can_fetch("PerplexityBot", "https://x.test/private")
    assert r.can_fetch("PerplexityBot", "https://x.test/menu")
    # a product token is matched without its version or description
    assert not r.can_fetch("gptbot/1.2 (+https://openai.com/gptbot)", "https://x.test/")


def test_comments_case_and_sitemaps():
    r = rules("# hello\nUSER-AGENT: *   # everyone\ndisallow: /Admin  # keep out\n"
              "Sitemap: https://x.test/map.xml\nsitemap: https://x.test/news.xml\n")
    assert not r.can_fetch("b", "https://x.test/Admin/users")
    assert r.can_fetch("b", "https://x.test/admin/users")          # paths are case-sensitive
    assert r.site_maps() == ["https://x.test/map.xml", "https://x.test/news.xml"]


def test_an_error_page_is_not_a_rules_file():
    assert looks_like_robots("User-agent: *\nDisallow:\n")
    assert looks_like_robots("Sitemap: https://x.test/s.xml\n")
    assert not looks_like_robots("<html><body>Page not found</body></html>")
    assert not looks_like_robots("")
