#!/opt/x-use/.venv/bin/python
"""Container-only proof that finite X cookies survive a clean Chromium restart."""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path


sys.path.insert(0, "/opt/hermes-runtime")


def chromium_main_pid() -> int:
    for path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = path.read_bytes().replace(b"\0", b" ")
        except OSError:
            continue
        if (
            b"chromium" in command
            and b"remote-debugging-port=9222" in command
            and b"--type=" not in command
        ):
            return int(path.parent.name)
    raise RuntimeError("Persistent Chromium process was not found")


def wait_for_restarted_browser(old_pid: int) -> int:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            new_pid = chromium_main_pid()
            with urllib.request.urlopen(  # nosec B310 - fixed loopback URL
                "http://127.0.0.1:9222/json/version", timeout=1
            ) as response:
                if new_pid != old_pid and response.status == 200:
                    return new_pid
        except Exception:
            pass
        time.sleep(0.25)
    raise RuntimeError("Persistent Chromium did not restart")


def main() -> None:
    from hermes_x_use_common import (
        CdpClient,
        REQUIRED_SESSION_COOKIES,
        _clear_existing_x_cookies,
        cookie_domain_allowed,
        validate_session_export,
    )
    from hermes_x_use_adapter import (
        SafeAttachedBrowserManager,
        runtime_config_loader,
    )

    export = json.dumps(
        [
            {
                "name": "auth_token",
                "value": "ephemeral-smoke-auth",
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expirationDate": 4102444800,
            },
            {
                "name": "ct0",
                "value": "ephemeral-smoke-csrf",
                "domain": ".x.com",
                "path": "/",
                "secure": True,
                "expirationDate": 4102444800,
            },
        ]
    ).encode()
    cookies = validate_session_export(export)
    client = CdpClient()
    _clear_existing_x_cookies(client)
    client.request("Storage.setCookies", {"cookies": cookies})
    cookies.clear()
    old_pid = chromium_main_pid()
    before_restart = client.request("Storage.getCookies").get("cookies", [])
    before_names = {
        str(item.get("name"))
        for item in before_restart
        if isinstance(item, dict)
        and cookie_domain_allowed(item.get("domain"))
        and item.get("value")
    }
    assert REQUIRED_SESSION_COOKIES.issubset(before_names)
    original_verifier = SafeAttachedBrowserManager.ensure_expected_handle
    SafeAttachedBrowserManager.ensure_expected_handle = (
        lambda self, timeout=35: self.expected_handle
    )
    manager = SafeAttachedBrowserManager(
        {"account_id": "expected_user", "is_active": True},
        runtime_config_loader(),
    )
    first_driver = manager.get_driver()
    first_owned_handle = str(manager._owned_handle)
    try:
        client.request("Browser.close", timeout=5)
    except Exception:
        # The browser websocket normally closes before Browser.close can send
        # its response; that is the expected clean-shutdown behavior.
        pass
    try:
        client.close()
    except Exception:
        pass
    new_pid = wait_for_restarted_browser(old_pid)

    try:
        second_driver = manager.get_driver()
        second_owned_handle = str(manager._owned_handle)
        assert second_driver is not first_driver
        assert second_owned_handle
        assert second_owned_handle != first_owned_handle
        assert manager.is_driver_active()
    finally:
        manager.close_driver()
        SafeAttachedBrowserManager.ensure_expected_handle = original_verifier

    client = CdpClient()
    stored = client.request("Storage.getCookies").get("cookies", [])
    client.close()
    names = {
        str(item.get("name"))
        for item in stored
        if isinstance(item, dict)
        and cookie_domain_allowed(item.get("domain"))
        and isinstance(item.get("expires"), (int, float))
        and item["expires"] > time.time() + 60
        and item.get("value")
    }
    assert REQUIRED_SESSION_COOKIES.issubset(names)
    print(
        json.dumps(
            {
                "status": "ok",
                "chromium_restarted": old_pid != new_pid,
                "warm_session_reattached": True,
                "persistent_required_cookie_names": sorted(
                    REQUIRED_SESSION_COOKIES & names
                ),
                "cookie_values_exposed": False,
            }
        )
    )


if __name__ == "__main__":
    main()
