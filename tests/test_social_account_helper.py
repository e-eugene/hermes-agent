from __future__ import annotations

import importlib.util
import inspect
import json
from pathlib import Path

import pytest


HELPER_PATH = Path(__file__).parents[1] / "runtime" / "social-account.py"


@pytest.fixture
def helper():
    spec = importlib.util.spec_from_file_location("social_account_helper", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_account_never_returns_password(helper) -> None:
    account = {
        "type": "reddit",
        "login": "reddit-user",
        "password": "reddit-secret",
        "is_active": True,
    }

    result = helper.public_account(account)

    assert result == {
        "type": "reddit",
        "login": "reddit-user",
        "password_set": True,
        "is_active": True,
    }
    assert "reddit-secret" not in json.dumps(result)


def test_assigned_account_reads_active_runtime_secret(helper, tmp_path) -> None:
    helper.ACCOUNTS_PATH = tmp_path / "social-accounts.json"
    helper.ACCOUNTS_PATH.write_text(
        json.dumps(
            [
                {
                    "type": "reddit",
                    "login": "reddit-user",
                    "password": "reddit-secret",
                    "is_active": True,
                },
                {
                    "type": "x",
                    "login": "inactive-user",
                    "password": "inactive-secret",
                    "is_active": False,
                },
            ]
        )
    )

    account = helper.assigned_account("reddit")

    assert account["login"] == "reddit-user"
    with pytest.raises(RuntimeError, match="No active x account"):
        helper.assigned_account("x")


def test_reddit_login_uses_existing_browser_session_without_exposing_password(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeClient:
        def reddit_page_session(self):
            return "cdp-session"

        def navigate(self, session_id, url):
            calls.append(("navigate", session_id, url))

        def reddit_user(self, session_id):
            calls.append(("user", session_id))
            return "reddit-user"

    monkeypatch.setattr(
        helper, "verify_browser_network", lambda: calls.append("network")
    )
    result = helper.login_reddit(
        {
            "type": "reddit",
            "login": "reddit-user",
            "password": "reddit-secret",
            "is_active": True,
        },
        client=FakeClient(),
    )

    assert result == {
        "status": "already_logged_in",
        "type": "reddit",
        "login": "reddit-user",
    }
    assert calls[0] == "network"
    assert "reddit-secret" not in json.dumps(result)


def test_browser_network_check_is_optional_for_direct_runtime(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "direct")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_ENABLED", "false")
    monkeypatch.setattr(
        helper.socket,
        "create_connection",
        lambda *args, **kwargs: pytest.fail("proxy helper should not be called"),
    )

    helper.verify_browser_network()


def test_browser_network_check_verifies_bridge_without_rotating_proxy(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class Connection:
        def __enter__(self):
            calls.append("open")
            return self

        def __exit__(self, *_):
            calls.append("close")

    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "assigned_proxy")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_ENABLED", "true")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PORT", "8899")
    monkeypatch.setattr(
        helper.socket,
        "create_connection",
        lambda address, timeout: calls.append((address, timeout)) or Connection(),
    )

    helper.verify_browser_network()

    assert calls == [(("127.0.0.1", 8899), 2), "open", "close"]
    source = inspect.getsource(helper.verify_browser_network).lower()
    assert "residential-proxy" not in source
    assert '"sticky"' not in source
    assert '"rotate"' not in source


def test_browser_network_check_fails_closed_when_proxy_bridge_is_unavailable(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "assigned_proxy")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_ENABLED", "true")
    monkeypatch.setattr(
        helper.socket,
        "create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionRefusedError()),
    )

    with pytest.raises(RuntimeError, match="proxy bridge is unavailable"):
        helper.verify_browser_network()


def test_x_status_reads_login_without_requiring_password(helper, tmp_path) -> None:
    helper.ACCOUNTS_PATH = tmp_path / "social-accounts.json"
    helper.ACCOUNTS_PATH.write_text(
        json.dumps(
            [
                {
                    "type": "x",
                    "login": "assigned-handle",
                    "is_active": True,
                }
            ]
        )
    )

    result = helper.assigned_social_account("x", require_password=False)

    assert result == {
        "type": "x",
        "login": "assigned-handle",
        "is_active": True,
    }
    with pytest.raises(RuntimeError, match="has no credentials"):
        helper.assigned_social_account("x", require_password=True)


def test_passwordless_x_lookup_does_not_access_the_password_field(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PasswordTrap(dict):
        def get(self, key, default=None):
            if key == "password":
                pytest.fail("password must not be read for X status or manual login")
            return super().get(key, default)

    account = PasswordTrap(
        {"type": "x", "login": "assigned-handle", "is_active": True}
    )
    monkeypatch.setattr(helper, "load_accounts", lambda: [account])

    assert helper.assigned_social_account("x", require_password=False) is account


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            {
                "profile_href": "/Profile_123",
                "account_switcher_text": "Display Name\n@fallback",
            },
            "Profile_123",
        ),
        (
            {
                "profile_href": "https://x.com/absolute_user",
                "account_switcher_text": "",
            },
            "absolute_user",
        ),
        (
            {
                "profile_href": "/not/a/profile",
                "account_switcher_text": "Display Name\n@fallback_2",
            },
            "fallback_2",
        ),
        (
            {
                "profile_href": "/abcdefghijklmnop",
                "account_switcher_text": "@abcdefghijklmnop",
            },
            None,
        ),
        (
            {
                "profile_href": "https://example.com/not_x",
                "account_switcher_text": "no handle here",
            },
            None,
        ),
    ],
)
def test_x_identity_accepts_only_valid_profile_or_switcher_handles(
    helper,
    snapshot: dict[str, str],
    expected: str | None,
) -> None:
    assert helper.x_handle_from_snapshot(snapshot) == expected


def test_x_identity_poll_waits_for_react_hydration(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshots = iter(
        [
            {
                "url": "about:blank",
                "app_ready": False,
                "profile_href": "",
                "account_switcher_text": "",
                "challenge": None,
            },
            {
                "url": "https://x.com/home",
                "app_ready": False,
                "profile_href": "",
                "account_switcher_text": "",
                "challenge": None,
            },
            {
                "url": "https://x.com/home",
                "app_ready": True,
                "profile_href": "/hydrated_user",
                "account_switcher_text": "",
                "challenge": None,
            },
        ]
    )
    sleeps: list[float] = []
    monkeypatch.setattr(
        helper,
        "x_identity_snapshot",
        lambda cdp, session_id: next(snapshots),
    )
    monkeypatch.setattr(helper.time, "sleep", sleeps.append)

    assert helper.poll_x_identity(object(), "identity-session", timeout=1) == (
        "authenticated",
        "hydrated_user",
    )
    assert len(sleeps) == 2


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/i/flow/login?redirect_after_login=%2Fhome",
        "https://x.com/i/jf/onboarding?flow_name=login",
    ],
)
def test_x_identity_poll_requires_a_positive_logged_out_page(
    helper,
    monkeypatch: pytest.MonkeyPatch,
    url: str,
) -> None:
    monkeypatch.setattr(
        helper,
        "x_identity_snapshot",
        lambda cdp, session_id: {
            "url": url,
            "app_ready": True,
            "profile_href": "",
            "account_switcher_text": "",
            "challenge": None,
        },
    )

    assert helper.poll_x_identity(object(), "identity-session", timeout=0) == (
        "not_logged_in",
        None,
    )


def test_x_identity_poll_prefers_a_challenge_over_an_onboarding_url(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helper,
        "x_identity_snapshot",
        lambda cdp, session_id: {
            "url": "https://x.com/i/jf/onboarding?flow_name=login",
            "app_ready": True,
            "profile_href": "",
            "account_switcher_text": "",
            "challenge": "2FA",
        },
    )

    assert helper.poll_x_identity(object(), "identity-session", timeout=0) == (
        "manual_attention",
        "2FA",
    )


def test_x_identity_poll_does_not_misclassify_an_unsettled_or_unknown_page(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        helper,
        "x_identity_snapshot",
        lambda cdp, session_id: {
            "url": "https://x.com/home",
            "app_ready": True,
            "profile_href": "",
            "account_switcher_text": "",
            "challenge": None,
        },
    )

    assert helper.poll_x_identity(object(), "identity-session", timeout=0) == (
        "indeterminate",
        None,
    )


def test_x_identity_snapshot_rejects_an_invalid_cdp_result(helper) -> None:
    class FakeXBrowser:
        def evaluate(self, session_id, expression):
            return None

    with pytest.raises(RuntimeError, match="invalid response"):
        helper.x_identity_snapshot(FakeXBrowser(), "identity-session")


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (
            ("authenticated", "assigned-handle"),
            {
                "status": "authenticated",
                "type": "x",
                "login": "assigned-handle",
            },
        ),
        (
            ("authenticated", "other-handle"),
            {
                "status": "wrong_account",
                "type": "x",
                "login": "assigned-handle",
                "error": "Chromium is authenticated as another X account",
            },
        ),
        (
            ("manual_attention", "2FA"),
            {
                "status": "manual_attention",
                "type": "x",
                "login": "assigned-handle",
                "error": "X requires manual 2FA completion",
            },
        ),
        (
            ("not_logged_in", None),
            {
                "status": "not_logged_in",
                "type": "x",
                "login": "assigned-handle",
            },
        ),
        (
            ("indeterminate", None),
            {
                "status": "manual_attention",
                "type": "x",
                "login": "assigned-handle",
                "error": "X identity could not be verified; inspect Remote Chromium",
            },
        ),
    ],
)
def test_x_status_uses_and_closes_a_temporary_identity_target(
    helper,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[str, str | None],
    expected: dict[str, str],
) -> None:
    calls: list[object] = []

    class FakeXBrowser:
        def create_target(self, url, *, background):
            calls.append(("create", url, background))
            return "identity-target"

        def attach(self, target_id):
            calls.append(("attach", target_id))
            return "identity-session"

        def close_target(self, target_id):
            calls.append(("close", target_id))

    monkeypatch.setattr(
        helper,
        "poll_x_identity",
        lambda cdp, session_id: calls.append(("poll", session_id)) or identity,
    )

    result = helper.inspect_x(
        {
            "type": "x",
            "login": "assigned-handle",
            "password": "must-not-leak",
            "is_active": True,
        },
        cdp=FakeXBrowser(),
    )

    assert result == expected
    assert calls == [
        ("create", "https://x.com/home", True),
        ("attach", "identity-target"),
        ("poll", "identity-session"),
        ("close", "identity-target"),
    ]
    assert "must-not-leak" not in json.dumps(result)


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        (
            ("authenticated", "assigned-handle"),
            {
                "status": "authenticated",
                "type": "x",
                "login": "assigned-handle",
            },
        ),
        (
            ("manual_attention", "2FA"),
            {
                "status": "manual_attention",
                "type": "x",
                "login": "assigned-handle",
                "error": "X requires manual 2FA completion",
            },
        ),
    ],
)
def test_x_status_does_not_mask_a_result_when_temporary_target_close_fails(
    helper,
    monkeypatch: pytest.MonkeyPatch,
    identity: tuple[str, str | None],
    expected: dict[str, str],
) -> None:
    calls: list[object] = []

    class FakeXBrowser:
        def create_target(self, url, *, background):
            return "identity-target"

        def attach(self, target_id):
            return "identity-session"

        def close_target(self, target_id):
            calls.append(("close", target_id))
            raise RuntimeError("target already closed")

    monkeypatch.setattr(
        helper,
        "poll_x_identity",
        lambda cdp, session_id: identity,
    )

    result = helper.inspect_x(
        {"type": "x", "login": "assigned-handle", "is_active": True},
        cdp=FakeXBrowser(),
    )

    assert result == expected
    assert calls == [("close", "identity-target")]


def test_x_status_does_not_mask_identity_failure_when_target_close_also_fails(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeXBrowser:
        def create_target(self, url, *, background):
            return "identity-target"

        def attach(self, target_id):
            return "identity-session"

        def close_target(self, target_id):
            calls.append(("close", target_id))
            raise RuntimeError("target already closed")

    monkeypatch.setattr(
        helper,
        "poll_x_identity",
        lambda cdp, session_id: (_ for _ in ()).throw(RuntimeError("failed")),
    )

    with pytest.raises(RuntimeError, match="failed"):
        helper.inspect_x(
            {"type": "x", "login": "assigned-handle", "is_active": True},
            cdp=FakeXBrowser(),
        )

    assert calls == [("close", "identity-target")]


def test_x_passwordless_login_checks_identity_then_hands_off_to_operator(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeXBrowser:
        def present_login(self):
            calls.append("present")
            return "visible-login-target"

    monkeypatch.setattr(
        helper,
        "inspect_x",
        lambda account, *, cdp: calls.append(("identity", dict(account)))
        or {
            "status": "not_logged_in",
            "type": "x",
            "login": "assigned-handle",
        },
    )

    result = helper.login_x(
        {"type": "x", "login": "assigned-handle", "is_active": True},
        cdp=FakeXBrowser(),
    )

    assert calls == [
        (
            "identity",
            {"type": "x", "login": "assigned-handle", "is_active": True},
        ),
        "present",
    ]
    assert result == {
        "status": "manual_attention",
        "type": "x",
        "login": "assigned-handle",
        "error": "Complete X sign-in in Remote Chromium, then check status",
    }


@pytest.mark.parametrize("status", ["authenticated", "wrong_account"])
def test_x_login_does_not_replace_an_authenticated_or_wrong_account_page(
    helper,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    existing = {"status": status, "type": "x", "login": "assigned-handle"}
    monkeypatch.setattr(helper, "inspect_x", lambda account, *, cdp: existing)

    class FakeXBrowser:
        def present_login(self):
            pytest.fail("login tab must not be prepared for a terminal identity state")

    assert (
        helper.login_x(
            {"type": "x", "login": "assigned-handle", "is_active": True},
            cdp=FakeXBrowser(),
        )
        == existing
    )


@pytest.mark.parametrize(
    "auth_url",
    [
        "https://x.com/i/flow/login",
        "https://x.com/i/flow/verify_phone",
        "https://x.com/i/jf/onboarding/web?mode=login",
        "https://x.com/account/access?lang=en",
    ],
)
def test_x_login_reuses_and_focuses_an_existing_visible_auth_or_challenge_tab(
    helper,
    auth_url: str,
) -> None:
    calls: list[tuple[str, object]] = []
    browser = helper.XBrowser.__new__(helper.XBrowser)

    def request(method, params=None, **kwargs):
        calls.append((method, params or {}))
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    {
                        "type": "page",
                        "targetId": "challenge-target",
                        "url": auth_url,
                    }
                ]
            }
        return {}

    browser.request = request

    assert browser.present_login() == "challenge-target"
    assert calls == [
        ("Target.getTargets", {}),
        ("Target.activateTarget", {"targetId": "challenge-target"}),
    ]


def test_x_login_opens_and_focuses_a_new_visible_tab_when_none_exists(helper) -> None:
    calls: list[tuple[str, object]] = []
    browser = helper.XBrowser.__new__(helper.XBrowser)

    def request(method, params=None, **kwargs):
        calls.append((method, params or {}))
        if method == "Target.getTargets":
            return {"targetInfos": []}
        if method == "Target.createTarget":
            return {"targetId": "new-login-target"}
        return {}

    browser.request = request

    assert browser.present_login() == "new-login-target"
    assert calls == [
        ("Target.getTargets", {}),
        (
            "Target.createTarget",
            {"url": "https://x.com/i/flow/login", "background": False},
        ),
        ("Target.activateTarget", {"targetId": "new-login-target"}),
    ]


def test_x_login_preserves_a_non_auth_x_tab_and_opens_a_new_login_tab(
    helper,
) -> None:
    calls: list[tuple[str, object, str | None]] = []
    browser = helper.XBrowser.__new__(helper.XBrowser)

    def request(method, params=None, *, session_id=None, **kwargs):
        calls.append((method, params or {}, session_id))
        if method == "Target.getTargets":
            return {
                "targetInfos": [
                    {
                        "type": "page",
                        "targetId": "visible-x-target",
                        "url": "https://x.com/explore",
                    }
                ]
            }
        if method == "Target.createTarget":
            return {"targetId": "new-login-target"}
        return {}

    browser.request = request

    assert browser.present_login() == "new-login-target"
    assert calls == [
        ("Target.getTargets", {}, None),
        (
            "Target.createTarget",
            {"url": "https://x.com/i/flow/login", "background": False},
            None,
        ),
        ("Target.activateTarget", {"targetId": "new-login-target"}, None),
    ]
    assert all(method != "Page.navigate" for method, _, _ in calls)


def test_x_account_action_never_requires_or_passes_the_allocated_password(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    def assigned(account_type, *, require_password):
        calls.append((account_type, require_password))
        return {
            "type": "x",
            "login": "assigned-handle",
            "password": "must-not-leak",
            "is_active": True,
        }

    class FakeXBrowser:
        def close(self):
            calls.append("close")

    monkeypatch.setattr(helper, "assigned_social_account", assigned)
    monkeypatch.setattr(helper, "XBrowser", FakeXBrowser)
    monkeypatch.setattr(
        helper,
        "login_x",
        lambda account, *, cdp: calls.append(("login", dict(account)))
        or {
            "status": "manual_attention",
            "type": "x",
            "login": account["login"],
        },
    )

    result = helper.x_account_action("login")

    assert calls == [
        ("x", False),
        (
            "login",
            {"type": "x", "login": "assigned-handle", "is_active": True},
        ),
        "close",
    ]
    assert "must-not-leak" not in json.dumps(result)


def test_x_manual_login_source_has_no_credentials_selectors_or_screenshots(
    helper,
) -> None:
    source = "\n".join(
        inspect.getsource(value)
        for value in (
            helper.login_x,
            helper.x_account_action,
            helper.XBrowser.present_login,
        )
    ).lower()

    assert '["password"]' not in source
    assert '.get("password")' not in source
    for forbidden in (
        "queryselector",
        "autocomplete",
        "loginform_login_button",
        "inserttext",
        "capturescreenshot",
    ):
        assert forbidden not in source
    assert "page.navigate" not in source
    helper_source = HELPER_PATH.read_text().lower()
    identity_source = inspect.getsource(helper.x_identity_snapshot)
    assert '[data-testid="AppTabBar_Profile_Link"]' in identity_source
    assert '[data-testid="SideNav_AccountSwitcher_Button"]' in identity_source
    assert "fetch(" not in identity_source
    assert "/i/api/1.1/account/settings.json" not in helper_source
    assert "x-manual-attention.png" not in helper_source
    assert "page.capturescreenshot" not in helper_source


def test_reddit_login_fills_credentials_without_returning_password(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted: list[tuple[int, str]] = []
    users = iter([None, "reddit-user"])

    class FakeClient:
        def reddit_page_session(self):
            return "cdp-session"

        def navigate(self, session_id, url):
            return None

        def reddit_user(self, session_id):
            return next(users)

        def login_nodes(self, session_id):
            return 10, 20, 30

        def insert_text(self, session_id, node_id, value):
            inserted.append((node_id, value))

        def click(self, session_id, node_id):
            assert node_id == 30

    monkeypatch.setattr(helper, "verify_browser_network", lambda: None)
    monkeypatch.setattr(helper.time, "sleep", lambda _: None)
    result = helper.login_reddit(
        {
            "type": "reddit",
            "login": "reddit-user",
            "password": "reddit-secret",
            "is_active": True,
        },
        client=FakeClient(),
    )

    assert inserted == [(10, "reddit-user"), (20, "reddit-secret")]
    assert result["status"] == "logged_in"
    assert "reddit-secret" not in json.dumps(result)
