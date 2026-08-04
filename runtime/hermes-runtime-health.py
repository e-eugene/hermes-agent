#!/usr/bin/env python3
"""Authenticated readiness endpoint for private runtime networks."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import AF_INET6
from urllib.error import URLError
from urllib.request import Request, urlopen
import os


TOKEN = os.environ.get("API_SERVER_KEY", "")
TUI_PORT = os.environ.get("HERMES_TUI_PORT", "9119")

if not TOKEN:
    raise SystemExit("API_SERVER_KEY must be non-empty")


def healthy() -> bool:
    checks = (
        ("http://[::1]:8642/health", {"Authorization": f"Bearer {TOKEN}"}),
        ("http://127.0.0.1:9222/json/version", {}),
        (f"http://127.0.0.1:{TUI_PORT}/api/status", {"Authorization": f"Bearer {TOKEN}", "Host": f"localhost:{TUI_PORT}"}),
    )
    try:
        for url, headers in checks:
            with urlopen(Request(url, headers=headers), timeout=2) as response:  # nosec B310
                if response.status != 200:
                    return False
    except (OSError, URLError):
        return False
    return True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health" or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self.send_error(404)
            return
        self.send_response(200 if healthy() else 503)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class Server(HTTPServer):
    address_family = AF_INET6


Server(("::", 8643), Handler).serve_forever()
