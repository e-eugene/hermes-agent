from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
ENTRYPOINT = ROOT / "runtime" / "hermes-runtime-entrypoint.sh"
HEALTH_SERVER = ROOT / "runtime" / "hermes-runtime-health.py"
DOCKERFILE = ROOT / "Dockerfile"


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
        [os.environ.get("PYTHON", sys.executable), str(HEALTH_SERVER)],
        env={"PATH": os.environ["PATH"], "API_SERVER_KEY": ""},
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )

    assert result.returncode != 0
    assert "API_SERVER_KEY must be non-empty" in result.stderr


def test_entrypoint_sets_and_clears_runtime_context_deterministically() -> None:
    content = ENTRYPOINT.read_text()

    assert 'config set agent.system_prompt "${HERMES_RUNTIME_CONTEXT:-}"' in content
    assert "unset HERMES_RUNTIME_CONTEXT" in content
    assert "HERMES_BROWSER_NETWORK_MODE" in content
    assert "HERMES_RESIDENTIAL_PROXY_PASSWORD" in content


def test_entrypoint_keeps_upstream_proxy_secrets_inside_the_bridge() -> None:
    content = ENTRYPOINT.read_text()

    bridge_start = content.index("/usr/local/bin/residential-proxy serve")
    secret_unset = content.index("unset HERMES_RESIDENTIAL_PROXY_HOST", bridge_start)
    browser_start = content.index("/usr/local/bin/hermes-browser-supervisor")

    assert bridge_start < secret_unset < browser_start
    assert 'RESIDENTIAL_PROXY_URL="http://127.0.0.1:${HERMES_RESIDENTIAL_PROXY_PORT:-8899}"' in content
    assert "RESIDENTIAL_PROXY_URL" in content[content.index("  direct)") : content.index("  *)")]


def test_public_image_contains_only_private_remote_chromium_endpoints() -> None:
    content = DOCKERFILE.read_text()

    assert "x11vnc" in content
    assert "hermes-browser-gateway" in content
    assert "hermes-browser-network-status" in content
    assert "EXPOSE 6081 8642 8643 9120" in content


def test_x_use_is_pinned_isolated_and_uses_system_chromedriver() -> None:
    content = DOCKERFILE.read_text()
    lock = (ROOT / "runtime" / "x-use-requirements.lock").read_text()

    assert "e57e215e45b3e68cbd8cd7c46799cd932c234eac" in content
    assert "@sha256:f7b35053268f532f98955195c909f15a230470fbcbdacaa9fdecb95707dad04a" in content
    assert "ARG X_USE_COMMIT" not in content
    assert "/opt/x-use/.venv" in content
    assert "chromium-driver" in content
    assert "--no-deps --no-build-isolation --editable /opt/x-use/source" in content
    assert "x-use-requirements.lock" in content
    requirement_names = {
        line.split("==", 1)[0].lower()
        for line in lock.splitlines()
        if line and not line.startswith("#") and "==" in line
    }
    assert "undetected-chromedriver" not in requirement_names
    assert "selenium-stealth" not in requirement_names
    assert "selenium" in requirement_names
    assert "webdriver-manager" in requirement_names
    assert "cp /opt/x-use/source/LICENSE /licenses/x-use-LICENSE" in content
    assert "COPY LICENSE /licenses/hermes-agent-LICENSE" in content


def test_new_runtime_image_does_not_install_the_legacy_x_helper() -> None:
    content = DOCKERFILE.read_text()

    assert "COPY runtime/social-account.py" not in content
    assert "/usr/local/bin/social-account" not in content


def test_hermes_native_mcp_has_a_defense_in_depth_allowlist() -> None:
    content = ENTRYPOINT.read_text()

    assert 'mcp_servers["x_use"]' in content
    assert '"command": "/usr/local/bin/hermes-x-use-mcp"' in content
    assert '"prompts": False' in content
    assert '"resources": False' in content
    assert '"HTTP_PROXY": proxy_url' in content
    assert '"HTTPS_PROXY": proxy_url' in content
    assert '"NO_PROXY": "127.0.0.1,localhost,::1"' in content
    assert 'os.environ.get("HERMES_X_DIRECT_POSTING_ENABLED", "")' in content
    assert '"HERMES_X_DIRECT_POSTING_ENABLED": (' in content
    assert '"true" if direct_posting_enabled else "false"' in content
    assert '"HERMES_BROWSER_NETWORK_MODE": "assigned_proxy"' in content
    assert '"HERMES_RESIDENTIAL_PROXY_PORT": str(parsed_proxy.port)' in content
    assert '"RESIDENTIAL_PROXY_URL": proxy_url' in content
    assert 'parsed_proxy.hostname != "127.0.0.1"' in content
    assert 'mcp_servers.pop("x_use", None)' in content
    assert '"approve_draft"' not in content
    assert '"run_cycle"' not in content


def test_installed_hermes_mcp_preflight_gates_runtime_startup() -> None:
    entrypoint = ENTRYPOINT.read_text()
    dockerfile = DOCKERFILE.read_text()
    preflight = (ROOT / "runtime" / "hermes-x-use-native-preflight.py").read_text()

    assert "/usr/local/bin/hermes-x-use-native-preflight" in entrypoint
    assert "COPY runtime/hermes-x-use-native-preflight.py" in dockerfile
    assert "register_mcp_servers" in preflight
    assert "shutdown_mcp_servers" in preflight
    assert 'PREFIX = "mcp__x_use__"' in preflight
    assert "names != expected" in preflight
    assert "write_marker()" in preflight


def test_api_keeps_existing_tools_and_adds_x_use() -> None:
    content = ENTRYPOINT.read_text()

    assert 'config.setdefault("platform_toolsets", {})["api_server"] = [' in content
    api_block = content.split(
        'config.setdefault("platform_toolsets", {})["api_server"] = [', 1
    )[1].split("]", 1)[0]
    assert '"terminal"' in api_block
    assert '"browser"' in api_block
    assert '"x_use"' in api_block
    assert 'platform_toolsets["cli"]' not in content


def test_dashboard_publish_has_no_cancellable_outer_timeout() -> None:
    content = (ROOT / "runtime" / "hermes-runtime-health.py").read_text()

    assert "asyncio.wait_for(" not in content
    assert "X_USE_ACTION_TIMEOUT_SECONDS" not in content
