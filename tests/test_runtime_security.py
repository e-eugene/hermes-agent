from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
ENTRYPOINT = ROOT / "runtime" / "hermes-runtime-entrypoint.sh"
HEALTH_SERVER = ROOT / "runtime" / "hermes-runtime-health.py"


def runtime_environment() -> dict[str, str]:
    return {
        "PATH": os.environ["PATH"],
        "API_SERVER_ENABLED": "true",
        "API_SERVER_HOST": "::",
        "API_SERVER_PORT": "8642",
        "API_SERVER_KEY": "per-agent-runtime-key",
        "API_SERVER_MODEL_NAME": "test-agent",
        "HERMES_DASHBOARD_SESSION_TOKEN": "per-agent-runtime-key",
        "HERMES_PRESET_PROVIDER": "openai-api",
        "HERMES_MODEL": "test-model",
        "HERMES_TUI_PORT": "9119",
        "HERMES_TUI_BRIDGE_PORT": "9120",
        "HERMES_TUI_WS_ORPHAN_REAP_GRACE_SECONDS": "120",
        "HERMES_SOCIAL_ACCOUNTS_PATH": (
            "/tmp/hermes-secrets/social-accounts.json"
        ),
    }


def run_entrypoint(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ENTRYPOINT)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_entrypoint_rejects_empty_api_key_before_startup() -> None:
    environment = runtime_environment()
    environment["API_SERVER_KEY"] = ""

    result = run_entrypoint(environment)

    assert result.returncode == 1
    assert "API_SERVER_KEY" in result.stderr
    assert "/opt/data" not in result.stderr


def test_entrypoint_rejects_mismatched_runtime_tokens() -> None:
    environment = runtime_environment()
    environment["HERMES_DASHBOARD_SESSION_TOKEN"] = "different-token"

    result = run_entrypoint(environment)

    assert result.returncode == 1
    assert "must match" in result.stderr


def test_entrypoint_rejects_invalid_private_port() -> None:
    environment = runtime_environment()
    environment["HERMES_TUI_BRIDGE_PORT"] = "70000"

    result = run_entrypoint(environment)

    assert result.returncode == 1
    assert "between 1 and 65535" in result.stderr


def test_entrypoint_rejects_non_contract_api_port() -> None:
    environment = runtime_environment()
    environment["API_SERVER_PORT"] = "8644"

    result = run_entrypoint(environment)

    assert result.returncode == 1
    assert "API_SERVER_PORT must be 8642" in result.stderr


def test_health_server_fails_closed_without_api_key() -> None:
    result = subprocess.run(
        [os.environ.get("PYTHON", "python3"), str(HEALTH_SERVER)],
        env={"PATH": os.environ["PATH"], "API_SERVER_KEY": ""},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "API_SERVER_KEY must be non-empty" in result.stderr
