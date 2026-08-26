from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / "runtime"
MCP_ENTRYPOINT = RUNTIME / "hermes-x-use-mcp.py"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))


@pytest.fixture
def adapter(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "assigned_proxy")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PORT", "8899")
    monkeypatch.setenv("RESIDENTIAL_PROXY_URL", "http://127.0.0.1:8899")
    monkeypatch.delenv("HERMES_X_DIRECT_POSTING_ENABLED", raising=False)
    common = importlib.import_module("hermes_x_use_common")
    module = importlib.import_module("hermes_x_use_adapter")
    settings = tmp_path / "config" / "settings.json"
    accounts = tmp_path / "config" / "accounts.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "browser_settings": {"type": "chrome"},
                "mcp": {
                    "draft_mode": True,
                    "drafts_file": str(tmp_path / "data" / "drafts.jsonl"),
                },
                "queue": {
                    "store_file": str(tmp_path / "queue.jsonl"),
                    "auto_drain": {"enabled": False},
                },
                "twitter_automation": {
                    "processed_tweets_file": str(tmp_path / "processed.csv")
                },
            }
        )
    )
    accounts.write_text(
        json.dumps([{"account_id": "expected_user", "is_active": True}])
    )
    data = tmp_path / "x-use"
    monkeypatch.setattr(module, "X_USE_SETTINGS_PATH", settings)
    monkeypatch.setattr(module, "X_USE_ACCOUNTS_PATH", accounts)
    monkeypatch.setattr(module, "X_USE_DATA_DIR", data)
    monkeypatch.setattr(module, "load_expected_handle", lambda: "expected_user")
    monkeypatch.setattr(common, "load_expected_handle", lambda: "expected_user")
    return module


def test_server_exposes_only_the_curated_tool_allowlist(adapter) -> None:
    server = adapter.create_safe_server()
    names = {tool.name for tool in asyncio.run(server.list_tools())}

    assert names == adapter.MCP_ALLOWED_TOOLS
    assert names.isdisjoint(adapter.MCP_FORBIDDEN_TOOLS)
    assert "approve_draft" not in names
    assert "run_cycle" not in names
    assert asyncio.run(server.list_prompts()) == []
    assert asyncio.run(server.list_resources()) == []
    asyncio.run(adapter.shutdown_safe_server(server))


def call_tool(server, name: str, arguments: dict[str, object]) -> dict[str, object]:
    result = asyncio.run(server.call_tool(name, arguments))
    content = result[0] if isinstance(result, tuple) else result
    assert content
    return json.loads(content[0].text)


def install_single_tweet_fixture(adapter, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    class FakeScraper:
        def __init__(self, browser_manager, account_id):
            assert account_id == "expected_user"

        def scrape_tweets_from_url(self, tweet_url, kind, limit):
            assert kind == "tweet"
            assert limit == adapter.MAX_SINGLE_TWEET_CANDIDATES
            calls.append(tweet_url)
            return [
                SimpleNamespace(
                    tweet_id="123",
                    user_handle="@Some_User",
                    text_content="tweet text",
                    like_count=1,
                    retweet_count=2,
                    reply_count=3,
                    view_count=4,
                    media=[],
                )
            ]

    class FakePool:
        idle_timeout_seconds = 600

        def find_account_dict(self, account_id):
            return {"account_id": account_id, "is_active": True}

        @asynccontextmanager
        async def session(self, account_id):
            yield object()

        async def close_all(self):
            return None

    monkeypatch.setattr(adapter, "TweetScraper", FakeScraper)
    monkeypatch.setattr(adapter, "session_pool", lambda loader: FakePool())
    return calls


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (
            {
                "status": "ready",
                "configured": True,
                "session_present": True,
                "account_verified": True,
                "expected_handle": "expected_user",
                "authenticated_handle": "expected_user",
            },
            {
                "status": "ready",
                "session_present": True,
                "account_verified": True,
            },
        ),
        (
            {
                "status": "not_configured",
                "configured": True,
                "session_present": False,
                "account_verified": False,
                "expected_handle": "expected_user",
            },
            {
                "status": "not_configured",
                "session_present": False,
                "account_verified": False,
            },
        ),
        (
            {
                "status": "wrong_account",
                "configured": True,
                "session_present": True,
                "account_verified": False,
                "expected_handle": "expected_user",
                "authenticated_handle": "different_user",
            },
            {
                "status": "wrong_account",
                "session_present": True,
                "account_verified": False,
            },
        ),
    ],
)
def test_account_health_uses_live_cdp_state_without_cookie_file_details(
    adapter, monkeypatch: pytest.MonkeyPatch, snapshot, expected
) -> None:
    monkeypatch.setattr(adapter, "live_status", lambda: snapshot)
    server = adapter.create_safe_server()

    result = call_tool(server, "get_account_health", {"account": "expected_user"})

    assert result["ok"] is True
    for key, value in expected.items():
        assert result[key] == value
    assert "cookies" not in result
    assert "cookie_file" not in json.dumps(result).lower()
    asyncio.run(adapter.shutdown_safe_server(server))


def test_account_health_rejects_an_unassigned_handle_without_a_cdp_probe(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        adapter,
        "live_status",
        lambda: pytest.fail("unassigned account must fail before a browser probe"),
    )
    server = adapter.create_safe_server()

    result = call_tool(server, "get_account_health", {"account": "other_user"})

    assert result["ok"] is False
    assert "not assigned" in result["error"]["message"]
    asyncio.run(adapter.shutdown_safe_server(server))


@pytest.mark.parametrize("tool_name", ["get_tweet", "prepare_reply"])
@pytest.mark.parametrize(
    "tweet_url",
    [
        "https://attacker.example/status/1",
        "https://x.com.attacker.example/user/status/1",
        "file:///tmp/status/1",
        "javascript://x.com/user/status/1",
    ],
)
def test_single_tweet_tools_reject_non_x_navigation_before_browser_use(
    adapter, monkeypatch: pytest.MonkeyPatch, tool_name: str, tweet_url: str
) -> None:
    import xuse.mcp.tools as upstream_tools

    async def unexpected_scrape(*args, **kwargs):
        pytest.fail("invalid caller URL must fail before browser navigation")

    monkeypatch.setattr(upstream_tools, "scrape_single_tweet", unexpected_scrape)
    server = adapter.create_safe_server()

    result = call_tool(
        server,
        tool_name,
        {
            "account": "expected_user",
            "tweet_url": tweet_url,
            "include_images": False,
        },
    )

    assert result["ok"] is False
    assert "https://x.com tweet URL" in result["error"]["message"]
    asyncio.run(adapter.shutdown_safe_server(server))


@pytest.mark.parametrize("tool_name", ["get_tweet", "prepare_reply"])
def test_single_tweet_tools_navigate_only_to_canonical_x_url(
    adapter, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    calls: list[tuple[str, str]] = []

    class FakeScraper:
        def __init__(self, browser_manager, account_id):
            assert account_id == "expected_user"

        def scrape_tweets_from_url(self, tweet_url, kind, limit):
            assert kind == "tweet"
            assert limit == adapter.MAX_SINGLE_TWEET_CANDIDATES
            calls.append((tweet_url, "123"))
            return [
                SimpleNamespace(
                    tweet_id="123",
                    user_handle="@Some_User",
                    text_content="tweet text",
                    like_count=1,
                    retweet_count=2,
                    reply_count=3,
                    view_count=4,
                    media=[],
                )
            ]

    class FakePool:
        idle_timeout_seconds = 600

        def find_account_dict(self, account_id):
            return {"account_id": account_id, "is_active": True}

        @asynccontextmanager
        async def session(self, account_id):
            yield object()

        async def close_all(self):
            return None

    monkeypatch.setattr(adapter, "TweetScraper", FakeScraper)
    monkeypatch.setattr(adapter, "session_pool", lambda loader: FakePool())
    server = adapter.create_safe_server()

    result = call_tool(
        server,
        tool_name,
        {
            "account": "@Expected_User",
            "tweet_url": "https://x.com/i/web/status/123",
            "include_images": False,
        },
    )

    assert result["ok"] is True
    assert result["tweet_url"] == "https://x.com/i/web/status/123"
    assert calls == [("https://x.com/i/web/status/123", "123")]
    asyncio.run(adapter.shutdown_safe_server(server))


@pytest.mark.parametrize("tool_name", ["get_tweet", "prepare_reply"])
def test_single_tweet_tools_do_not_fetch_images_by_default(
    adapter, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    calls = install_single_tweet_fixture(adapter, monkeypatch)
    monkeypatch.setattr(
        adapter,
        "images_for_tweet",
        lambda _tweet: pytest.fail("default single-tweet calls must not fetch images"),
    )
    server = adapter.create_safe_server()

    result = call_tool(
        server,
        tool_name,
        {
            "account": "expected_user",
            "tweet_url": "https://x.com/some_user/status/123",
        },
    )

    assert result["ok"] is True
    assert result["text_content"] == "tweet text"
    assert calls == ["https://x.com/some_user/status/123"]
    asyncio.run(adapter.shutdown_safe_server(server))


@pytest.mark.parametrize("tool_name", ["get_tweet", "prepare_reply"])
def test_low_data_mode_ignores_include_images_true_without_failing(
    adapter, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    calls = install_single_tweet_fixture(adapter, monkeypatch)
    monkeypatch.setenv("HERMES_X_LOW_DATA_MODE", "true")
    monkeypatch.setattr(
        adapter,
        "images_for_tweet",
        lambda _tweet: pytest.fail("low-data mode must not download images"),
    )
    server = adapter.create_safe_server()

    result = call_tool(
        server,
        tool_name,
        {
            "account": "expected_user",
            "tweet_url": "https://x.com/some_user/status/123",
            "include_images": True,
        },
    )

    assert result["ok"] is True
    assert result["text_content"] == "tweet text"
    assert calls == ["https://x.com/some_user/status/123"]
    asyncio.run(adapter.shutdown_safe_server(server))


@pytest.mark.parametrize(
    ("tool_name", "include_exact", "expected_ok"),
    [
        ("get_tweet", True, True),
        ("prepare_reply", False, False),
    ],
)
def test_single_tweet_tools_require_the_exact_status_id_not_thread_ancestor(
    adapter,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    include_exact: bool,
    expected_ok: bool,
) -> None:
    class FakeScraper:
        def __init__(self, browser_manager, account_id):
            assert account_id == "expected_user"

        def scrape_tweets_from_url(self, tweet_url, kind, limit):
            assert tweet_url == "https://x.com/target_user/status/222"
            assert kind == "tweet"
            assert limit == adapter.MAX_SINGLE_TWEET_CANDIDATES
            candidates = [
                SimpleNamespace(
                    tweet_id="111",
                    user_handle="@Ancestor",
                    text_content="ancestor text must never be returned",
                    like_count=0,
                    retweet_count=0,
                    reply_count=0,
                    view_count=0,
                    media=[],
                )
            ]
            if include_exact:
                candidates.append(
                    SimpleNamespace(
                        tweet_id="222",
                        user_handle="@Target_User",
                        text_content="exact target text",
                        like_count=1,
                        retweet_count=2,
                        reply_count=3,
                        view_count=4,
                        media=[],
                    )
                )
            return candidates

    class FakePool:
        idle_timeout_seconds = 600

        def find_account_dict(self, account_id):
            return {"account_id": account_id, "is_active": True}

        @asynccontextmanager
        async def session(self, account_id):
            yield object()

        async def close_all(self):
            return None

    monkeypatch.setattr(adapter, "TweetScraper", FakeScraper)
    monkeypatch.setattr(adapter, "session_pool", lambda loader: FakePool())
    server = adapter.create_safe_server()
    result = call_tool(
        server,
        tool_name,
        {
            "account": "expected_user",
            "tweet_url": "https://x.com/Target_User/status/222",
            "include_images": False,
        },
    )

    assert result["ok"] is expected_ok
    assert "ancestor text" not in json.dumps(result)
    if expected_ok:
        assert result["tweet_id"] == "222"
        assert result["text_content"] == "exact target text"
    else:
        assert "Could not load tweet 222" in result["error"]["message"]
    asyncio.run(adapter.shutdown_safe_server(server))


@pytest.mark.parametrize(
    "query",
    [
        "unsafe\nquery",
        "auth_token=secret",
        "x" * 257,
        "   ",
    ],
)
def test_search_query_rejects_controls_credentials_and_oversize(adapter, query) -> None:
    with pytest.raises(adapter.ToolError):
        adapter.validate_search_query(query)


def test_search_query_is_normalized_and_bounded(adapter) -> None:
    assert adapter.validate_search_query("  safe topic  ") == "safe topic"
    assert adapter.validate_search_query("x" * 256) == "x" * 256


def test_stage_text_rejects_silent_truncation(adapter) -> None:
    assert adapter.validate_staged_text(" exact text ") == "exact text"
    with pytest.raises(adapter.ToolError, match="270-character"):
        adapter.validate_staged_text("x" * 271)


@pytest.mark.parametrize(
    "text",
    [
        "auth_token=top-secret",
        "ct0: top-secret",
        "Authorization: Bearer abcdefghijkl",
        "api_key=top-secret",
        "cookie=session-secret",
        "proxy_password=top-secret",
        "look at https://operator:password@example.com/path",
        "unsafe\x7ftext",
        "multiline\ntext",
    ],
)
def test_stage_text_rejects_credential_like_or_control_content(adapter, text) -> None:
    with pytest.raises(adapter.ToolError, match="credential-like"):
        adapter.validate_staged_text(text)


def test_stage_text_allows_noncredential_discussion(adapter) -> None:
    assert (
        adapter.validate_staged_text("We updated our Cookie Policy and API design.")
        == "We updated our Cookie Policy and API design."
    )


def test_stage_tools_persist_only_canonical_execution_payloads(adapter) -> None:
    server = adapter.create_safe_server()

    post = call_tool(
        server,
        "post_tweet",
        {"account": "expected_user", "text": "review this"},
    )
    reply = call_tool(
        server,
        "reply_to_tweet",
        {
            "account": "expected_user",
            "tweet_url": "https://Twitter.com/Some_User/status/123?tracking=yes",
            "text": "reply text",
        },
    )

    assert post["payload"] == {"text": "review this"}
    assert reply["payload"] == {
        "text": "reply text",
        "tweet_url": "https://x.com/some_user/status/123",
        "tweet_id": "123",
    }
    asyncio.run(adapter.shutdown_safe_server(server))


def test_direct_posting_mode_executes_post_without_creating_a_draft(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    pacing: list[str] = []

    async def fake_post(ctx, account_id: str, text: str, media=None, community=None):
        calls.append((account_id, text))
        return {"account": account_id, "action": "post_tweet", "success": True}

    async def fake_pacing(ctx, account: str):
        pacing.append(account)

    monkeypatch.setenv("HERMES_X_DIRECT_POSTING_ENABLED", "true")
    monkeypatch.setattr(adapter.actions, "exec_post", fake_post)
    monkeypatch.setattr(adapter, "reserve_persistent_action_pacing", fake_pacing)
    server = adapter.create_safe_server()

    result = call_tool(
        server,
        "post_tweet",
        {"account": "expected_user", "text": "publish now"},
    )

    assert result == {
        "ok": True,
        "account": "expected_user",
        "action": "post_tweet",
        "success": True,
        "direct_posting": True,
        "message": (
            "Published directly because direct X posting is enabled for this "
            "Hermes instance."
        ),
    }
    assert calls == [("expected_user", "publish now")]
    assert pacing == ["expected_user"]
    assert server.xuse_ctx.draft_store.list() == []
    asyncio.run(adapter.shutdown_safe_server(server))


def test_direct_posting_mode_executes_reply_to_canonical_tweet(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str, str]] = []

    async def fake_reply(
        ctx,
        account_id: str,
        tweet_url: str,
        reply_text: str,
        tweet_id: str | None = None,
        text_content: str = "",
    ):
        calls.append((account_id, tweet_url, reply_text, tweet_id or ""))
        return {
            "account": account_id,
            "action": "reply_to_tweet",
            "tweet_id": tweet_id,
            "success": True,
        }

    async def fake_pacing(ctx, account: str):
        return None

    monkeypatch.setenv("HERMES_X_DIRECT_POSTING_ENABLED", "1")
    monkeypatch.setattr(adapter.actions, "exec_reply", fake_reply)
    monkeypatch.setattr(adapter, "reserve_persistent_action_pacing", fake_pacing)
    server = adapter.create_safe_server()

    result = call_tool(
        server,
        "reply_to_tweet",
        {
            "tweet_url": "https://x.com/i/web/status/123",
            "text": "direct reply",
        },
    )

    assert result == {
        "ok": True,
        "account": "expected_user",
        "action": "reply_to_tweet",
        "success": True,
        "direct_posting": True,
        "message": (
            "Published directly because direct X posting is enabled for this "
            "Hermes instance."
        ),
        "tweet_id": "123",
        "tweet_url": "https://x.com/i/web/status/123",
        "receipt": {
            "action": "reply",
            "status": "accepted",
            "target_tweet_url": "https://x.com/i/web/status/123",
            "reply_text": "direct reply",
        },
    }
    assert calls == [
        (
            "expected_user",
            "https://x.com/i/web/status/123",
            "direct reply",
            "123",
        )
    ]
    assert server.xuse_ctx.draft_store.list() == []
    asyncio.run(adapter.shutdown_safe_server(server))


def reply_confirmation_browser(
    adapter,
    *,
    reply_visible: bool,
    reply_text: str = "Exact accepted reply",
    reply_href: str = "https://x.com/expected_user/status/456",
    disclosure_label: str | None = None,
    reply_delay_polls: int = 0,
    disclosure_delay_polls: int = 0,
):
    """Build a minimal source-thread DOM for read-only receipt proofs."""

    class TextNode:
        text = reply_text

    class Anchor:
        def get_attribute(self, name: str):
            return reply_href if name == "href" else None

    class Article:
        def find_elements(self, _by: str, selector: str):
            if selector == ".//*[@data-testid='tweetText']":
                return [TextNode()]
            if selector == ".//a[@href and .//time]":
                return [Anchor()]
            return []

    class Disclosure:
        text = disclosure_label or ""

        def __init__(self, driver):
            self.driver = driver
            self.clicks = 0

        def get_attribute(self, name: str):
            if name == "innerText":
                return self.text
            if name == "data-testid":
                return None
            if name == "style":
                return ""
            if name in {"aria-label", "title", "aria-controls", "aria-expanded"}:
                return None
            return None

        def click(self):
            self.clicks += 1
            self.driver.reply_visible = True

    class Driver:
        def __init__(self):
            self.reply_visible = reply_visible
            self.disclosure = Disclosure(self) if disclosure_label else None
            self.page_load_timeouts: list[float] = []
            self.reply_polls = 0
            self.disclosure_polls = 0

        def find_elements(self, _by: str, selector: str):
            if selector == adapter.TWEET_ARTICLE_XPATH:
                self.reply_polls += 1
                return (
                    [Article()]
                    if self.reply_visible and self.reply_polls > reply_delay_polls
                    else []
                )
            if selector == adapter.REPLY_DISCLOSURE_CONTROL_XPATH:
                self.disclosure_polls += 1
                return (
                    [self.disclosure]
                    if self.disclosure
                    and self.disclosure_polls > disclosure_delay_polls
                    else []
                )
            return []

        def set_page_load_timeout(self, value: float):
            self.page_load_timeouts.append(value)

    class Manager:
        def __init__(self, driver):
            self.driver = driver
            self.navigations: list[str] = []

        def navigate_to(self, url: str, ensure_driver: bool = True):
            assert ensure_driver is False
            self.navigations.append(url)
            return True

        def get_driver(self):
            pytest.fail("confirmation must not reattach a browser target")

    driver = Driver()
    return Manager(driver), driver


def install_confirmation_clock(adapter, monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Advance the bounded DOM-poll clock without making tests wait five seconds."""

    now = 0.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(adapter.time, "monotonic", monotonic)
    monkeypatch.setattr(adapter.time, "sleep", sleep)
    return sleeps


def test_reply_confirmation_checks_source_thread_with_absolute_status_href(adapter) -> None:
    manager, driver = reply_confirmation_browser(adapter, reply_visible=True)
    evidence: dict[str, object] = {}

    result = adapter._confirmed_direct_reply_url(
        manager,
        "expected_user",
        "123",
        "Exact accepted reply",
        "https://x.com/source_user/status/123",
        evidence,
    )

    assert result == "https://x.com/expected_user/status/456"
    assert manager.navigations == ["https://x.com/source_user/status/123"]
    assert driver.disclosure is None
    assert driver.page_load_timeouts == [
        adapter.CONFIRMATION_PAGE_LOAD_TIMEOUT_SECONDS,
        adapter.DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS,
    ]
    assert evidence == {}


@pytest.mark.parametrize(
    "disclosure_label",
    ["Show probable spam", "Показать возможный спам"],
)
def test_reply_confirmation_reveals_localized_hidden_spam_reply(
    adapter, monkeypatch: pytest.MonkeyPatch, disclosure_label: str
) -> None:
    manager, driver = reply_confirmation_browser(
        adapter,
        reply_visible=False,
        disclosure_label=disclosure_label,
    )
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    evidence: dict[str, object] = {}

    result = adapter._confirmed_direct_reply_url(
        manager,
        "expected_user",
        "123",
        "Exact accepted reply",
        "https://x.com/source_user/status/123",
        evidence,
    )

    assert result == "https://x.com/expected_user/status/456"
    assert driver.disclosure is not None
    assert driver.disclosure.clicks == 1
    assert manager.navigations == ["https://x.com/source_user/status/123"]
    assert driver.page_load_timeouts == [
        adapter.CONFIRMATION_PAGE_LOAD_TIMEOUT_SECONDS,
        adapter.DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS,
    ]
    assert evidence == {
        "proof_source": "source_thread_hidden_spam",
        "hidden_spam_disclosed": True,
    }


def test_reply_confirmation_accepts_assigned_account_keyword_match(adapter) -> None:
    manager, _driver = reply_confirmation_browser(
        adapter,
        reply_visible=True,
        reply_text="PersProtect.com can help reduce exposed personal data.",
    )
    evidence: dict[str, object] = {}

    result = adapter._confirmed_direct_reply_url(
        manager,
        "expected_user",
        "123",
        None,
        "https://x.com/source_user/status/123",
        evidence,
        ("persprotect.com", "persprotect"),
    )

    assert result == "https://x.com/expected_user/status/456"
    assert evidence == {
        "matched_text": "PersProtect.com can help reduce exposed personal data."
    }


def test_reply_confirmation_accepts_hidden_spam_keyword_match(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, driver = reply_confirmation_browser(
        adapter,
        reply_visible=False,
        reply_text="PersProtect helps reduce exposed personal data.",
        disclosure_label="Show probable spam",
    )
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    evidence: dict[str, object] = {}

    result = adapter._confirmed_direct_reply_url(
        manager,
        "expected_user",
        "123",
        None,
        "https://x.com/source_user/status/123",
        evidence,
        ("persprotect.com", "persprotect"),
    )

    assert result == "https://x.com/expected_user/status/456"
    assert driver.disclosure is not None
    assert driver.disclosure.clicks == 1
    assert evidence == {
        "matched_text": "PersProtect helps reduce exposed personal data.",
        "proof_source": "source_thread_hidden_spam",
        "hidden_spam_disclosed": True,
    }


@pytest.mark.parametrize("kind", ["reply", "hidden_spam"])
def test_reply_confirmation_waits_for_delayed_source_dom(
    adapter, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    manager, driver = reply_confirmation_browser(
        adapter,
        reply_visible=kind == "reply",
        disclosure_label="Show probable spam" if kind == "hidden_spam" else None,
        reply_delay_polls=2 if kind == "reply" else 0,
        disclosure_delay_polls=2 if kind == "hidden_spam" else 0,
    )
    sleeps = install_confirmation_clock(adapter, monkeypatch)
    evidence: dict[str, object] = {}

    result = adapter._confirmed_direct_reply_url(
        manager,
        "expected_user",
        "123",
        "Exact accepted reply",
        "https://x.com/source_user/status/123",
        evidence,
    )

    assert result == "https://x.com/expected_user/status/456"
    assert sleeps[:2] == [
        adapter.CONFIRMATION_REPLY_DISCOVERY_POLL_SECONDS,
        adapter.CONFIRMATION_REPLY_DISCOVERY_POLL_SECONDS,
    ]
    if kind == "reply":
        assert driver.disclosure_polls == 2
        assert evidence == {}
    else:
        assert driver.disclosure is not None
        assert driver.disclosure.clicks == 1
        assert evidence == {
            "proof_source": "source_thread_hidden_spam",
            "hidden_spam_disclosed": True,
        }


def test_reply_confirmation_does_not_click_an_unrecognized_disclosure_control(
    adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, driver = reply_confirmation_browser(
        adapter,
        reply_visible=False,
        disclosure_label="Show more replies",
    )
    sleeps = install_confirmation_clock(adapter, monkeypatch)
    result = adapter._confirmed_direct_reply_url(
        manager,
        "expected_user",
        "123",
        "Exact accepted reply",
        "https://x.com/source_user/status/123",
    )

    assert result is None
    assert driver.disclosure is not None
    assert driver.disclosure.clicks == 0
    assert manager.navigations == [
        "https://x.com/source_user/status/123",
    ]
    assert driver.page_load_timeouts == [
        adapter.CONFIRMATION_PAGE_LOAD_TIMEOUT_SECONDS,
        adapter.DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS,
    ]
    assert sum(sleeps) == pytest.approx(
        adapter.CONFIRMATION_REPLY_DISCOVERY_TIMEOUT_SECONDS
    )


def test_reply_disclosure_filter_rejects_generic_or_writing_controls(adapter) -> None:
    class Control:
        text = "Show more replies"

        def __init__(
            self, *, test_id: str | None = None, text: str = "Show more replies"
        ):
            self.test_id = test_id
            self.text = text

        def get_attribute(self, name: str):
            values = {
                "data-testid": self.test_id,
                "style": "",
                "aria-label": "Show",
                "aria-controls": "reply-region",
                "aria-expanded": "false",
                "innerText": self.text,
                "textContent": self.text,
            }
            return values.get(name)

    assert adapter._is_reply_disclosure_control(Control()) is False
    assert adapter._is_reply_disclosure_control(
        Control(text="Click Show probable spam to continue")
    ) is False
    assert adapter._is_reply_disclosure_control(
        Control(test_id="like", text="Show probable spam")
    ) is False


def test_reply_confirmation_rejects_identical_text_from_another_account(
    adapter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, _driver = reply_confirmation_browser(
        adapter,
        reply_visible=True,
        reply_href="/another_user/status/456",
    )
    install_confirmation_clock(adapter, monkeypatch)
    result = adapter._confirmed_direct_reply_url(
        manager,
        "expected_user",
        "123",
        "Exact accepted reply",
        "https://x.com/source_user/status/123",
    )

    assert result is None
    assert manager.navigations == [
        "https://x.com/source_user/status/123",
    ]
    assert manager.driver.page_load_timeouts == [
        adapter.CONFIRMATION_PAGE_LOAD_TIMEOUT_SECONDS,
        adapter.DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS,
    ]


def test_like_confirmation_uses_one_bounded_read_only_navigation(adapter) -> None:
    class Driver:
        def __init__(self):
            self.page_load_timeouts: list[float] = []

        def set_page_load_timeout(self, value: float):
            self.page_load_timeouts.append(value)

        def find_elements(self, _by: str, selector: str):
            assert selector == "//*[@data-testid='unlike']"
            return [object()]

    class Manager:
        def __init__(self):
            self.driver = Driver()
            self.navigations: list[str] = []

        def get_driver(self):
            pytest.fail("confirmation must not reattach a browser target")

        def navigate_to(self, url: str, ensure_driver: bool = True):
            assert ensure_driver is False
            self.navigations.append(url)
            return True

    manager = Manager()

    result = adapter._confirmed_like_url(
        manager, "https://x.com/source_user/status/123"
    )

    assert result == "https://x.com/source_user/status/123"
    assert manager.navigations == ["https://x.com/source_user/status/123"]
    assert manager.driver.page_load_timeouts == [
        adapter.CONFIRMATION_PAGE_LOAD_TIMEOUT_SECONDS,
        adapter.DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS,
    ]


def test_like_confirmation_waits_for_delayed_target_toolbar(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Driver:
        def __init__(self):
            self.page_load_timeouts: list[float] = []
            self.unlike_polls = 0

        def set_page_load_timeout(self, value: float):
            self.page_load_timeouts.append(value)

        def find_elements(self, _by: str, selector: str):
            assert selector == "//*[@data-testid='unlike']"
            self.unlike_polls += 1
            return [object()] if self.unlike_polls > 2 else []

    class Manager:
        def __init__(self):
            self.driver = Driver()
            self.navigations: list[str] = []

        def navigate_to(self, url: str, ensure_driver: bool = True):
            assert ensure_driver is False
            self.navigations.append(url)
            return True

    manager = Manager()
    sleeps = install_confirmation_clock(adapter, monkeypatch)

    result = adapter._confirmed_like_url(
        manager, "https://x.com/source_user/status/123"
    )

    assert result == "https://x.com/source_user/status/123"
    assert manager.navigations == ["https://x.com/source_user/status/123"]
    assert manager.driver.unlike_polls == 3
    assert sleeps == [
        adapter.CONFIRMATION_REPLY_DISCOVERY_POLL_SECONDS,
        adapter.CONFIRMATION_REPLY_DISCOVERY_POLL_SECONDS,
    ]


def test_dashboard_reply_confirmation_only_uses_the_read_only_proof_path(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[
        tuple[object, str, str, str | None, str | None, dict[str, object], tuple[str, ...]]
    ] = []

    class Pool:
        @asynccontextmanager
        async def confirmation_session(self, account: str):
            assert account == "expected_user"
            yield "verified-browser"

    def fake_proof(
        browser_manager,
        account_id: str,
        target_tweet_id: str,
        reply_text: str | None,
        target_tweet_url: str | None,
        confirmation_evidence: dict[str, object],
        reply_keywords: tuple[str, ...],
    ):
        calls.append(
            (
                browser_manager,
                account_id,
                target_tweet_id,
                reply_text,
                target_tweet_url,
                confirmation_evidence,
                reply_keywords,
            )
        )
        return "https://x.com/expected_user/status/456"

    async def unexpected_write(*_args, **_kwargs):
        pytest.fail("receipt confirmation must not send an X write")

    monkeypatch.setattr(adapter, "runtime_config_loader", lambda: object())
    monkeypatch.setattr(adapter, "session_pool", lambda _loader: Pool())
    monkeypatch.setattr(adapter, "_confirmed_direct_reply_url", fake_proof)
    monkeypatch.setattr(adapter.actions, "exec_reply", unexpected_write)
    monkeypatch.setattr(adapter.actions, "exec_like", unexpected_write, raising=False)

    result = asyncio.run(
        adapter.confirm_dashboard_action(
            action="reply",
            target_tweet_url="https://x.com/source_user/status/123",
            reply_text="Exact accepted reply",
        )
    )

    assert result == {
        "status": "confirmed",
        "permalink": "https://x.com/expected_user/status/456",
    }
    assert calls == [
        (
            "verified-browser",
            "expected_user",
            "123",
            "Exact accepted reply",
            "https://x.com/source_user/status/123",
            {},
            (),
        )
    ]


def test_dashboard_reply_confirmation_accepts_keyword_criteria(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str | None, tuple[str, ...]]] = []

    class Pool:
        @asynccontextmanager
        async def confirmation_session(self, account: str):
            assert account == "expected_user"
            yield "verified-browser"

    def fake_proof(
        _browser_manager,
        _account_id: str,
        _target_tweet_id: str,
        reply_text: str | None,
        _target_tweet_url: str | None,
        confirmation_evidence: dict[str, object],
        reply_keywords: tuple[str, ...],
    ):
        calls.append((reply_text, reply_keywords))
        confirmation_evidence["matched_text"] = (
            "PersProtect.com helps reduce exposed personal data."
        )
        return "https://x.com/expected_user/status/456"

    monkeypatch.setattr(adapter, "runtime_config_loader", lambda: object())
    monkeypatch.setattr(adapter, "session_pool", lambda _loader: Pool())
    monkeypatch.setattr(adapter, "_confirmed_direct_reply_url", fake_proof)

    result = asyncio.run(
        adapter.confirm_dashboard_action(
            action="reply",
            target_tweet_url="https://x.com/source_user/status/123",
            reply_keywords=["PersProtect.com", "persprotect"],
        )
    )

    assert result == {
        "status": "confirmed",
        "permalink": "https://x.com/expected_user/status/456",
        "matched_text": "PersProtect.com helps reduce exposed personal data.",
    }
    assert calls == [(None, ("persprotect.com", "persprotect"))]


def test_dashboard_reply_confirmation_reports_hidden_spam_proof(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager, driver = reply_confirmation_browser(
        adapter,
        reply_visible=False,
        disclosure_label="Show probable spam",
    )

    class Pool:
        @asynccontextmanager
        async def confirmation_session(self, account: str):
            assert account == "expected_user"
            yield manager

    async def unexpected_write(*_args, **_kwargs):
        pytest.fail("receipt confirmation must not send an X write")

    monkeypatch.setattr(adapter, "runtime_config_loader", lambda: object())
    monkeypatch.setattr(adapter, "session_pool", lambda _loader: Pool())
    monkeypatch.setattr(adapter.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(adapter.actions, "exec_reply", unexpected_write)
    monkeypatch.setattr(adapter.actions, "exec_like", unexpected_write, raising=False)

    result = asyncio.run(
        adapter.confirm_dashboard_action(
            action="reply",
            target_tweet_url="https://x.com/source_user/status/123",
            reply_text="Exact accepted reply",
        )
    )

    assert result == {
        "status": "confirmed",
        "permalink": "https://x.com/expected_user/status/456",
        "proof_source": "source_thread_hidden_spam",
        "hidden_spam_disclosed": True,
    }
    assert driver.disclosure is not None
    assert driver.disclosure.clicks == 1


def test_like_tweet_executes_one_canonical_verified_like(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str]] = []
    sessions: list[str] = []
    pacing: list[str] = []
    store = adapter.LockedBoundedDraftStore(
        adapter.X_USE_DATA_DIR / "drafts" / "drafts.jsonl"
    )
    monkeypatch.setattr(adapter, "draft_store", lambda: store)

    class FakePool:
        idle_timeout_seconds = 600

        def find_account_dict(self, account: str):
            return {"account_id": account, "is_active": True}

        @asynccontextmanager
        async def session(self, account: str):
            sessions.append(account)
            yield object()

        async def close_all(self):
            return None

    def fake_like_outcome(browser_manager, *, tweet_id: str, tweet_url: str) -> str:
        calls.append(("expected_user", tweet_id, tweet_url))
        return "clicked_confirmed"

    async def fake_pacing(ctx, account: str):
        pacing.append(account)

    monkeypatch.setattr(adapter, "session_pool", lambda loader: FakePool())
    monkeypatch.setattr(adapter, "_like_tweet_outcome", fake_like_outcome)
    monkeypatch.setattr(adapter, "reserve_persistent_action_pacing", fake_pacing)
    server = adapter.create_safe_server()

    first = call_tool(
        server,
        "like_tweet",
        {
            "account": "@Expected_User",
            "tweet_url": "https://x.com/i/web/status/123",
        },
    )
    second = call_tool(
        server,
        "like_tweet",
        {
            "tweet_url": "https://x.com/i/web/status/123",
        },
    )

    assert first == {
        "ok": True,
        "account": "expected_user",
        "action": "like_tweet",
        "tweet_id": "123",
        "tweet_url": "https://x.com/i/web/status/123",
        "success": True,
        "already_liked": False,
        "outcome": "clicked_confirmed",
        "receipt": {
            "action": "like",
            "status": "confirmed",
            "target_tweet_url": "https://x.com/i/web/status/123",
            "permalink": "https://x.com/i/web/status/123",
        },
    }
    assert second["ok"] is True
    assert second["already_liked"] is True
    assert second["outcome"] == "already_liked"
    assert second["receipt"]["status"] == "confirmed"
    assert calls == [
        ("expected_user", "123", "https://x.com/i/web/status/123")
    ]
    assert sessions == ["expected_user"]
    assert pacing == ["expected_user"]
    assert store.list() == []
    asyncio.run(adapter.shutdown_safe_server(server))


def test_like_tweet_rejects_noncanonical_targets_without_touching_browser(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakePool:
        idle_timeout_seconds = 600

        def find_account_dict(self, account: str):
            return {"account_id": account, "is_active": True}

        @asynccontextmanager
        async def session(self, account: str):
            raise AssertionError("invalid like target must not open a browser")
            yield object()

        async def close_all(self):
            return None

    monkeypatch.setattr(adapter, "session_pool", lambda loader: FakePool())
    server = adapter.create_safe_server()

    result = call_tool(
        server,
        "like_tweet",
        {"account": "expected_user", "tweet_url": "https://example.com/status/123"},
    )

    assert result["ok"] is False
    assert "https://x.com tweet URL" in result["error"]["message"]
    asyncio.run(adapter.shutdown_safe_server(server))


def test_like_tweet_click_without_dom_flip_is_accepted_for_read_only_confirmation(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakePool:
        idle_timeout_seconds = 600

        def find_account_dict(self, account: str):
            return {"account_id": account, "is_active": True}

        @asynccontextmanager
        async def session(self, account: str):
            yield object()

        async def close_all(self):
            return None

    async def fake_pacing(ctx, account: str):
        return None

    monkeypatch.setattr(adapter, "session_pool", lambda loader: FakePool())
    monkeypatch.setattr(
        adapter, "_like_tweet_outcome", lambda *_args, **_kwargs: "clicked_unconfirmed"
    )
    monkeypatch.setattr(adapter, "reserve_persistent_action_pacing", fake_pacing)
    server = adapter.create_safe_server()

    result = call_tool(
        server, "like_tweet", {"tweet_url": "https://x.com/i/web/status/123"}
    )

    assert result["ok"] is True
    assert result["outcome"] == "clicked_unconfirmed"
    assert result["receipt"] == {
        "action": "like",
        "status": "accepted",
        "target_tweet_url": "https://x.com/i/web/status/123",
        "diagnostic_code": "like_confirmation_pending",
    }
    asyncio.run(adapter.shutdown_safe_server(server))


def test_like_waits_for_exact_status_article_after_x_navigation(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[float, float | None]] = []

    class Browser:
        def navigate_to(self, _url: str) -> bool:
            return True

        def get_driver(self):
            return object()

    class Article:
        def find_elements(self, _by, _xpath):
            return [object()]

    class Wait:
        def __init__(self, _driver, timeout: float, poll_frequency: float | None = None):
            calls.append((timeout, poll_frequency))

        def until(self, predicate):
            value = predicate(object())
            assert value is article
            return value

    article = Article()
    monkeypatch.setattr(adapter, "WebDriverWait", Wait)
    monkeypatch.setattr(adapter, "find_article_with_status_id", lambda *_args: article)

    assert (
        adapter._like_tweet_outcome(
            Browser(), tweet_id="123", tweet_url="https://x.com/i/web/status/123"
        )
        == "already_liked"
    )
    assert calls == [(15, 0.25)]


def test_like_uses_dom_click_when_transient_overlay_intercepts_button(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Browser:
        def navigate_to(self, _url: str) -> bool:
            return True

        def get_driver(self):
            return driver

    class Article:
        def find_elements(self, _by, xpath):
            if "unlike" in xpath:
                return []
            return []

    class Button:
        def click(self):
            raise adapter.ElementClickInterceptedException("overlay")

    class Driver:
        def __init__(self):
            self.scripts: list[str] = []

        def execute_script(self, script, _button):
            self.scripts.append(script)

    class Wait:
        def __init__(self, _driver, _timeout, poll_frequency=None):
            self.target = _driver

        def until(self, predicate):
            if self.target is driver:
                return article
            if self.target is article:
                return button
            return predicate(self.target)

    article = Article()
    button = Button()
    driver = Driver()
    monkeypatch.setattr(adapter, "WebDriverWait", Wait)
    monkeypatch.setattr(adapter, "find_article_with_status_id", lambda *_args: article)

    assert (
        adapter._like_tweet_outcome(
            Browser(), tweet_id="123", tweet_url="https://x.com/i/web/status/123"
        )
        == "clicked_confirmed"
    )
    assert driver.scripts == [
        "arguments[0].scrollIntoView({block: 'center'});",
        "arguments[0].click();",
    ]


def test_mcp_draft_readers_revalidate_persistent_records_and_preview(adapter) -> None:
    server = adapter.create_safe_server()
    store = server.xuse_ctx.draft_store
    safe = store.create(
        "expected_user",
        "post_tweet",
        {"text": "safe text"},
        "auth_token=must-never-be-returned",
    )
    unsafe = store.create(
        "other_user",
        "post_tweet",
        {"text": "other account"},
        "other account",
    )

    listed = call_tool(server, "list_drafts", {})
    fetched = call_tool(server, "get_draft", {"draft_id": safe.draft_id})
    rejected = call_tool(server, "get_draft", {"draft_id": unsafe.draft_id})

    assert listed["ok"] is True
    assert [item["draft_id"] for item in listed["drafts"]] == [safe.draft_id]
    assert "auth_token" not in json.dumps(listed)
    assert fetched["draft"]["payload"] == {"text": "safe text"}
    assert fetched["draft"]["preview"] == 'Post as @expected_user: "safe text"'
    assert rejected["ok"] is False
    asyncio.run(adapter.shutdown_safe_server(server))


def test_mcp_metrics_reader_returns_only_bounded_typed_summary(adapter) -> None:
    metrics = adapter.X_USE_DATA_DIR / "metrics" / "data" / "metrics"
    metrics.mkdir(parents=True)
    (metrics / "expected_user.json").write_text(
        json.dumps(
            {
                "account_id": "attacker-controlled",
                "counters": {
                    "posts": 2,
                    "replies": -1,
                    "errors": "secret-value",
                    "unexpected": 999,
                },
                "last_run_started_at": "2026-08-22T10:00:00",
                "raw_event": "auth_token=must-not-leak",
            }
        )
    )
    server = adapter.create_safe_server()

    result = call_tool(server, "get_metrics", {"account": "expected_user"})

    assert result["ok"] is True
    assert result["summary"]["account_id"] == "expected_user"
    assert result["summary"]["counters"] == {
        "posts": 2,
        "replies": 0,
        "retweets": 0,
        "quote_tweets": 0,
        "likes": 0,
        "errors": 0,
    }
    assert "raw_event" not in json.dumps(result)
    assert "secret-value" not in json.dumps(result)
    asyncio.run(adapter.shutdown_safe_server(server))


def test_wrong_handle_fails_closed_before_browser_action(adapter, monkeypatch) -> None:
    class SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            self.driver.current = handle

    class FakeDriver:
        window_handles = ["operator-tab", "owned-tab"]

        def __init__(self):
            self.current = "operator-tab"
            self.switch_to = SwitchTo(self)

        def get(self, url):
            assert self.current == "owned-tab"

        def execute_script(self, script):
            assert script == adapter.SELENIUM_IDENTITY_SCRIPT
            assert script.startswith("return (")
            assert not script.startswith("return \n")
            return {
                "url": "https://x.com/home",
                "app_ready": True,
                "profile_href": "/different_user",
                "account_switcher_text": "",
            }

    manager = adapter.SafeAttachedBrowserManager.__new__(
        adapter.SafeAttachedBrowserManager
    )
    manager.driver = FakeDriver()
    manager._owned_handle = "owned-tab"
    manager.expected_handle = "expected_user"
    manager.logged_in_handle = None

    with pytest.raises(adapter.WrongAccountError, match="different_user"):
        manager.ensure_expected_handle(timeout=0)
    assert manager.driver.current == "owned-tab"


def test_confirmation_identity_proof_caps_home_navigation_before_polling(adapter) -> None:
    class SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            self.driver.current = handle

    class FakeDriver:
        window_handles = ["owned-tab"]

        def __init__(self):
            self.current = "owned-tab"
            self.switch_to = SwitchTo(self)
            self.page_load_timeouts: list[float] = []

        def set_page_load_timeout(self, value: float):
            self.page_load_timeouts.append(value)

        def get(self, url):
            assert url == adapter.X_HOME_URL
            assert self.current == "owned-tab"

        def execute_script(self, script):
            assert script == adapter.SELENIUM_IDENTITY_SCRIPT
            return {
                "url": "https://x.com/home",
                "app_ready": True,
                "profile_href": "/expected_user",
                "account_switcher_text": "",
            }

    manager = adapter.SafeAttachedBrowserManager.__new__(
        adapter.SafeAttachedBrowserManager
    )
    manager.driver = FakeDriver()
    manager._owned_handle = "owned-tab"
    manager.expected_handle = "expected_user"
    manager.logged_in_handle = None

    assert (
        manager.ensure_expected_handle(
            timeout=adapter.CONFIRMATION_IDENTITY_TIMEOUT_SECONDS,
            page_load_timeout=adapter.CONFIRMATION_IDENTITY_TIMEOUT_SECONDS,
        )
        == "expected_user"
    )
    assert 0 < manager.driver.page_load_timeouts[0] <= (
        adapter.CONFIRMATION_IDENTITY_TIMEOUT_SECONDS
    )
    assert manager.driver.page_load_timeouts[-1] == (
        adapter.DEFAULT_PAGE_LOAD_TIMEOUT_SECONDS
    )


def test_confirmation_session_uses_one_bounded_fresh_identity_check(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class Manager:
        def get_driver(
            self,
            *,
            identity_timeout: float,
            identity_page_load_timeout: float,
        ):
            calls.append(
                (
                    "identity",
                    identity_timeout,
                    identity_page_load_timeout,
                )
            )

        def close_driver(self):
            calls.append("close-target")

    manager = Manager()
    pool = adapter.VerifiedSessionPool.__new__(adapter.VerifiedSessionPool)
    pool._browser_factory = lambda account: calls.append(("factory", account)) or manager
    pool.config_loader = object()
    pool.find_account_dict = lambda account: {"account_id": account}

    async def unexpected_acquire(_account: str):
        pytest.fail("confirmation must not create a cached SessionPool entry")

    pool.acquire = unexpected_acquire
    monkeypatch.setattr(
        adapter,
        "acquire_browser_action_lock",
        lambda timeout_seconds: calls.append(("lock", timeout_seconds))
        or SimpleNamespace(),
    )
    monkeypatch.setattr(
        adapter,
        "release_browser_action_lock",
        lambda _handle: calls.append("release-lock"),
    )

    async def run() -> None:
        async with pool.confirmation_session("expected_user") as current:
            assert current is manager
            calls.append("read-only-proof")

    asyncio.run(run())

    assert calls == [
        ("lock", adapter.CONFIRMATION_PROCESS_LOCK_TIMEOUT_SECONDS),
        ("factory", {"account_id": "expected_user"}),
        (
            "identity",
            adapter.CONFIRMATION_IDENTITY_TIMEOUT_SECONDS,
            adapter.CONFIRMATION_IDENTITY_TIMEOUT_SECONDS,
        ),
        "read-only-proof",
        "close-target",
        "release-lock",
    ]


def test_verified_session_holds_the_cross_process_lock_through_the_action(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Manager:
        def get_driver(self):
            calls.append("get-driver")

        def ensure_expected_handle(self):
            calls.append("verify")

    class Entry:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.browser_manager = Manager()

        def touch(self):
            calls.append("touch")

    pool = adapter.VerifiedSessionPool.__new__(adapter.VerifiedSessionPool)

    async def acquire(account_id: str):
        assert account_id == "expected_user"
        return Entry()

    pool.acquire = acquire
    monkeypatch.setattr(
        adapter,
        "acquire_browser_action_lock",
        lambda: calls.append("acquire-process") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        adapter,
        "release_browser_action_lock",
        lambda handle: calls.append("release-process"),
    )

    async def run() -> None:
        async with pool.session("expected_user"):
            calls.append("browser-action")

    asyncio.run(run())

    assert calls == [
        "acquire-process",
        "get-driver",
        "verify",
        "browser-action",
        "touch",
        "release-process",
    ]


def test_verified_session_defers_cancellation_until_browser_worker_finishes(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    started = threading.Event()
    finish = threading.Event()

    class Manager:
        def get_driver(self):
            calls.append("get-driver")

        def ensure_expected_handle(self):
            calls.append("verify-start")
            started.set()
            assert finish.wait(2)
            calls.append("verify-finished")

    class Entry:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.browser_manager = Manager()

        def touch(self):
            calls.append("touch")

    pool = adapter.VerifiedSessionPool.__new__(adapter.VerifiedSessionPool)

    async def acquire(account_id: str):
        return Entry()

    pool.acquire = acquire
    monkeypatch.setattr(
        adapter,
        "acquire_browser_action_lock",
        lambda: calls.append("acquire-process") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        adapter,
        "release_browser_action_lock",
        lambda handle: calls.append("release-process"),
    )

    async def run() -> None:
        async def use_session() -> None:
            async with pool.session("expected_user"):
                pytest.fail("cancelled verification must not yield the browser")

        task = asyncio.create_task(use_session())
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        await asyncio.sleep(0.05)
        assert "release-process" not in calls
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert calls == [
        "acquire-process",
        "get-driver",
        "verify-start",
        "verify-finished",
        "touch",
        "release-process",
    ]


def test_warm_verified_session_reattaches_after_browser_restart(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    class Manager:
        active = True

        def get_driver(self):
            calls.append("get-driver")
            if not self.active:
                calls.append("reattach")
                self.active = True

        def ensure_expected_handle(self):
            assert self.active is True
            calls.append("verify")

    class Entry:
        def __init__(self):
            self.lock = asyncio.Lock()
            self.browser_manager = Manager()

        def touch(self):
            calls.append("touch")

    entry = Entry()
    pool = adapter.VerifiedSessionPool.__new__(adapter.VerifiedSessionPool)

    async def acquire(account_id: str):
        assert account_id == "expected_user"
        return entry

    pool.acquire = acquire
    monkeypatch.setattr(
        adapter, "acquire_browser_action_lock", lambda: SimpleNamespace()
    )
    monkeypatch.setattr(adapter, "release_browser_action_lock", lambda handle: None)

    async def run() -> None:
        async with pool.session("expected_user") as first:
            assert first is entry.browser_manager
        entry.browser_manager.active = False
        async with pool.session("expected_user") as second:
            assert second is entry.browser_manager

    asyncio.run(run())
    assert calls == [
        "get-driver",
        "verify",
        "touch",
        "get-driver",
        "reattach",
        "verify",
        "touch",
    ]


def test_crashed_owned_target_is_replaced_before_the_next_action(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    class SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            self.driver.current = handle
            calls.append(("switch", handle))

    class CrashedDriver:
        window_handles = ["owned-tab"]

        def __init__(self):
            self.current = "owned-tab"
            self.switch_to = SwitchTo(self)

        def execute_script(self, script):
            raise adapter.WebDriverException("tab crashed")

        def close(self):
            calls.append(("close", self.current))
            self.window_handles.remove(self.current)

    class FreshDriver:
        window_handles = ["fresh-tab"]

        def __init__(self):
            self.current = "fresh-tab"
            self.switch_to = SwitchTo(self)

        def set_page_load_timeout(self, value):
            calls.append(("page-timeout", value))

        def set_script_timeout(self, value):
            calls.append(("script-timeout", value))

        def execute_script(self, script):
            return "complete"

    class OldService:
        def stop(self):
            calls.append("old-service-stop")

    class NewService:
        pass

    manager = adapter.SafeAttachedBrowserManager.__new__(
        adapter.SafeAttachedBrowserManager
    )
    manager.driver = CrashedDriver()
    manager._service = OldService()
    manager._owned_handle = "owned-tab"
    manager.logged_in_handle = "expected_user"
    monkeypatch.setattr(adapter, "ChromeService", lambda **kwargs: NewService())
    monkeypatch.setattr(adapter.webdriver, "Chrome", lambda **kwargs: FreshDriver())
    monkeypatch.setattr(manager, "_create_owned_target", lambda: "fresh-tab")
    monkeypatch.setattr(manager, "ensure_expected_handle", lambda: calls.append("verify"))

    assert manager.get_driver().current == "fresh-tab"
    assert ("close", "owned-tab") in calls
    assert "old-service-stop" in calls
    assert "verify" in calls


def test_navigation_applies_low_data_blocklist_before_page_load(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "https://x.com/example/status/123"
    calls: list[object] = []

    class Driver:
        current_url = target

        def execute_cdp_cmd(self, method, params):
            calls.append((method, params))
            return {}

        def get(self, url):
            calls.append(("get", url))

    manager = adapter.SafeAttachedBrowserManager.__new__(
        adapter.SafeAttachedBrowserManager
    )
    driver = Driver()
    monkeypatch.setattr(manager, "get_driver", lambda: driver)
    monkeypatch.setattr(manager, "_switch_to_owned", lambda: calls.append("switch"))

    assert manager.navigate_to(target) is True

    assert calls[:4] == [
        "switch",
        ("Network.enable", {}),
        (
            "Network.setBlockedURLs",
            {"urls": list(adapter.MEDIA_BLOCKED_URL_PATTERNS)},
        ),
        ("get", target),
    ]


def test_navigation_accepts_a_committed_page_after_a_transient_webdriver_error(
    adapter, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = "https://x.com/example/status/123"
    calls: list[str] = []

    class Driver:
        current_url = target

        def get(self, url):
            calls.append(url)
            raise adapter.WebDriverException("transient navigation failure")

        def execute_script(self, script):
            assert script == "return document.readyState"
            return "complete"

    manager = adapter.SafeAttachedBrowserManager.__new__(
        adapter.SafeAttachedBrowserManager
    )
    driver = Driver()
    monkeypatch.setattr(manager, "get_driver", lambda: driver)
    monkeypatch.setattr(manager, "_switch_to_owned", lambda: None)

    assert manager.navigate_to(target) is True
    assert calls == [target]


def test_navigation_retries_once_before_any_x_click(adapter, monkeypatch: pytest.MonkeyPatch) -> None:
    target = "https://x.com/example/status/123"
    calls: list[str] = []

    class Driver:
        current_url = "chrome-error://chromewebdata/"

        def get(self, url):
            calls.append(url)
            if len(calls) == 1:
                raise adapter.WebDriverException("temporary navigation failure")
            self.current_url = url

        def execute_script(self, script):
            return "complete"

    manager = adapter.SafeAttachedBrowserManager.__new__(
        adapter.SafeAttachedBrowserManager
    )
    driver = Driver()
    monkeypatch.setattr(manager, "get_driver", lambda: driver)
    monkeypatch.setattr(manager, "_switch_to_owned", lambda: None)

    assert manager.navigate_to(target) is True
    assert calls == [target, target]


def test_teardown_closes_only_owned_tab_and_never_quits_browser(adapter) -> None:
    calls: list[object] = []

    class SwitchTo:
        def __init__(self, driver):
            self.driver = driver

        def window(self, handle):
            calls.append(("switch", handle))
            self.driver.current = handle

    class FakeDriver:
        def __init__(self):
            self.window_handles = ["operator-tab", "owned-tab"]
            self.current = "operator-tab"
            self.switch_to = SwitchTo(self)

        def close(self):
            calls.append(("close", self.current))
            self.window_handles.remove(self.current)

        def quit(self):
            pytest.fail("persistent Chromium must never be quit")

    class FakeService:
        def stop(self):
            calls.append("service-stop")

    manager = adapter.SafeAttachedBrowserManager.__new__(
        adapter.SafeAttachedBrowserManager
    )
    manager.driver = FakeDriver()
    manager._service = FakeService()
    manager._owned_handle = "owned-tab"
    manager.logged_in_handle = "expected_user"
    manager.close_driver()

    assert calls == [("switch", "owned-tab"), ("close", "owned-tab"), "service-stop"]


def test_draft_store_is_bounded_locked_and_permission_safe(adapter, tmp_path) -> None:
    path = tmp_path / "drafts" / "drafts.jsonl"
    store = adapter.LockedBoundedDraftStore(path)
    draft = store.create(
        "expected_user",
        "post_tweet",
        {"text": "hello", "media": [], "community": None},
        'Post as @expected_user: "hello"',
    )

    assert path.stat().st_mode & 0o777 == 0o600
    assert path.with_suffix(".lock").stat().st_mode & 0o777 == 0o600
    assert store.transition(
        draft.draft_id, expected="pending", target="rejected"
    ).status == "rejected"
    with pytest.raises(adapter.DraftConflictError):
        store.transition(draft.draft_id, expected="pending", target="approved")


def test_draft_store_prunes_old_terminal_records_and_remains_usable(
    adapter, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "MAX_DRAFTS", 5)
    monkeypatch.setattr(adapter, "MAX_RETAINED_TERMINAL_DRAFTS", 3)
    path = tmp_path / "drafts" / "drafts.jsonl"
    store = adapter.LockedBoundedDraftStore(path)
    for index in range(8):
        draft = store.create(
            "expected_user",
            "post_tweet",
            {"text": f"post {index}"},
            f"post {index}",
        )
        store.transition(draft.draft_id, expected="pending", target="rejected")

    reloaded = adapter.LockedBoundedDraftStore(path)
    assert len(reloaded.list()) == 3
    replacement = reloaded.create(
        "expected_user", "post_tweet", {"text": "still usable"}, "still usable"
    )
    assert replacement.status == "pending"


def test_persistent_action_pacing_survives_a_fresh_context(
    adapter, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(adapter, "X_USE_DATA_DIR", tmp_path / "x-use")
    loader = adapter.runtime_config_loader()

    class FakePool:
        def find_account_dict(self, account_id):
            return {"account_id": account_id, "is_active": True}

    def context():
        return adapter.Ctx(
            config_loader=loader,
            session_pool=FakePool(),
            draft_store=SimpleNamespace(),
            draft_mode=True,
        )

    current = [1000.0]
    waits: list[float] = []

    def now():
        return current[0]

    async def fake_sleep(seconds: float):
        waits.append(seconds)
        current[0] += seconds

    asyncio.run(
        adapter.reserve_persistent_action_pacing(
            context(), "expected_user", now=now, sleep=fake_sleep
        )
    )
    assert waits == []
    current[0] = 1010.0
    asyncio.run(
        adapter.reserve_persistent_action_pacing(
            context(), "expected_user", now=now, sleep=fake_sleep
        )
    )

    assert waits == [50.0]
    state = tmp_path / "x-use" / "metrics" / "action-pacing.json"
    assert state.stat().st_mode & 0o777 == 0o600
    assert json.loads(state.read_text()) == {
        "account": "expected_user",
        "last_action_at": 1060.0,
    }


def test_historical_approved_draft_recovers_failed_not_pending(
    adapter, tmp_path
) -> None:
    path = tmp_path / "drafts" / "drafts.jsonl"
    first = adapter.LockedBoundedDraftStore(path)
    draft = first.create(
        "expected_user", "post_tweet", {"text": "hello"}, "hello"
    )
    first.transition(draft.draft_id, expected="pending", target="approved")

    recovered = adapter.LockedBoundedDraftStore(path)

    assert recovered.get(draft.draft_id).status == "failed"
    assert json.loads(path.read_text().splitlines()[-1])["status"] == "failed"


def test_draft_reload_recovers_only_final_approved_once(adapter, tmp_path) -> None:
    path = tmp_path / "drafts" / "drafts.jsonl"
    store = adapter.LockedBoundedDraftStore(path)
    completed = store.create(
        "expected_user", "post_tweet", {"text": "completed"}, "completed"
    )
    store.transition(completed.draft_id, expected="pending", target="approved")
    store.set_status(completed.draft_id, "executed")
    interrupted = store.create(
        "expected_user", "post_tweet", {"text": "interrupted"}, "interrupted"
    )
    store.transition(interrupted.draft_id, expected="pending", target="approved")

    reloaded = adapter.LockedBoundedDraftStore(path)
    size_after_recovery = path.stat().st_size
    assert reloaded.get(completed.draft_id).status == "executed"
    assert reloaded.get(interrupted.draft_id).status == "failed"
    for _ in range(5):
        assert reloaded.get(interrupted.draft_id).status == "failed"
        reloaded.list()

    assert path.stat().st_size == size_after_recovery


def test_crash_claim_is_visible_and_cannot_be_retried(
    adapter, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts" / "drafts.jsonl"
    store = adapter.LockedBoundedDraftStore(path)
    draft = store.create(
        "expected_user", "post_tweet", {"text": "hello"}, "hello"
    )
    monkeypatch.setattr(adapter, "draft_store", lambda: store)

    class FakePool:
        @asynccontextmanager
        async def session(self, account: str):
            assert account == "expected_user"
            yield object()

        async def close_all(self):
            return None

    monkeypatch.setattr(adapter, "session_pool", lambda loader: FakePool())

    class SimulatedProcessDeath(BaseException):
        pass

    calls = 0

    async def die_after_claim(ctx, claimed):
        nonlocal calls
        calls += 1
        assert claimed.status == "failed"
        raise SimulatedProcessDeath

    monkeypatch.setattr(adapter.actions, "execute_draft", die_after_claim)
    async def no_pacing(*args, **kwargs):
        return None
    monkeypatch.setattr(adapter, "reserve_persistent_action_pacing", no_pacing)

    with pytest.raises(SimulatedProcessDeath):
        asyncio.run(adapter.approve_dashboard_draft(draft.draft_id))

    assert adapter.LockedBoundedDraftStore(path).get(draft.draft_id).status == "failed"
    with pytest.raises(adapter.DraftConflictError):
        asyncio.run(adapter.approve_dashboard_draft(draft.draft_id))
    assert calls == 1


def test_upstream_dedup_recovery_returns_executed_without_republishing(
    adapter, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "drafts" / "drafts.jsonl"
    store = adapter.LockedBoundedDraftStore(path)
    draft = store.create(
        "expected_user", "post_tweet", {"text": "hello"}, "hello"
    )
    monkeypatch.setattr(adapter, "draft_store", lambda: store)

    class FakePool:
        @asynccontextmanager
        async def session(self, account: str):
            yield object()

        async def close_all(self):
            return None

    monkeypatch.setattr(adapter, "session_pool", lambda loader: FakePool())
    calls = 0

    async def already_processed(ctx, claimed):
        nonlocal calls
        calls += 1
        raise adapter.ToolError("An identical post was already executed (dedup).")

    monkeypatch.setattr(adapter.actions, "execute_draft", already_processed)
    async def no_pacing(*args, **kwargs):
        return None
    monkeypatch.setattr(adapter, "reserve_persistent_action_pacing", no_pacing)

    result = asyncio.run(adapter.approve_dashboard_draft(draft.draft_id))

    assert result == {
        "draft_id": draft.draft_id,
        "status": "executed",
        "result": {
            "account": "expected_user",
            "action": "post_tweet",
            "success": True,
        },
    }
    assert adapter.LockedBoundedDraftStore(path).get(draft.draft_id).status == "executed"
    assert calls == 1


def test_mcp_network_environment_overwrites_inherited_proxy_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("hermes_x_use_mcp_test", MCP_ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "assigned_proxy")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PORT", "8899")
    monkeypatch.setenv("RESIDENTIAL_PROXY_URL", "http://127.0.0.1:8899")
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.setenv(key, "http://upstream-user:secret@example.com")

    module.configure_network_environment()

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:8899"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:8899"
    assert "ALL_PROXY" not in os.environ
    assert os.environ["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert all(
        "upstream-user" not in os.environ.get(key, "")
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    )


def test_mcp_entrypoint_refuses_direct_network_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "hermes_x_use_mcp_direct_test", MCP_ENTRYPOINT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("HERMES_BROWSER_NETWORK_MODE", "direct")
    monkeypatch.delenv("RESIDENTIAL_PROXY_URL", raising=False)

    with pytest.raises(RuntimeError, match="assigned proxy"):
        module.configure_network_environment()
