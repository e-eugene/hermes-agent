#!/opt/hermes/.venv/bin/python
"""Use assigned social-account credentials without printing their secrets."""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from websockets.sync.client import connect


ACCOUNTS_PATH = Path(
    os.environ.get(
        "HERMES_SOCIAL_ACCOUNTS_PATH",
        "/tmp/hermes-secrets/social-accounts.json",
    )
)
CDP_VERSION_URL = os.environ.get(
    "BROWSER_CDP_VERSION_URL",
    "http://127.0.0.1:9222/json/version",
)
NETWORK_MODES = {"assigned_proxy", "direct"}
X_HOME_URL = "https://x.com/home"
X_LOGIN_URL = "https://x.com/i/flow/login"
X_IDENTITY_TIMEOUT = 30.0
X_HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,15}$")
X_HANDLE_IN_TEXT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])@([A-Za-z0-9_]{1,15})(?![A-Za-z0-9_])"
)


def load_accounts() -> list[dict[str, Any]]:
    try:
        value = json.loads(ACCOUNTS_PATH.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeError("Assigned social-account secrets are unavailable") from exc
    if not isinstance(value, list):
        raise RuntimeError("Assigned social-account configuration is invalid")
    return [account for account in value if isinstance(account, dict)]


def public_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": str(account.get("type") or ""),
        "login": str(account.get("login") or ""),
        "password_set": bool(account.get("password")),
        "is_active": bool(account.get("is_active")),
    }


def assigned_social_account(
    account_type: str,
    *,
    require_password: bool = True,
) -> dict[str, Any]:
    matches = [
        account
        for account in load_accounts()
        if account.get("type") == account_type and account.get("is_active")
    ]
    if not matches:
        raise RuntimeError(f"No active {account_type} account is assigned")
    account = matches[0]
    if not account.get("login") or (require_password and not account.get("password")):
        raise RuntimeError(f"Assigned {account_type} account has no credentials")
    return account


def assigned_account(account_type: str) -> dict[str, Any]:
    """Backward-compatible credential-required account lookup."""

    return assigned_social_account(account_type, require_password=True)


class CdpClient:
    def __init__(self) -> None:
        with urllib.request.urlopen(CDP_VERSION_URL, timeout=5) as response:
            websocket_url = json.load(response)["webSocketDebuggerUrl"]
        self.websocket = connect(
            websocket_url,
            open_timeout=5,
            close_timeout=1,
            proxy=None,
        )
        self._request_id = 0

    def close(self) -> None:
        self.websocket.close()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 10,
    ) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        message: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id
        self.websocket.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = json.loads(
                self.websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
            )
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                raise RuntimeError(
                    f"Chromium CDP command {method} failed: "
                    f"{response['error'].get('message', 'unknown error')}"
                )
            return response.get("result") or {}
        raise RuntimeError(f"Chromium CDP command {method} timed out")

    def reddit_page_session(self) -> str:
        targets = self.request("Target.getTargets").get("targetInfos", [])
        pages = [target for target in targets if target.get("type") == "page"]
        reddit_pages = [
            target for target in pages if "reddit.com" in str(target.get("url") or "")
        ]
        target = (reddit_pages or pages)[-1] if (reddit_pages or pages) else None
        if target is None:
            target_id = self.request(
                "Target.createTarget",
                {"url": "about:blank"},
            )["targetId"]
        else:
            target_id = target["targetId"]
        return self.request(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )["sessionId"]

    def evaluate(self, session_id: str, expression: str) -> Any:
        result = self.request(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
            },
            session_id=session_id,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("Reddit page script failed")
        return (result.get("result") or {}).get("value")

    def navigate(self, session_id: str, url: str) -> None:
        self.request("Page.enable", session_id=session_id)
        self.request("Page.navigate", {"url": url}, session_id=session_id)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                if self.evaluate(session_id, "document.readyState") == "complete":
                    return
            except RuntimeError:
                pass
            time.sleep(0.25)
        raise RuntimeError("Reddit page did not finish loading")

    def reddit_user(self, session_id: str) -> str | None:
        value = self.evaluate(
            session_id,
            "fetch('/api/me.json').then(r => r.json()).then(j => j.data?.name || null)",
        )
        return str(value) if value else None

    def login_nodes(self, session_id: str) -> tuple[int, int, int]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            document = self.request(
                "DOM.getFlattenedDocument",
                {"depth": -1, "pierce": True},
                session_id=session_id,
            )
            nodes = document.get("nodes", [])
            username_node = None
            password_node = None
            submit_node = None
            for node in nodes:
                attributes = node.get("attributes") or []
                attrs = {
                    str(attributes[index]).lower(): str(attributes[index + 1])
                    for index in range(0, len(attributes) - 1, 2)
                }
                node_name = str(node.get("nodeName") or "").upper()
                identity = " ".join(
                    attrs.get(key, "")
                    for key in ("name", "id", "autocomplete", "placeholder")
                ).lower()
                if node_name == "INPUT" and attrs.get("type", "text") == "password":
                    password_node = node.get("nodeId")
                elif node_name == "INPUT" and any(
                    token in identity for token in ("username", "email")
                ):
                    username_node = node.get("nodeId")
                elif node_name == "BUTTON" and attrs.get("type", "").lower() == "submit":
                    submit_node = node.get("nodeId")
            if username_node and password_node and submit_node:
                return int(username_node), int(password_node), int(submit_node)
            time.sleep(0.5)
        raise RuntimeError(
            "Reddit login fields were not found; CAPTCHA or a page change may require manual attention"
        )

    def insert_text(self, session_id: str, node_id: int, value: str) -> None:
        self.request("DOM.focus", {"nodeId": node_id}, session_id=session_id)
        self.request("Input.insertText", {"text": value}, session_id=session_id)

    def click(self, session_id: str, node_id: int) -> None:
        remote_object = self.request(
            "DOM.resolveNode",
            {"nodeId": node_id},
            session_id=session_id,
        )["object"]
        self.request(
            "Runtime.callFunctionOn",
            {
                "objectId": remote_object["objectId"],
                "functionDeclaration": "function () { this.click(); }",
            },
            session_id=session_id,
        )


def browser_network_mode() -> str:
    """Resolve the explicit browser mode without silently falling back to direct."""

    configured = os.environ.get("HERMES_BROWSER_NETWORK_MODE")
    if configured:
        if configured not in NETWORK_MODES:
            raise RuntimeError("Browser network mode is invalid")
        return configured
    # Older image consumers did not set a mode. Retain their existing behavior:
    # a configured bridge means proxy mode; otherwise the browser is direct.
    return (
        "assigned_proxy"
        if os.environ.get("HERMES_RESIDENTIAL_PROXY_ENABLED") == "true"
        else "direct"
    )


def verify_browser_network() -> None:
    """Fail closed when the selected browser route is not ready.

    The residential proxy bridge owns the persistent selector. Social-account
    status and login checks must never rotate it because an X session should
    keep the same browser identity and exit route.
    """

    if browser_network_mode() == "direct":
        return
    if os.environ.get("HERMES_RESIDENTIAL_PROXY_ENABLED") != "true":
        raise RuntimeError("Assigned proxy browser mode requires an active proxy")
    try:
        port = int(os.environ.get("HERMES_RESIDENTIAL_PROXY_PORT", "8899"))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError as exc:
        raise RuntimeError("Assigned proxy bridge configuration is invalid") from exc
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
    except OSError as exc:
        raise RuntimeError("Assigned proxy bridge is unavailable") from exc


def login_reddit(
    account: dict[str, Any],
    *,
    client: CdpClient | None = None,
) -> dict[str, Any]:
    verify_browser_network()
    cdp = client or CdpClient()
    owns_client = client is None
    try:
        session_id = cdp.reddit_page_session()
        cdp.navigate(session_id, "https://www.reddit.com/")
        current_user = cdp.reddit_user(session_id)
        expected_user = str(account["login"])
        if current_user == expected_user:
            return {
                "status": "already_logged_in",
                "type": "reddit",
                "login": expected_user,
            }
        if current_user:
            raise RuntimeError(
                f"Reddit is already logged in as a different account ({current_user})"
            )

        cdp.navigate(session_id, "https://www.reddit.com/login/")
        username_node, password_node, submit_node = cdp.login_nodes(session_id)
        cdp.insert_text(session_id, username_node, expected_user)
        cdp.insert_text(session_id, password_node, str(account["password"]))
        cdp.click(session_id, submit_node)

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                current_user = cdp.reddit_user(session_id)
            except RuntimeError:
                continue
            if current_user == expected_user:
                return {
                    "status": "logged_in",
                    "type": "reddit",
                    "login": expected_user,
                }
        return {
            "status": "submitted",
            "type": "reddit",
            "login": expected_user,
            "message": "Login submitted; complete CAPTCHA or 2FA in the browser if prompted",
        }
    finally:
        if owns_client:
            cdp.close()


def is_x_page(url: object) -> bool:
    hostname = (urlparse(str(url or "")).hostname or "").lower()
    return hostname == "x.com" or hostname.endswith(".x.com")


def is_x_logged_out_page(url: object) -> bool:
    parsed = urlparse(str(url or ""))
    return is_x_page(url) and parsed.path.startswith(
        ("/i/flow/login", "/i/jf/onboarding")
    )


def is_x_auth_page(url: object) -> bool:
    parsed = urlparse(str(url or ""))
    return is_x_page(url) and parsed.path.startswith(
        ("/i/flow/", "/i/jf/", "/account/access")
    )


class XBrowser:
    """Minimal CDP client for one persistent, operator-visible Chromium profile."""

    def __init__(self) -> None:
        with urllib.request.urlopen(
            CDP_VERSION_URL, timeout=5
        ) as response:  # nosec B310
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
        timeout: float = 12,
    ) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        message: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id
        self.websocket.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = json.loads(
                self.websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
            )
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                raise RuntimeError("Chromium X-account command failed")
            return response.get("result") or {}
        raise RuntimeError("Chromium X-account command timed out")

    def page_targets(self) -> list[dict[str, Any]]:
        targets = self.request("Target.getTargets").get("targetInfos", [])
        return [
            item
            for item in targets
            if isinstance(item, dict)
            and item.get("type") == "page"
            and item.get("targetId")
        ]

    def create_target(self, url: str, *, background: bool) -> str:
        return str(
            self.request(
                "Target.createTarget",
                {"url": url, "background": background},
            )["targetId"]
        )

    def attach(self, target_id: str) -> str:
        return str(
            self.request(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )["sessionId"]
        )

    def close_target(self, target_id: str) -> None:
        self.request("Target.closeTarget", {"targetId": target_id})

    def evaluate(self, session_id: str, expression: str, *, timeout: float = 15) -> Any:
        result = self.request(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            session_id=session_id,
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("X page evaluation failed")
        return (result.get("result") or {}).get("value")

    def present_login(self) -> str:
        """Reuse an auth tab or open a new one without replacing operator work."""

        pages = self.page_targets()
        auth_pages = [item for item in pages if is_x_auth_page(item.get("url"))]
        target = auth_pages[-1] if auth_pages else None
        if target is None:
            target_id = self.create_target(X_LOGIN_URL, background=False)
        else:
            target_id = str(target["targetId"])
        self.request("Target.activateTarget", {"targetId": target_id})
        return target_id


def normalized_login(value: str) -> str:
    return value.strip().lstrip("@").lower()


def x_identity_snapshot(cdp: XBrowser, session_id: str) -> dict[str, Any]:
    result = cdp.evaluate(
        session_id,
        r"""
        (() => {
          const profile = document.querySelector('[data-testid="AppTabBar_Profile_Link"]');
          const switcher = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
          const text = (document.body?.innerText || '').toLowerCase();
          let challenge = null;
          if (text.includes('captcha')) challenge = 'CAPTCHA';
          else if (text.includes('authentication code') || text.includes('verification code')) challenge = '2FA';
          else if (text.includes('unusual activity') || text.includes('verify your identity') || text.includes('enter your phone') || text.includes('enter your email')) challenge = 'challenge';
          return {
            url: String(location.href || ''),
            app_ready: location.hostname === 'x.com' &&
              document.readyState !== 'loading' &&
              Boolean(document.querySelector('#react-root')),
            profile_href: profile?.getAttribute('href') || '',
            account_switcher_text: (switcher?.innerText || switcher?.textContent || '').slice(0, 200),
            challenge,
          };
        })()
        """,
    )
    if not isinstance(result, dict):
        raise RuntimeError("X identity probe returned an invalid response")
    return result


def x_handle_from_profile_href(value: object) -> str | None:
    parsed = urlparse(str(value or ""))
    if parsed.hostname and not is_x_page(value):
        return None
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/?", parsed.path)
    if not match:
        return None
    handle = match.group(1)
    return handle if X_HANDLE_PATTERN.fullmatch(handle) else None


def x_handle_from_switcher_text(value: object) -> str | None:
    match = X_HANDLE_IN_TEXT_PATTERN.search(str(value or ""))
    if not match:
        return None
    handle = match.group(1)
    return handle if X_HANDLE_PATTERN.fullmatch(handle) else None


def x_handle_from_snapshot(snapshot: dict[str, Any]) -> str | None:
    return x_handle_from_profile_href(
        snapshot.get("profile_href")
    ) or x_handle_from_switcher_text(snapshot.get("account_switcher_text"))


def poll_x_identity(
    cdp: XBrowser,
    session_id: str,
    *,
    timeout: float = X_IDENTITY_TIMEOUT,
) -> tuple[str, str | None]:
    """Wait for a positive X identity, logged-out, or challenge signal."""

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        snapshot = x_identity_snapshot(cdp, session_id)
        url = str(snapshot.get("url") or "")
        if is_x_page(url) and bool(snapshot.get("app_ready")):
            handle = x_handle_from_snapshot(snapshot)
            if handle:
                return "authenticated", handle
            attention = snapshot.get("challenge")
            if attention in {"CAPTCHA", "2FA", "challenge"}:
                return "manual_attention", str(attention)
            if is_x_logged_out_page(url):
                return "not_logged_in", None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "indeterminate", None
        time.sleep(min(0.25, remaining))


def inspect_x(account: dict[str, Any], *, cdp: XBrowser) -> dict[str, Any]:
    expected = str(account["login"])
    target_id = cdp.create_target(X_HOME_URL, background=True)
    try:
        session_id = cdp.attach(target_id)
        state, detail = poll_x_identity(cdp, session_id)
        if state == "authenticated" and normalized_login(str(detail)) == normalized_login(expected):
            return {"status": "authenticated", "type": "x", "login": expected}
        if state == "authenticated":
            return {
                "status": "wrong_account",
                "type": "x",
                "login": expected,
                "error": "Chromium is authenticated as another X account",
            }
        if state == "manual_attention":
            return {
                "status": "manual_attention",
                "type": "x",
                "login": expected,
                "error": f"X requires manual {detail} completion",
            }
        if state == "not_logged_in":
            return {"status": "not_logged_in", "type": "x", "login": expected}
        return {
            "status": "manual_attention",
            "type": "x",
            "login": expected,
            "error": "X identity could not be verified; inspect Remote Chromium",
        }
    finally:
        try:
            cdp.close_target(target_id)
        except Exception:
            # The renderer may already have discarded the temporary target.
            # Cleanup is best-effort and must not replace a verified result or
            # the original identity-probe error with a closeTarget failure.
            pass


def login_x(account: dict[str, Any], *, cdp: XBrowser) -> dict[str, Any]:
    existing = inspect_x(account, cdp=cdp)
    if existing["status"] in {"authenticated", "wrong_account"}:
        return existing
    cdp.present_login()
    return {
        "status": "manual_attention",
        "type": "x",
        "login": str(account["login"]),
        "error": "Complete X sign-in in Remote Chromium, then check status",
    }


def x_account_action(action: str) -> dict[str, Any]:
    assigned = assigned_social_account("x", require_password=False)
    account = {
        "type": "x",
        "login": str(assigned["login"]),
        "is_active": True,
    }
    cdp = XBrowser()
    try:
        return (
            inspect_x(account, cdp=cdp)
            if action == "status"
            else login_x(account, cdp=cdp)
        )
    finally:
        cdp.close()


def command() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "list"
    account_type = sys.argv[2] if len(sys.argv) > 2 else ""
    try:
        if action == "list" or (action == "status" and not account_type):
            accounts = [public_account(account) for account in load_accounts()]
            print(json.dumps({"accounts": accounts}))
            return
        if action == "status" and account_type == "x":
            verify_browser_network()
            print(json.dumps(x_account_action("status")))
            return
        if action == "login":
            if account_type == "reddit":
                print(json.dumps(login_reddit(assigned_account(account_type))))
                return
            if account_type == "x":
                verify_browser_network()
                print(json.dumps(x_account_action("login")))
                return
        raise RuntimeError("usage: social-account [list|status x|login reddit|login x]")
    except RuntimeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        raise SystemExit(1) from exc
    except Exception:
        # CDP/websocket library errors can vary by version. The dashboard gets
        # a generic result instead of an implementation traceback or stderr.
        print(json.dumps({"status": "error", "error": "Social-account helper failed"}))
        raise SystemExit(1)


if __name__ == "__main__":
    command()
