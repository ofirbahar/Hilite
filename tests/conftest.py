"""A small fake website served from this machine, with known problems planted in it."""
from __future__ import annotations

import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bvm.config import ClientConfig

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

HOME = """<html lang="en"><head><meta name="viewport" content="width=device-width"><title>Acme Home</title>
<meta name="description" content="Acme makes widgets."></head>
<body><h1>Acme</h1>
<a href="/about">About</a> <a href="/pricing">Pricing</a> <a href="/missing">Old page</a>
<a href="/slow">Slow</a> <a href="/private/notes">Private</a>
<a href="https://other.example/">Elsewhere</a> <a href="mailto:hi@acme.test">Mail</a>
<img src="a.png" alt="A widget"></body></html>"""

ABOUT = """<html><head><meta name="viewport" content="width=device-width"></head><body><h1>About</h1><h1>Team</h1>
<img src="team.png"><a href="/">Home</a></body></html>"""

PRICING = """<html><head><meta name="viewport" content="width=device-width"><title>Acme Home</title>
<meta name="robots" content="noindex, follow">
<meta name="description" content="Prices."></head><body><h1>Pricing</h1></body></html>"""

SLOW = """<html><head><meta name="viewport" content="width=device-width"><title>Slow page</title><meta name="description" content="Slow.">
</head><body><h1>Slow</h1></body></html>"""

ROBOTS = "User-agent: *\nDisallow: /private/\n\nUser-agent: OAI-SearchBot\nDisallow: /\n"

PAGES = {
    "/": (200, "text/html", HOME),
    "/about": (200, "text/html", ABOUT),
    "/pricing": (200, "text/html", PRICING),
    "/slow": (200, "text/html", SLOW),
    "/private/notes": (200, "text/html", "<html><title>Secret</title></html>"),
    "/robots.txt": (200, "text/plain", ROBOTS),
}


class Handler(BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):
        Handler.hits.append(self.path)
        if self.path == "/slow":
            time.sleep(0.4)
        status, ctype, body = PAGES.get(self.path, (404, "text/html", "<html>gone</html>"))
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="session")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def cfg(site):
    Handler.hits.clear()
    return ClientConfig(name="Acme", slug="acme", site=site,
                        max_pages=20, delay_seconds=0, slow_page_seconds=0.2)
