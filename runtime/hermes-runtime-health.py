#!/opt/hermes/.venv/bin/python
"""Authenticated private runtime control API.

Only a trusted dashboard can reach this listener. Responses deliberately expose
capability, account state and browser egress information, never credentials,
proxy endpoints, local paths or helper stderr.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from socket import AF_INET6
from urllib.error import URLError
from urllib.request import Request, urlopen


TOKEN = os.environ.get("API_SERVER_KEY", "")
TUI_PORT = os.environ.get("HERMES_TUI_PORT", "9119")
BROWSER_GATEWAY_PORT = int(os.environ.get("HERMES_BROWSER_GATEWAY_PORT", "6081"))
NETWORK_MODES = {"assigned_proxy", "direct"}
SOCIAL_STATUSES = {
    "authenticated",
    "not_logged_in",
    "wrong_account",
    "manual_attention",
    "error",
}

if not TOKEN:
    raise SystemExit("API_SERVER_KEY must be non-empty")

# Do not make regular readiness probes depend on an external IP-echo provider.
# This snapshot is refreshed when the dashboard explicitly requests the network
# diagnostic and remains deliberately non-persistent.
NETWORK_SNAPSHOT: dict[str, object] = {}


def browser_network_mode() -> str:
    configured = os.environ.get("HERMES_BROWSER_NETWORK_MODE")
    if configured in NETWORK_MODES:
        return configured
    return (
        "assigned_proxy"
        if os.environ.get("HERMES_RESIDENTIAL_PROXY_ENABLED") == "true"
        else "direct"
    )


def capabilities() -> list[str]:
    return ["x_social_account", "remote_chromium", "network_status"]


def healthy() -> bool:
    checks = (
        ("http://[::1]:8642/health", {"Authorization": f"Bearer {TOKEN}"}),
        ("http://127.0.0.1:9222/json/version", {}),
        (
            f"http://127.0.0.1:{TUI_PORT}/api/status",
            {"Authorization": f"Bearer {TOKEN}", "Host": f"localhost:{TUI_PORT}"},
        ),
    )
    try:
        for url, headers in checks:
            with urlopen(Request(url, headers=headers), timeout=2) as response:  # nosec B310
                if response.status != 200:
                    return False
        with socket.create_connection(("127.0.0.1", 5900), timeout=2):
            pass
        with socket.create_connection(("::1", BROWSER_GATEWAY_PORT), timeout=2):
            pass
    except (OSError, URLError):
        return False
    return True


def safe_login(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    # Account names are model-visible metadata, but cap/control characters avoid
    # turning an unexpected helper response into an information-disclosure path.
    candidate = value.strip()
    if not candidate or len(candidate) > 128 or any(ord(char) < 32 for char in candidate):
        return None
    return candidate


def safe_social_payload(payload: object) -> dict[str, object]:
    """Convert helper stdout to a deliberately small, credential-free schema."""

    source = payload if isinstance(payload, dict) else {}
    status = str(source.get("status") or "error")
    if status not in SOCIAL_STATUSES:
        status = "error"
    result: dict[str, object] = {"status": status, "type": "x"}
    if login := safe_login(source.get("login")):
        result["login"] = login
    fixed_errors = {
        "wrong_account": "Chromium is authenticated as another X account",
        "manual_attention": "X requires manual attention in Remote Chromium",
        "error": "X social-account helper failed",
    }
    if error := fixed_errors.get(status):
        result["error"] = error
    return result


def social_account(account_type: str, action: str) -> tuple[int, dict[str, object]]:
    if account_type != "x" or action not in {"status", "login"}:
        return 404, {"status": "error", "error": "Unsupported social-account action"}
    try:
        result = subprocess.run(
            ["/usr/local/bin/social-account", action, account_type],
            capture_output=True,
            text=True,
            timeout=75,
            check=False,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return 503, {"status": "error", "type": "x", "error": "X social-account helper failed"}
    return (200 if result.returncode == 0 else 409), safe_social_payload(payload)


def safe_network_payload(payload: object) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    mode = browser_network_mode()
    result: dict[str, object] = {
        "status": "unhealthy",
        "mode": mode,
    }
    if source.get("status") == "ok":
        try:
            exit_ip = ipaddress.ip_address(str(source.get("exit_ip") or ""))
        except ValueError:
            exit_ip = None
        if exit_ip and exit_ip.is_global:
            result["status"] = "healthy"
            result["exit_ip"] = str(exit_ip)
            return result
    result["error"] = "Browser network diagnostic failed"
    return result


def browser_network_status() -> tuple[int, dict[str, object]]:
    global NETWORK_SNAPSHOT
    try:
        result = subprocess.run(
            ["/usr/local/bin/hermes-browser-network-status"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        payload = {
            "status": "unavailable",
            "mode": browser_network_mode(),
            "error": "Browser network diagnostic failed",
        }
        NETWORK_SNAPSHOT = payload
        return 503, payload
    sanitized = safe_network_payload(payload)
    NETWORK_SNAPSHOT = sanitized
    return (200 if result.returncode == 0 and sanitized["status"] == "healthy" else 503), sanitized


def network_snapshot() -> dict[str, object]:
    snapshot = NETWORK_SNAPSHOT.copy()
    snapshot.setdefault("status", "unavailable")
    snapshot["mode"] = browser_network_mode()
    return snapshot


class Handler(BaseHTTPRequestHandler):
    def authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def respond(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self.authorized():
            self.respond(404, {"status": "not_found"})
            return
        if self.path == "/health":
            ready = healthy()
            self.respond(
                200 if ready else 503,
                {
                    "status": "ok" if ready else "unhealthy",
                    "capabilities": capabilities(),
                    "network": network_snapshot(),
                },
            )
            return
        if self.path == "/network/status":
            status, payload = browser_network_status()
            self.respond(status, payload)
            return
        self.respond(404, {"status": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if not self.authorized():
            self.respond(404, {"status": "not_found"})
            return
        parts = self.path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "social-accounts":
            self.respond(404, {"status": "not_found"})
            return
        status, payload = social_account(parts[1], parts[2])
        self.respond(status, payload)

    def log_message(self, format: str, *args: object) -> None:
        return


class Server(HTTPServer):
    address_family = AF_INET6


def main() -> None:
    Server(("::", 8643), Handler).serve_forever()


if __name__ == "__main__":
    main()
