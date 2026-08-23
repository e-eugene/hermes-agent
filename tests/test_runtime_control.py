from __future__ import annotations

import importlib.util
import json
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
def control(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("API_SERVER_KEY", "test-runtime-token")
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "assigned_proxy")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PORT", "8899")
    monkeypatch.setenv("RESIDENTIAL_PROXY_URL", "http://127.0.0.1:8899")
    monkeypatch.delenv("HERMES_X_DIRECT_POSTING_ENABLED", raising=False)
    marker = tmp_path / "native-mcp-ready.json"
    marker.write_text(
        json.dumps(
            {
                "commit": "e57e215e45b3e68cbd8cd7c46799cd932c234eac",
                "tool_count": 14,
                "version": "2.4.1",
            }
        )
    )
    marker.chmod(0o600)
    monkeypatch.setenv("HERMES_X_USE_PREFLIGHT_MARKER", str(marker))
    return load_module(CONTROL_PATH, "runtime_control")


def test_health_capabilities_and_network_snapshot_are_public_only(control) -> None:
    control.NETWORK_SNAPSHOT = {
        "status": "healthy",
        "mode": "assigned_proxy",
        "exit_ip": "8.8.8.8",
    }

    assert control.capabilities() == [
        "x_use_mcp",
        "x_session_import",
        "x_draft_approval",
        "x_use_like",
        "x_like_tweet",
        "persistent_browser_profile",
        "remote_chromium",
        "network_status",
    ]
    assert control.network_snapshot() == {
        "status": "healthy",
        "mode": "assigned_proxy",
        "exit_ip": "8.8.8.8",
    }


def test_health_capabilities_include_direct_posting_only_when_enabled(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "x_direct_posting" not in control.capabilities()

    monkeypatch.setenv("HERMES_X_DIRECT_POSTING_ENABLED", "true")

    assert control.capabilities() == [
        "x_use_mcp",
        "x_session_import",
        "x_draft_approval",
        "x_direct_posting",
        "x_use_like",
        "x_like_tweet",
        "persistent_browser_profile",
        "remote_chromium",
        "network_status",
    ]


def test_direct_runtime_does_not_advertise_x_use_capabilities(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "direct")
    monkeypatch.delenv("RESIDENTIAL_PROXY_URL", raising=False)

    assert control.capabilities() == [
        "persistent_browser_profile",
        "remote_chromium",
        "network_status",
    ]


def test_x_use_capabilities_require_exact_private_native_preflight_marker(
    control,
) -> None:
    control.X_USE_PREFLIGHT_MARKER.unlink()
    assert control.x_use_preflight_ready() is False
    assert control.capabilities() == [
        "persistent_browser_profile",
        "remote_chromium",
        "network_status",
    ]

    control.X_USE_PREFLIGHT_MARKER.write_text(
        json.dumps(
            {
                "commit": control.X_USE_COMMIT,
                "tool_count": 13,
                "version": "2.4.1",
            }
        )
    )
    control.X_USE_PREFLIGHT_MARKER.chmod(0o600)
    assert control.x_use_preflight_ready() is False


def test_x_use_preflight_marker_rejects_group_readable_or_symlink(
    control, tmp_path: Path
) -> None:
    control.X_USE_PREFLIGHT_MARKER.chmod(0o640)
    assert control.x_use_preflight_ready() is False

    target = tmp_path / "target.json"
    target.write_text(
        json.dumps(
            {
                "commit": control.X_USE_COMMIT,
                "tool_count": 14,
                "version": "2.4.1",
            }
        )
    )
    link = tmp_path / "marker-link.json"
    link.symlink_to(target)
    control.X_USE_PREFLIGHT_MARKER = link
    assert control.x_use_preflight_ready() is False


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


def test_x_use_session_import_never_echoes_cookie_values(
    control, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = b'{"cookies":[{"name":"auth_token","value":"must-not-leak"}]}'
    monkeypatch.setattr(
        control,
        "import_session",
        lambda body: {
            "status": "ready",
            "configured": True,
            "session_present": True,
            "account_verified": True,
            "expected_handle": "expected_user",
            "authenticated_handle": "expected_user",
            "version": "2.4.1",
            "debug": body.decode(),
        },
    )

    status, payload = control.x_use_import_session(raw)

    assert status == 200
    assert payload == {
        "status": "ready",
        "configured": True,
        "session_present": True,
        "account_verified": True,
        "version": "2.4.1",
        "expected_handle": "expected_user",
        "authenticated_handle": "expected_user",
    }
    assert "must-not-leak" not in json.dumps(payload)


@pytest.mark.parametrize("state", ["ready", "not_configured", "wrong_account"])
def test_x_use_status_returns_200_for_every_valid_state_snapshot(
    control, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setattr(
        control,
        "live_status",
        lambda: {
            "status": state,
            "configured": True,
            "session_present": state != "not_configured",
            "account_verified": state == "ready",
            "expected_handle": "expected_user",
            "authenticated_handle": (
                "different_user" if state == "wrong_account" else "expected_user"
            ),
        },
    )

    http_status, payload = control.x_use_status()

    assert http_status == 200
    assert payload["status"] == state


def test_dashboard_draft_payload_exposes_content_but_not_local_media_paths(control) -> None:
    payload = control.safe_drafts_payload(
        {
            "drafts": [
                {
                    "draft_id": "draft-1",
                    "account": "expected_user",
                    "action": "post_tweet",
                    "payload": {
                        "text": "review me",
                    },
                    "preview": "must-not-be-trusted /secret/operator/file.png",
                    "created_at": "2026-08-22T00:00:00Z",
                    "status": "pending",
                }
            ]
        }
    )

    assert payload["drafts"][0]["payload"] == {"text": "review me"}
    assert "preview" not in payload["drafts"][0]
    assert "/secret/operator" not in json.dumps(payload)


def test_dashboard_drafts_drop_noncanonical_or_credential_marked_payloads(control) -> None:
    payload = control.safe_drafts_payload(
        {
            "drafts": [
                {
                    "draft_id": "extra-field",
                    "account": "expected_user",
                    "action": "post_tweet",
                    "payload": {"text": "hello", "media": []},
                    "status": "pending",
                },
                {
                    "draft_id": "secret",
                    "account": "expected_user",
                    "action": "post_tweet",
                    "payload": {"text": "Bearer abcdefghijkl"},
                    "status": "pending",
                },
                {
                    "draft_id": "bad-reply",
                    "account": "expected_user",
                    "action": "reply_to_tweet",
                    "payload": {
                        "text": "hello",
                        "tweet_url": "https://x.com/user/status/123",
                        "tweet_id": "456",
                    },
                    "status": "pending",
                },
            ]
        }
    )

    assert payload == {"drafts": [], "count": 0}


def test_dashboard_reply_payload_is_canonical_and_action_result_is_allowlisted(control) -> None:
    drafts = control.safe_drafts_payload(
        {
            "drafts": [
                {
                    "draft_id": "reply-1",
                    "account": "Expected_User",
                    "action": "reply_to_tweet",
                    "payload": {
                        "text": "hello",
                        "tweet_url": "https://x.com/some_user/status/123",
                        "tweet_id": "123",
                    },
                    "preview": "ignored",
                    "created_at": "2026-08-22T00:00:00Z",
                    "status": "pending",
                }
            ]
        }
    )
    action = control.safe_draft_action_payload(
        {
            "draft_id": "reply-1",
            "status": "executed",
            "result": {
                "account": "Expected_User",
                "action": "reply",
                "success": True,
                "tweet_id": "123",
                "cookies": "must-not-leak",
            },
        }
    )

    assert drafts["drafts"][0]["payload"] == {
        "text": "hello",
        "tweet_url": "https://x.com/some_user/status/123",
        "tweet_id": "123",
    }
    assert action["result"] == {
        "account": "expected_user",
        "action": "reply_to_tweet",
        "success": True,
        "tweet_id": "123",
    }
    assert "must-not-leak" not in json.dumps(action)
