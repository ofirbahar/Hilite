"""Opens a page in a real, headless browser, for sites that are built in the browser.

Optional: without the `playwright` package (see requirements-render.txt) the scan still
runs and reports such pages as empty for crawlers. The browser keeps the scanner's own
name, obeys the same public-address rule as the crawler and never loads pictures.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .config import is_public_address

GOTO_TIMEOUT_MS = 20_000
SETTLE_TIMEOUT_MS = 6_000
SKIPPED_KINDS = ("image", "media", "font")   # not needed to read a page, and heavy


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


class Browser:
    """Use as a context manager, from one thread only (a Playwright rule)."""

    def __init__(self, user_agent: str, public_only: bool = False):
        self.user_agent, self.public_only = user_agent, public_only
        self._public_hosts: dict[str, bool] = {}
        self._playwright = self._browser = self._context = None

    def __enter__(self) -> "Browser":
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch()
            self._context = self._browser.new_context(user_agent=self.user_agent)
            self._context.route("**/*", self._guard)
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        for part in (self._context, self._browser):
            try:
                if part is not None:
                    part.close()
            except Exception:  # noqa: BLE001 - closing must never fail a scan
                pass
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = self._browser = self._context = None

    def _allowed(self, url: str) -> bool:
        if not self.public_only:
            return True
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return True                       # data: and blob: never leave the browser
        host = parsed.netloc
        if host not in self._public_hosts:
            self._public_hosts[host] = is_public_address(url)
        return self._public_hosts[host]

    def _guard(self, route) -> None:
        request = route.request
        if request.resource_type in SKIPPED_KINDS or not self._allowed(request.url):
            route.abort()
        else:
            route.continue_()

    def html(self, url: str) -> str | None:
        """The page as it stands after its scripts ran, or None if it could not be opened."""
        if not self._allowed(url):
            return None
        from playwright.sync_api import Error, TimeoutError

        page = self._context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            except TimeoutError:
                pass                          # a busy page: take what it shows by now
            return page.content()
        except Error:
            return None
        finally:
            page.close()
