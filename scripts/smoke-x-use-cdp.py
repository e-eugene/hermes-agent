#!/opt/x-use/.venv/bin/python
"""Container-only proof that x-use owns one CDP target, not Chromium itself."""

from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, "/opt/hermes-runtime")


def chromium_pids() -> set[int]:
    result: set[int] = set()
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
            result.add(int(path.parent.name))
    return result


def page_targets(client) -> dict[str, str]:
    return {
        str(item["targetId"]): str(item.get("url") or "")
        for item in client.request("Target.getTargets").get("targetInfos", [])
        if isinstance(item, dict) and item.get("type") == "page" and item.get("targetId")
    }


def main() -> None:
    secrets = Path("/tmp/hermes-secrets")
    secrets.mkdir(parents=True, exist_ok=True)
    social_path = secrets / "social-accounts.json"
    social_path.write_text(
        json.dumps(
            [{"type": "x", "login": "expected_user", "is_active": True}]
        ),
        encoding="utf-8",
    )
    social_path.chmod(0o600)

    from hermes_x_use_common import CdpClient, configure_runtime

    configure_runtime()
    client = CdpClient()
    operator_id = str(
        client.request(
            "Target.createTarget",
            {
                "url": "data:text/html,<title>operator-marker</title>",
                "background": True,
            },
        )["targetId"]
    )
    before_targets = page_targets(client)
    before_pids = chromium_pids()
    client.close()
    assert operator_id in before_targets

    from hermes_x_use_adapter import SafeAttachedBrowserManager, runtime_config_loader

    original_verifier = SafeAttachedBrowserManager.ensure_expected_handle
    SafeAttachedBrowserManager.ensure_expected_handle = (
        lambda self, timeout=35: self.expected_handle
    )
    try:
        manager = SafeAttachedBrowserManager(
            {"account_id": "expected_user", "is_active": True},
            runtime_config_loader(),
        )
        driver = manager.get_driver()
        owned_handle = str(manager._owned_handle)
        driver.get("data:text/html,<title>x-use-owned-marker</title>")
        manager.close_driver()
    finally:
        SafeAttachedBrowserManager.ensure_expected_handle = original_verifier

    owned_target_id = owned_handle.removeprefix("CDwindow-")
    client = CdpClient()
    after_targets = page_targets(client)
    after_pids = chromium_pids()
    client.close()

    if not (
        operator_id in after_targets
        and "operator-marker" in after_targets[operator_id]
        and owned_target_id not in after_targets
        and before_pids
        and before_pids == after_pids
    ):
        raise AssertionError(
            json.dumps(
                {
                    "operator_id": operator_id,
                    "owned_target_id": owned_target_id,
                    "before_targets": before_targets,
                    "after_targets": after_targets,
                    "before_pids": sorted(before_pids),
                    "after_pids": sorted(after_pids),
                }
            )
        )
    print(
        json.dumps(
            {
                "status": "ok",
                "operator_target_preserved": True,
                "owned_target_closed": True,
                "chromium_process_preserved": True,
            }
        )
    )


if __name__ == "__main__":
    main()
