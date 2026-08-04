#!/opt/hermes/.venv/bin/python
"""Use assigned social-account credentials without printing their secrets."""

from __future__ import annotations

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


def assigned_account(account_type: str) -> dict[str, Any]:
    matches = [
        account
        for account in load_accounts()
        if account.get("type") == account_type and account.get("is_active")
    ]
    if not matches:
        raise RuntimeError(f"No active {account_type} account is assigned")
    account = matches[0]
    if not account.get("login") or not account.get("password"):
        raise RuntimeError(f"Assigned {account_type} account has no credentials")
    return account


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


def ensure_sticky_proxy() -> None:
    if os.environ.get("HERMES_RESIDENTIAL_PROXY_ENABLED", "false") != "true":
        return
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


def command() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "list"
    try:
        if action in {"list", "status"}:
            accounts = [public_account(account) for account in load_accounts()]
            print(json.dumps({"accounts": accounts}))
            return
        if action == "login":
            account_type = sys.argv[2] if len(sys.argv) > 2 else ""
            if account_type != "reddit":
                raise RuntimeError(
                    "usage: social-account login reddit (other providers are not implemented)"
                )
            print(json.dumps(login_reddit(assigned_account(account_type))))
            return
        raise RuntimeError("usage: social-account [list|status|login reddit]")
    except RuntimeError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    command()
