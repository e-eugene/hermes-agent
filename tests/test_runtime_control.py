from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
CONTROL_PATH = ROOT / "runtime" / "hermes-runtime-health.py"
NETWORK_PATH = ROOT / "runtime" / "hermes-browser-network-status.py"


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def control(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("API_SERVER_KEY", "test-runtime-token")
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "assigned_proxy")
    return load_module(CONTROL_PATH, "runtime_control")


def test_health_capabilities_and_network_snapshot_are_public_only(control) -> None:
    control.NETWORK_SNAPSHOT = {
        "status": "healthy",
        "mode": "assigned_proxy",
        "exit_ip": "8.8.8.8",
    }

    assert control.capabilities() == [
        "x_social_account",
        "remote_chromium",
        "network_status",
    ]
    assert control.network_snapshot() == {
        "status": "healthy",
        "mode": "assigned_proxy",
        "exit_ip": "8.8.8.8",
    }


def test_social_payload_is_redacted_even_if_helper_stderr_was_sensitive(control) -> None:
    result = control.safe_social_payload(
        {
            "status": "manual_attention",
            "type": "x",
            "login": "@assigned-handle",
            "error": "password=must-not-leak proxy=must-not-leak",
        }
    )

    assert result == {
        "status": "manual_attention",
        "type": "x",
        "login": "@assigned-handle",
        "error": "X requires manual attention in Remote Chromium",
    }
    assert "must-not-leak" not in str(result)


def test_network_payload_only_accepts_a_global_ip_and_the_selected_mode(control) -> None:
    result = control.safe_network_payload(
        {
            "status": "ok",
            "browser_network_mode": "direct",
            "exit_ip": "8.8.8.8",
            "error": "proxy-password=must-not-leak",
        }
    )

    assert result == {
        "status": "healthy",
        "mode": "assigned_proxy",
        "exit_ip": "8.8.8.8",
    }
    assert "must-not-leak" not in str(result)


def test_network_payload_fails_closed_for_private_or_invalid_egress(control) -> None:
    result = control.safe_network_payload(
        {"status": "ok", "exit_ip": "127.0.0.1"}
    )

    assert result == {
        "status": "unhealthy",
        "mode": "assigned_proxy",
        "error": "Browser network diagnostic failed",
    }


def test_network_status_parser_accepts_only_ip_echo_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HERMES_BROWSER_NETWORK_MODE", raising=False)
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_ENABLED", "false")
    network = load_module(NETWORK_PATH, "browser_network_status")

    assert network.browser_network_mode() == "direct"
    assert network.parse_exit_ip('{"ip":"8.8.8.8"}') == "8.8.8.8"
    assert network.parse_exit_ip("not an IP") is None
