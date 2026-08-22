from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


COMMON_PATH = Path(__file__).parents[1] / "runtime" / "hermes_x_use_common.py"


@pytest.fixture
def common(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "assigned_proxy")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PORT", "8899")
    monkeypatch.setenv("RESIDENTIAL_PROXY_URL", "http://127.0.0.1:8899")
    spec = importlib.util.spec_from_file_location("hermes_x_use_common_test", COMMON_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_export() -> bytes:
    return json.dumps(
        [
            {
                "name": "auth_token",
                "value": "auth-secret",
                "domain": ".x.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "expirationDate": 4102444800,
            },
            {
                "name": "ct0",
                "value": "csrf-secret",
                "domain": ".x.com",
                "path": "/",
                "sameSite": "no_restriction",
                "secure": True,
                "expirationDate": 4102444800,
            },
            {
                "name": "unrelated",
                "value": "must-not-import",
                "domain": ".example.com",
            },
        ]
    ).encode()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            "https://Twitter.com/Some_User/status/123456789?utm_source=test#x",
            ("https://x.com/some_user/status/123456789", "123456789"),
        ),
        ("https://x.com/user/status/0", None),
        ("https://user:pass@x.com/user/status/123", None),
        ("https://example.com/user/status/123", None),
        ("https://x.com/user/likes/123", None),
    ],
)
def test_canonical_x_status_url(common, value, expected) -> None:
    if expected is None:
        with pytest.raises(ValueError):
            common.canonical_x_status_url(value)
    else:
        assert common.canonical_x_status_url(value) == expected


def test_cookie_export_requires_both_x_session_cookies_and_filters_other_sites(common) -> None:
    cookies = common.validate_session_export(valid_export())

    assert {item["name"] for item in cookies} == {"auth_token", "ct0"}
    assert cookies[1]["sameSite"] == "None"
    assert "must-not-import" not in json.dumps(cookies)

    missing_ct0 = json.dumps(
        [
            {
                "name": "auth_token",
                "value": "secret",
                "domain": ".x.com",
                "expirationDate": 4102444800,
            }
        ]
    ).encode()
    with pytest.raises(common.SessionImportError, match="auth_token and ct0"):
        common.validate_session_export(missing_ct0)


def test_cookie_export_is_bounded(common) -> None:
    with pytest.raises(common.SessionImportError, match="512 KiB"):
        common.validate_session_export(b"x" * (common.MAX_SESSION_BYTES + 1))


def test_x_use_fails_closed_without_assigned_proxy_before_any_cdp_mutation(
    common, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "direct")
    monkeypatch.delenv("RESIDENTIAL_PROXY_URL", raising=False)
    factory_called = False

    def client_factory():
        nonlocal factory_called
        factory_called = True
        pytest.fail("direct mode must fail before connecting to CDP")

    with pytest.raises(common.RuntimeConfigurationError, match="assigned proxy"):
        common.live_status(client_factory=client_factory)
    with pytest.raises(common.RuntimeConfigurationError, match="assigned proxy"):
        common.import_session(valid_export(), client_factory=client_factory)
    assert factory_called is False


def test_import_applies_cookies_in_memory_and_never_creates_a_cookie_file(
    common, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    accounts = tmp_path / "social-accounts.json"
    accounts.write_text(
        json.dumps(
            [{"type": "x", "login": "Expected_User", "is_active": True}]
        )
    )
    monkeypatch.setattr(common, "SOCIAL_ACCOUNTS_PATH", accounts)
    calls: list[tuple[str, object]] = []

    class FakeClient:
        def request(self, method, params=None, **kwargs):
            calls.append((method, params))
            if method == "Storage.setCookies":
                return {"success": True}
            if method == "Storage.getCookies":
                return {
                    "cookies": [
                        {
                            "name": "auth_token",
                            "value": "stale-a",
                            "domain": "x.com",
                            "path": "/",
                            "expires": 4102444800,
                        },
                        {
                            "name": "auth_token",
                            "value": "a",
                            "domain": ".x.com",
                            "path": "/",
                            "expires": 4102444800,
                        },
                        {
                            "name": "ct0",
                            "value": "b",
                            "domain": ".x.com",
                            "path": "/",
                            "expires": 4102444800,
                        },
                    ]
                }
            return {}

        def close(self):
            calls.append(("close", None))

    monkeypatch.setattr(common, "inspect_identity", lambda client: "expected_user")
    result = common.import_session(valid_export(), client_factory=FakeClient)

    assert result["status"] == "ready"
    assert result["account_verified"] is True
    set_calls = [params for method, params in calls if method == "Storage.setCookies"]
    set_call = set_calls[-1]
    assert {item["name"] for item in set_call["cookies"]} == set()
    deleted = set_calls[0]["cookies"]
    assert {item["domain"] for item in deleted} == {"x.com", ".x.com"}
    assert all(item["value"] == "" and item["expires"] == 1 for item in deleted)
    assert list(tmp_path.rglob("*cookies*.json")) == []


def test_required_session_cookies_must_be_persistent_and_unexpired(common) -> None:
    expired = json.loads(valid_export())
    for item in expired:
        if item.get("name") in {"auth_token", "ct0"}:
            item["expirationDate"] = 1
    with pytest.raises(common.SessionImportError, match="future expiration"):
        common.validate_session_export(json.dumps(expired).encode())


@pytest.mark.parametrize("expiration", [True, float("inf"), float("nan")])
def test_required_cookie_expiration_rejects_bool_and_nonfinite(
    common, expiration
) -> None:
    payload = json.loads(valid_export())
    for item in payload:
        if item.get("name") in {"auth_token", "ct0"}:
            item["expirationDate"] = expiration
    raw = json.dumps(payload, allow_nan=True).encode()

    with pytest.raises(common.SessionImportError, match="future expiration"):
        common.validate_session_export(raw)


def test_uploaded_required_cookie_duplicates_are_rejected(common) -> None:
    duplicated = json.loads(valid_export())
    duplicated.append(
        {
            "name": "auth_token",
            "value": "another-session",
            "domain": "twitter.com",
            "expirationDate": 4102444800,
        }
    )

    with pytest.raises(common.SessionImportError, match="exactly one"):
        common.validate_session_export(json.dumps(duplicated).encode())


def test_status_treats_session_only_or_expired_required_cookies_as_absent(
    common, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    accounts = tmp_path / "accounts.json"
    accounts.write_text(
        json.dumps([{"type": "x", "login": "expected_user", "is_active": True}])
    )
    monkeypatch.setattr(common, "SOCIAL_ACCOUNTS_PATH", accounts)

    class FakeClient:
        def request(self, method, params=None, **kwargs):
            assert method == "Storage.getCookies"
            return {
                "cookies": [
                    {"name": "auth_token", "value": "a", "domain": ".x.com", "expires": -1},
                    {"name": "ct0", "value": "b", "domain": ".x.com", "expires": 1},
                ]
            }

        def close(self):
            return None

    result = common.live_status(client_factory=FakeClient)
    assert result["status"] == "not_configured"
    assert result["session_present"] is False


def test_status_treats_nonfinite_cookie_expiration_as_absent(
    common, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    accounts = tmp_path / "accounts.json"
    accounts.write_text(
        json.dumps([{"type": "x", "login": "expected_user", "is_active": True}])
    )
    monkeypatch.setattr(common, "SOCIAL_ACCOUNTS_PATH", accounts)

    class FakeClient:
        def request(self, method, params=None, **kwargs):
            return {
                "cookies": [
                    {
                        "name": "auth_token",
                        "value": "a",
                        "domain": ".x.com",
                        "expires": float("inf"),
                    },
                    {
                        "name": "ct0",
                        "value": "b",
                        "domain": ".x.com",
                        "expires": float("nan"),
                    },
                ]
            }

        def close(self):
            return None

    result = common.live_status(client_factory=FakeClient)
    assert result["status"] == "not_configured"
    assert result["session_present"] is False


def test_identity_probe_owns_and_closes_only_its_temporary_target(common) -> None:
    calls: list[tuple[str, object]] = []

    class FakeClient:
        def request(self, method, params=None, **kwargs):
            calls.append((method, params))
            if method == "Target.createTarget":
                return {"targetId": "owned-target"}
            if method == "Target.attachToTarget":
                return {"sessionId": "owned-session"}
            if method == "Runtime.evaluate":
                return {
                    "result": {
                        "value": {
                            "url": "https://x.com/home",
                            "app_ready": True,
                            "profile_href": "/Expected_User",
                            "account_switcher_text": "",
                        }
                    }
                }
            return {}

    assert common.inspect_identity(FakeClient(), timeout=0) == "expected_user"
    assert ("Target.closeTarget", {"targetId": "owned-target"}) in calls
    assert all("operator-target" not in str(call) for call in calls)


def test_identity_never_trusts_free_form_account_switcher_text(common) -> None:
    assert (
        common.handle_from_snapshot(
            {
                "profile_href": "",
                "account_switcher_text": "Display @expected_user\n@wrong_user",
            }
        )
        is None
    )
    assert (
        common.handle_from_snapshot(
            {
                "profile_href": "/wrong_user",
                "account_switcher_text": "Display @expected_user",
            }
        )
        == "wrong_user"
    )


@pytest.mark.parametrize(
    "href",
    [
        "http://x.com/expected_user",
        "https://example.com/expected_user",
        "https://user:pass@x.com/expected_user",
        "https://x.com:444/expected_user",
        "/expected_user?redirect=/wrong_user",
    ],
)
def test_identity_requires_a_canonical_x_profile_href(common, href: str) -> None:
    assert common.handle_from_snapshot({"profile_href": href}) is None


def test_assignment_requires_exactly_one_active_x_account(common, tmp_path) -> None:
    path = tmp_path / "accounts.json"
    path.write_text("[]")
    with pytest.raises(common.RuntimeConfigurationError, match="Exactly one"):
        common.load_expected_handle(path)

    path.write_text(
        json.dumps(
            [
                {"type": "x", "login": "one", "is_active": True},
                {"type": "x", "login": "two", "is_active": True},
            ]
        )
    )
    with pytest.raises(common.RuntimeConfigurationError, match="Exactly one"):
        common.load_expected_handle(path)


def test_browser_action_lock_is_permission_safe_and_bounded(
    common, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "locks" / "browser-action.lock"
    monkeypatch.setattr(common, "BROWSER_ACTION_LOCK_PATH", path)
    first = common.acquire_browser_action_lock(timeout_seconds=0)
    try:
        with pytest.raises(common.BrowserActionBusyError, match="busy"):
            common.acquire_browser_action_lock(timeout_seconds=0)
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    finally:
        common.release_browser_action_lock(first)
