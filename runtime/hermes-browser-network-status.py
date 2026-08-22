#!/opt/hermes/.venv/bin/python
"""Report the egress IP observed by the headed Chromium browser.

This intentionally uses Chrome DevTools Protocol instead of a host-side HTTP
client, so a successful result proves the browser's current proxy mode.  It
only emits the selected mode, a public IP address, and a redacted error.
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
import urllib.request
from typing import Any

from websockets.sync.client import connect


CDP_VERSION_URL = os.environ.get(
    "BROWSER_CDP_VERSION_URL", "http://127.0.0.1:9222/json/version"
)
IP_ECHO_URL = "https://api.ipify.org?format=json"
NETWORK_MODES = {"assigned_proxy", "direct"}


def browser_network_mode() -> str:
    """Return the explicit mode, preserving compatibility for older callers."""

    configured = os.environ.get("HERMES_BROWSER_NETWORK_MODE")
    if configured:
        if configured not in NETWORK_MODES:
            raise RuntimeError("Browser network mode is invalid")
        return configured
    return (
        "assigned_proxy"
        if os.environ.get("HERMES_RESIDENTIAL_PROXY_ENABLED") == "true"
        else "direct"
    )


class Cdp:
    def __init__(self) -> None:
        with urllib.request.urlopen(CDP_VERSION_URL, timeout=5) as response:  # nosec B310
            websocket_url = json.load(response)["webSocketDebuggerUrl"]
        self.websocket = connect(
            websocket_url,
            open_timeout=5,
            close_timeout=1,
            proxy=None,
        )
        self.request_id = 0

    def close(self) -> None:
        self.websocket.close()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        request: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            request["sessionId"] = session_id
        self.websocket.send(json.dumps(request))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = json.loads(
                self.websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
            )
            if message.get("id") != request_id:
                continue
            if message.get("error"):
                raise RuntimeError("Chromium network diagnostic command failed")
            return message.get("result") or {}
        raise RuntimeError("Chromium network diagnostic timed out")

    def exit_ip(self) -> str:
        target_id = self.request("Target.createTarget", {"url": "about:blank"})[
            "targetId"
        ]
        session_id: str | None = None
        try:
            session_id = self.request(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )["sessionId"]
            self.request("Page.enable", session_id=session_id)
            self.request("Page.navigate", {"url": IP_ECHO_URL}, session_id=session_id)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                result = self.request(
                    "Runtime.evaluate",
                    {
                        "expression": "document.readyState === 'complete' ? (document.body?.innerText || '') : ''",
                        "returnByValue": True,
                    },
                    session_id=session_id,
                )
                body = str((result.get("result") or {}).get("value") or "")
                parsed = parse_exit_ip(body)
                if parsed:
                    return parsed
                time.sleep(0.25)
            raise RuntimeError("Browser network diagnostic did not return an IP")
        finally:
            try:
                if session_id:
                    self.request("Target.detachFromTarget", {"sessionId": session_id})
                self.request("Target.closeTarget", {"targetId": target_id})
            except RuntimeError:
                pass


def parse_exit_ip(body: str) -> str | None:
    """Accept only a standalone public IP or api.ipify's JSON response."""

    candidate = body.strip()
    try:
        decoded = json.loads(candidate)
        if isinstance(decoded, dict):
            candidate = str(decoded.get("ip") or "").strip()
    except ValueError:
        pass
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def status() -> dict[str, str]:
    mode = browser_network_mode()
    client: Cdp | None = None
    try:
        client = Cdp()
        return {"status": "ok", "browser_network_mode": mode, "exit_ip": client.exit_ip()}
    except Exception:
        # CDP library failures differ between Chromium/websockets versions.
        # The private API deliberately gets no low-level endpoint or proxy data.
        return {
            "status": "error",
            "browser_network_mode": mode,
            "error": "Browser network diagnostic failed",
        }
    finally:
        if client:
            try:
                client.close()
            except OSError:
                pass


if __name__ == "__main__":
    result = status()
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "ok" else 1)
