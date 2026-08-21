#!/opt/hermes/.venv/bin/python
"""Use assigned social-account credentials without printing their secrets."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

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
ATTENTION_SCREENSHOT = Path("/opt/data/browser/x-manual-attention.png")
NETWORK_MODES = {"assigned_proxy", "direct"}


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


def ensure_sticky_proxy() -> None:
    if browser_network_mode() == "direct":
        return
    if os.environ.get("HERMES_RESIDENTIAL_PROXY_ENABLED") != "true":
        raise RuntimeError("Assigned proxy browser mode requires an active proxy")
    result = subprocess.run(
        ["/usr/local/bin/residential-proxy", "sticky"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not switch the residential proxy to sticky mode")


def login_reddit(
    account: dict[str, Any],
    *,
    client: CdpClient | None = None,
) -> dict[str, Any]:
    ensure_sticky_proxy()
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


class XBrowser:
    """Minimal CDP client for the single persistent Chromium X profile."""

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
        self.session_id = self._page_session()

    def close(self) -> None:
        self.websocket.close()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session: bool = True,
        timeout: float = 12,
    ) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        message: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session and hasattr(self, "session_id"):
            message["sessionId"] = self.session_id
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

    def _page_session(self) -> str:
        targets = self.request("Target.getTargets", session=False).get(
            "targetInfos", []
        )
        pages = [item for item in targets if item.get("type") == "page"]
        x_pages = [item for item in pages if "x.com" in str(item.get("url") or "")]
        target = (x_pages or pages)[-1] if (x_pages or pages) else None
        target_id = (
            target["targetId"]
            if target
            else self.request(
                "Target.createTarget", {"url": "about:blank"}, session=False
            )["targetId"]
        )
        return self.request(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            session=False,
        )["sessionId"]

    def evaluate(self, expression: str, *, timeout: float = 15) -> Any:
        result = self.request(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError("X page evaluation failed")
        return (result.get("result") or {}).get("value")

    def navigate(self, url: str) -> None:
        self.request("Page.enable")
        self.request("Page.navigate", {"url": url})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if self.evaluate("document.readyState") == "complete":
                    return
            except RuntimeError:
                pass
            time.sleep(0.25)
        raise RuntimeError("X page did not finish loading")

    def wait_for(self, expression: str, timeout: float = 20) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if value:
                return value
            time.sleep(0.4)
        return None

    def capture_attention(self) -> None:
        try:
            self.request("Page.enable")
            value = self.request("Page.captureScreenshot", {"format": "png"}).get(
                "data"
            )
            if value:
                ATTENTION_SCREENSHOT.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                ATTENTION_SCREENSHOT.write_bytes(base64.b64decode(value))
                ATTENTION_SCREENSHOT.chmod(0o600)
        except (OSError, ValueError, RuntimeError):
            pass


def normalized_login(value: str) -> str:
    return value.strip().lstrip("@").lower()


def x_current_user(cdp: XBrowser) -> str | None:
    result = cdp.evaluate("""
        fetch('/i/api/1.1/account/settings.json', {credentials: 'include'})
          .then(async response => {
            if (!response.ok) return null;
            const value = await response.json();
            return value.screen_name || null;
          }).catch(() => null)
        """)
    return str(result) if result else None


def x_challenge(cdp: XBrowser) -> str | None:
    return cdp.evaluate("""
        (() => {
          const text = (document.body?.innerText || '').toLowerCase();
          if (document.querySelector('iframe[src*="captcha"], [data-testid*="captcha"]') || text.includes('captcha')) return 'CAPTCHA';
          if (document.querySelector('input[autocomplete="one-time-code"]') || text.includes('authentication code') || text.includes('verification code')) return '2FA';
          if (text.includes('unusual activity') || text.includes('verify your identity') || text.includes('enter your phone') || text.includes('enter your email')) return 'challenge';
          return null;
        })()
        """)


def inspect_x(account: dict[str, Any], *, cdp: XBrowser) -> dict[str, Any]:
    cdp.navigate("https://x.com/home")
    actual = x_current_user(cdp)
    expected = str(account["login"])
    if actual and normalized_login(actual) == normalized_login(expected):
        return {"status": "authenticated", "type": "x", "login": expected}
    if actual:
        return {
            "status": "wrong_account",
            "type": "x",
            "login": expected,
            "error": "Chromium is authenticated as another X account",
        }
    attention = x_challenge(cdp)
    if attention:
        cdp.capture_attention()
        return {
            "status": "manual_attention",
            "type": "x",
            "login": expected,
            "error": f"X requires manual {attention} completion",
        }
    return {"status": "not_logged_in", "type": "x", "login": expected}


def fill_x(cdp: XBrowser, selector: str, value: str) -> bool:
    expression = f"""
    (() => {{
      const node = document.querySelector({json.dumps(selector)});
      if (!node) return false;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
      setter.call(node, {json.dumps(value)});
      node.dispatchEvent(new Event('input', {{bubbles: true}}));
      node.dispatchEvent(new Event('change', {{bubbles: true}}));
      node.focus();
      return true;
    }})()
    """
    return bool(cdp.evaluate(expression))


def click_x_text(cdp: XBrowser, text: str) -> bool:
    return bool(cdp.evaluate(f"""
            (() => {{
              const expected = {json.dumps(text.lower())};
              const node = [...document.querySelectorAll('button, [role="button"]')]
                .find(item => (item.innerText || '').trim().toLowerCase() === expected);
              if (!node) return false;
              node.click();
              return true;
            }})()
            """))


def login_x(account: dict[str, Any], *, cdp: XBrowser) -> dict[str, Any]:
    existing = inspect_x(account, cdp=cdp)
    # A challenge is intentionally resolved only by the operator in the
    # persistent Remote Chromium session. Navigating to the login flow here
    # would discard the CAPTCHA/2FA screen we need them to complete.
    if existing["status"] in {
        "authenticated",
        "wrong_account",
        "manual_attention",
    }:
        return existing
    cdp.navigate("https://x.com/i/flow/login")
    if not cdp.wait_for(
        "document.querySelector('input[autocomplete=\"username\"]') !== null"
    ):
        cdp.capture_attention()
        return {
            **existing,
            "status": "manual_attention",
            "error": "X login form requires manual attention",
        }
    x_login = str(account["login"]).lstrip("@")
    if not fill_x(cdp, 'input[autocomplete="username"]', x_login) or not click_x_text(cdp, "Next"):
        raise RuntimeError("Could not submit the X username step")
    password_ready = cdp.wait_for(
        "document.querySelector('input[name=\"password\"]') !== null || (document.body?.innerText || '').toLowerCase().includes('verify')",
        20,
    )
    if not password_ready or x_challenge(cdp):
        cdp.capture_attention()
        return {
            "status": "manual_attention",
            "type": "x",
            "login": str(account["login"]),
            "error": "X requires a manual identity challenge",
        }
    if not fill_x(cdp, 'input[name="password"]', str(account["password"])):
        raise RuntimeError("X password field was not found")
    if not bool(cdp.evaluate("""
        (() => {
          const button = document.querySelector('[data-testid="LoginForm_Login_Button"]');
          if (!button) return false;
          button.click();
          return true;
        })()
        """)):
        raise RuntimeError("X login submit button was not found")
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        time.sleep(0.7)
        actual = x_current_user(cdp)
        if actual:
            return inspect_x(account, cdp=cdp)
        attention = x_challenge(cdp)
        if attention:
            cdp.capture_attention()
            return {
                "status": "manual_attention",
                "type": "x",
                "login": str(account["login"]),
                "error": f"X requires manual {attention} completion",
            }
    cdp.capture_attention()
    return {
        "status": "manual_attention",
        "type": "x",
        "login": str(account["login"]),
        "error": "X did not complete login; continue in remote Chromium",
    }


def x_account_action(action: str) -> dict[str, Any]:
    account = assigned_social_account("x", require_password=action == "login")
    cdp = XBrowser()
    try:
        return inspect_x(account, cdp=cdp) if action == "status" else login_x(account, cdp=cdp)
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
            ensure_sticky_proxy()
            print(json.dumps(x_account_action("status")))
            return
        if action == "login":
            if account_type == "reddit":
                print(json.dumps(login_reddit(assigned_account(account_type))))
                return
            if account_type == "x":
                ensure_sticky_proxy()
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
