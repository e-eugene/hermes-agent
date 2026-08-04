from __future__ import annotations

import importlib.util
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

    monkeypatch.setattr(helper, "ensure_sticky_proxy", lambda: calls.append("sticky"))
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
    assert calls[0] == "sticky"
    assert "reddit-secret" not in json.dumps(result)


def test_sticky_proxy_is_optional_for_direct_browser_runtime(
    helper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_ENABLED", "false")
    monkeypatch.setattr(
        helper.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("proxy helper should not be called"),
    )

    helper.ensure_sticky_proxy()


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

    monkeypatch.setattr(helper, "ensure_sticky_proxy", lambda: None)
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
