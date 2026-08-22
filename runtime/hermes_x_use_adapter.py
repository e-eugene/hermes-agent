"""Hardened x-use 2.4.1 adapter for the Hermes runtime.

The upstream package supplies the X scraping and publishing engine. This
adapter narrows it to a fixed MCP allowlist, forces draft-only writes, attaches
Selenium to a dedicated tab in the existing persistent Chromium instance, and
keeps dashboard approval completely outside the MCP surface.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from selenium import webdriver
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService

from xuse import __version__ as upstream_version
from xuse.core.config_loader import ConfigLoader
from xuse.features.scraper import TweetScraper
from xuse.mcp import actions
from xuse.mcp.annotations import (
    LOCAL_WRITE_IDEMPOTENT,
    PUBLISHES_TO_X,
    READ_ONLY_FROM_X,
    READ_ONLY_LOCAL,
)
from xuse.mcp.drafts import Draft, DraftStatus, DraftStore
from xuse.mcp.executor import Ctx, ToolError
from xuse.mcp.media import images_for_tweet, media_envelope, with_images
from xuse.mcp.server import create_server as create_upstream_server
from xuse.mcp.server import shutdown as shutdown_upstream_server
from xuse.mcp.sessions import SessionEntry, SessionPool
from xuse.mcp.tools import guard, ok_

from hermes_x_use_common import (
    IDENTITY_EXPRESSION,
    MAX_X_TEXT_CHARS,
    X_HOME_URL,
    X_USE_ACCOUNTS_PATH,
    X_USE_DATA_DIR,
    X_USE_SETTINGS_PATH,
    X_USE_VERSION,
    acquire_browser_action_lock,
    canonical_x_status_url,
    configure_runtime,
    handle_from_snapshot,
    has_credential_like_content,
    live_status,
    load_expected_handle,
    normalize_handle,
    require_assigned_proxy_bridge,
    release_browser_action_lock,
)


if upstream_version != X_USE_VERSION:
    raise RuntimeError(
        f"Unsupported x-use version {upstream_version}; expected {X_USE_VERSION}"
    )


MCP_ALLOWED_TOOLS = frozenset(
    {
        "list_accounts",
        "get_account",
        "get_account_health",
        "get_metrics",
        "search_tweets",
        "search_profile",
        "get_tweet",
        "prepare_reply",
        "post_tweet",
        "reply_to_tweet",
        "list_drafts",
        "get_draft",
        "reject_draft",
    }
)
MCP_FORBIDDEN_TOOLS = frozenset(
    {
        "approve_draft",
        "run_cycle",
        "process_queue",
        "queue_post",
        "queue_engagement",
        "add_account",
        "update_account",
        "remove_account",
        "set_account_active",
        "add_proxy_pool",
        "update_proxy_pool",
        "remove_proxy_pool",
        "engage",
    }
)
MAX_DRAFT_RECORD_BYTES = 64 * 1024
MAX_DRAFT_FILE_BYTES = 8 * 1024 * 1024
MAX_DRAFTS = 500
MAX_PENDING_DRAFTS = 100
MAX_RETAINED_TERMINAL_DRAFTS = 300
MAX_SEARCH_QUERY_CHARS = 256
MAX_SINGLE_TWEET_CANDIDATES = 20
MAX_METRICS_FILE_BYTES = 256 * 1024
CDP_DEBUGGER_ADDRESS = "127.0.0.1:9222"
CHROMEDRIVER_PATH = "/usr/bin/chromedriver"


class WrongAccountError(RuntimeError):
    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"X account mismatch: expected @{expected}, authenticated as @{actual}"
        )


class SessionNotVerifiedError(RuntimeError):
    pass


class DraftConflictError(RuntimeError):
    pass


async def definitive_to_thread(function, /, *args):
    """Finish a running worker before propagating task cancellation.

    ``asyncio.to_thread`` cannot stop Selenium when its waiter is cancelled.
    Returning the deferred cancellation separately lets callers keep the
    process-wide browser lock until the worker is definitively finished.
    """

    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    cancellation: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(worker)
            return result, cancellation
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
        except BaseException:
            if cancellation is not None:
                raise cancellation from None
            raise


def raise_deferred_cancellation(
    cancellation: asyncio.CancelledError | None,
) -> None:
    if cancellation is not None:
        raise cancellation


def runtime_config_loader() -> ConfigLoader:
    if not X_USE_SETTINGS_PATH.is_file() or not X_USE_ACCOUNTS_PATH.is_file():
        configure_runtime()
    return ConfigLoader(
        settings_file=X_USE_SETTINGS_PATH,
        accounts_file=X_USE_ACCOUNTS_PATH,
    )


class SafeAttachedBrowserManager:
    """BrowserManager-compatible adapter for one owned CDP target.

    It never starts Chromium, never passes proxy settings, and intentionally
    never calls WebDriver.quit(). On teardown it closes only the target it
    created and stops its local chromedriver transport.
    """

    def __init__(self, account_config: dict[str, Any], config_loader: ConfigLoader):
        require_assigned_proxy_bridge()
        self.account_config = dict(account_config)
        self.config_loader = config_loader
        self.expected_handle = normalize_handle(self.account_config.get("account_id"))
        assigned = load_expected_handle()
        if self.expected_handle != assigned:
            raise WrongAccountError(assigned, self.expected_handle)
        self.driver: Any | None = None
        self._service: ChromeService | None = None
        self._owned_handle: str | None = None
        self.logged_in_handle: str | None = None
        # Attributes consumed by x-use media helpers. The persistent browser
        # already has the local proxy bridge; no proxy is passed to Selenium.
        self.effective_proxy = None
        self.cookies_data = None

    def _switch_to_owned(self) -> None:
        if self.driver is None or self._owned_handle is None:
            raise RuntimeError("x-use browser target is unavailable")
        handles = set(self.driver.window_handles)
        if self._owned_handle not in handles:
            raise RuntimeError("x-use browser target was closed")
        self.driver.switch_to.window(self._owned_handle)

    def _create_owned_target(self) -> str:
        assert self.driver is not None
        before = set(self.driver.window_handles)
        target = self.driver.execute_cdp_cmd(
            "Target.createTarget", {"url": "about:blank", "background": True}
        )
        target_id = str(target.get("targetId") or "")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            handles = set(self.driver.window_handles)
            exact = [handle for handle in handles if target_id and target_id in handle]
            if exact:
                return exact[0]
            created = handles - before
            if len(created) == 1:
                return created.pop()
            time.sleep(0.05)
        raise RuntimeError("Chromedriver did not expose the dedicated x-use target")

    def get_driver(self):
        if self.driver is not None and self.is_driver_active():
            self._switch_to_owned()
            return self.driver
        if self.driver is not None:
            self.close_driver()

        options = ChromeOptions()
        options.debugger_address = CDP_DEBUGGER_ADDRESS
        service = ChromeService(executable_path=CHROMEDRIVER_PATH)
        driver = webdriver.Chrome(service=service, options=options)
        self.driver = driver
        self._service = service
        try:
            driver.set_page_load_timeout(45)
            driver.set_script_timeout(30)
            self._owned_handle = self._create_owned_target()
            self._switch_to_owned()
            self.ensure_expected_handle()
            return driver
        except Exception:
            self.close_driver()
            raise

    def is_driver_active(self) -> bool:
        if self.driver is None or self._owned_handle is None:
            return False
        try:
            return self._owned_handle in set(self.driver.window_handles)
        except Exception:
            return False

    def navigate_to(self, url: str, ensure_driver: bool = True) -> bool:
        try:
            driver = self.get_driver() if ensure_driver else self.driver
            if driver is None:
                return False
            self._switch_to_owned()
            driver.get(url)
            return True
        except Exception:
            return False

    def ensure_expected_handle(self, timeout: float = 35) -> str:
        """Navigate only the owned target and fail closed on any mismatch."""

        if self.driver is None:
            raise SessionNotVerifiedError("Persistent Chromium is unavailable")
        self._switch_to_owned()
        self.driver.get(X_HOME_URL)
        deadline = time.monotonic() + max(0, timeout)
        actual: str | None = None
        while True:
            try:
                snapshot = self.driver.execute_script(
                    "return " + IDENTITY_EXPRESSION
                )
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise SessionNotVerifiedError(
                        "X session identity could not be read"
                    ) from exc
                time.sleep(0.25)
                continue
            actual = handle_from_snapshot(snapshot)
            if actual:
                break
            if time.monotonic() >= deadline:
                raise SessionNotVerifiedError("X session identity could not be verified")
            time.sleep(0.25)
        if actual != self.expected_handle:
            raise WrongAccountError(self.expected_handle, actual)
        self.logged_in_handle = actual
        return actual

    def close_driver(self) -> None:
        driver = self.driver
        service = self._service
        owned_handle = self._owned_handle
        self.driver = None
        self._service = None
        self._owned_handle = None
        self.logged_in_handle = None
        if driver is not None and owned_handle is not None:
            try:
                if owned_handle in set(driver.window_handles):
                    driver.switch_to.window(owned_handle)
                    driver.close()
            except Exception:
                pass
        if service is not None:
            try:
                service.stop()
            except Exception:
                pass


class VerifiedSessionPool(SessionPool):
    """SessionPool that re-verifies the assigned handle before every action."""

    async def _cold_start(self, account_id: str):
        process_lock, lock_cancellation = await definitive_to_thread(
            acquire_browser_action_lock
        )
        if lock_cancellation is not None:
            await definitive_to_thread(release_browser_action_lock, process_lock)
            raise lock_cancellation
        account_dict = self.find_account_dict(account_id)

        def start_manager():
            manager = (
                self._browser_factory(account_dict)
                if self._browser_factory is not None
                else SafeAttachedBrowserManager(account_dict, self.config_loader)
            )
            manager.get_driver()
            return manager

        try:
            manager, cancellation = await definitive_to_thread(start_manager)
            if cancellation is not None:
                _, _ = await definitive_to_thread(manager.close_driver)
                raise cancellation
            return SessionEntry(browser_manager=manager)
        finally:
            await definitive_to_thread(release_browser_action_lock, process_lock)

    @asynccontextmanager
    async def session(self, account_id: str):
        entry = await self.acquire(account_id)
        async with entry.lock:
            process_lock, lock_cancellation = await definitive_to_thread(
                acquire_browser_action_lock
            )
            if lock_cancellation is not None:
                await definitive_to_thread(
                    release_browser_action_lock, process_lock
                )
                raise lock_cancellation

            def verify_manager() -> None:
                # A supervised Chromium restart invalidates the cached
                # chromedriver transport and its owned target. get_driver()
                # detects that stale state, closes only our transport, and
                # reattaches before the per-action identity proof below.
                entry.browser_manager.get_driver()
                entry.browser_manager.ensure_expected_handle()

            try:
                _, cancellation = await definitive_to_thread(
                    verify_manager
                )
                raise_deferred_cancellation(cancellation)
                yield entry.browser_manager
            finally:
                entry.touch()
                await definitive_to_thread(
                    release_browser_action_lock, process_lock
                )


class LockedBoundedDraftStore(DraftStore):
    """Cross-process locked, permission-safe and bounded JSONL draft store."""

    def __init__(self, persistence_path: Path):
        self._thread_lock = threading.RLock()
        self._lock_depth = 0
        self._lock_file_handle: Any | None = None
        persistence_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(persistence_path.parent, 0o700)
        self._lock_path = persistence_path.with_suffix(".lock")
        self._lock_path.touch(mode=0o600, exist_ok=True)
        os.chmod(self._lock_path, 0o600)
        # Avoid upstream DraftStore._load(), which rewrites an interrupted
        # `approved` claim back to `pending` and permits a duplicate retry.
        # Our durable claim falls back to terminal `failed`, and any historical
        # approved record is recovered to that same fail-closed state.
        super().__init__(None)
        self._path = persistence_path
        with self.exclusive():
            self._prune_terminal_locked()
            if len(self._drafts) > MAX_DRAFTS:
                raise RuntimeError("Draft count limit reached")
        if persistence_path.exists():
            os.chmod(persistence_path, 0o600)

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._thread_lock:
            outermost = self._lock_depth == 0
            if outermost:
                self._lock_file_handle = self._lock_path.open("a+")
                fcntl.flock(self._lock_file_handle.fileno(), fcntl.LOCK_EX)
                self._reload_locked()
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
                if outermost:
                    assert self._lock_file_handle is not None
                    fcntl.flock(self._lock_file_handle.fileno(), fcntl.LOCK_UN)
                    self._lock_file_handle.close()
                    self._lock_file_handle = None

    def _reload_locked(self) -> None:
        self._drafts.clear()
        assert self._path is not None
        if not self._path.exists():
            return
        if self._path.stat().st_size > MAX_DRAFT_FILE_BYTES:
            raise RuntimeError("Draft store limit reached")
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > MAX_DRAFT_RECORD_BYTES:
                continue
            try:
                draft = Draft.model_validate_json(line)
            except Exception:
                continue
            self._drafts[draft.draft_id] = draft
        recovered = False
        for draft in self._drafts.values():
            # Fold the complete JSONL history first. Only a draft whose FINAL
            # record is approved represents an interrupted legacy execution.
            if draft.status == "approved":
                draft.status = "failed"
                recovered = True
        if recovered:
            self._compact_locked()

    def _compact_locked(self) -> None:
        assert self._path is not None
        temporary = self._path.with_name(self._path.name + ".tmp")
        content = "".join(
            draft.model_dump_json() + "\n" for draft in self._drafts.values()
        )
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_DRAFT_FILE_BYTES:
            raise RuntimeError("Draft store limit reached")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, self._path)
        directory_descriptor = os.open(self._path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    def _prune_terminal_locked(self, *, reserve: int = 0) -> None:
        terminal = sorted(
            (
                draft
                for draft in self._drafts.values()
                if draft.status != "pending"
            ),
            key=lambda draft: draft.created_at,
            reverse=True,
        )
        keep_terminal = min(
            MAX_RETAINED_TERMINAL_DRAFTS,
            max(0, MAX_DRAFTS - reserve),
        )
        keep_ids = {draft.draft_id for draft in terminal[:keep_terminal]}
        if len(self._drafts) + reserve <= MAX_DRAFTS and len(terminal) <= keep_terminal:
            return
        self._drafts = {
            draft_id: draft
            for draft_id, draft in self._drafts.items()
            if draft.status == "pending" or draft_id in keep_ids
        }
        self._compact_locked()

    def _append(self, draft: Draft) -> None:
        assert self._path is not None
        line = draft.model_dump_json() + "\n"
        if len(line.encode("utf-8")) > MAX_DRAFT_RECORD_BYTES:
            raise ValueError("Draft is too large")
        current_size = self._path.stat().st_size if self._path.exists() else 0
        if current_size + len(line.encode("utf-8")) > MAX_DRAFT_FILE_BYTES:
            self._compact_locked()
            return
        descriptor = os.open(
            self._path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        try:
            os.write(descriptor, line.encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(self._path, 0o600)

    def create(
        self,
        account: str,
        action: str,
        payload: dict[str, Any],
        preview: str,
    ) -> Draft:
        encoded_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded_payload) > MAX_DRAFT_RECORD_BYTES or len(preview) > 4096:
            raise ValueError("Draft is too large")
        with self.exclusive():
            self._prune_terminal_locked(reserve=1)
            if len(self._drafts) >= MAX_DRAFTS:
                raise ValueError("Draft count limit reached")
            if sum(d.status == "pending" for d in self._drafts.values()) >= MAX_PENDING_DRAFTS:
                raise ValueError("Pending draft limit reached")
            draft = Draft(
                draft_id=uuid4().hex,
                account=account,
                action=action,
                payload=payload,
                preview=preview,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            self._drafts[draft.draft_id] = draft
            self._append(draft)
            return draft.model_copy(deep=True)

    def get(self, draft_id: str) -> Draft:
        with self.exclusive():
            try:
                return self._drafts[draft_id].model_copy(deep=True)
            except KeyError:
                raise KeyError(f"Unknown draft_id '{draft_id}'.") from None

    def list(self, status: DraftStatus | None = None) -> list[Draft]:
        with self.exclusive():
            drafts = list(self._drafts.values())
            if status is not None:
                drafts = [draft for draft in drafts if draft.status == status]
            return [draft.model_copy(deep=True) for draft in drafts]

    def set_status(self, draft_id: str, status: DraftStatus) -> Draft:
        with self.exclusive():
            try:
                draft = self._drafts[draft_id]
            except KeyError:
                raise KeyError(f"Unknown draft_id '{draft_id}'.") from None
            draft.status = status
            self._append(draft)
            return draft.model_copy(deep=True)

    def transition(
        self,
        draft_id: str,
        *,
        expected: DraftStatus,
        target: DraftStatus,
    ) -> Draft:
        with self.exclusive():
            try:
                draft = self._drafts[draft_id]
            except KeyError:
                raise KeyError(f"Unknown draft_id '{draft_id}'.") from None
            if draft.status != expected:
                raise DraftConflictError(
                    f"Draft '{draft_id}' is {draft.status}, expected {expected}"
                )
            draft.status = target
            self._append(draft)
            return draft.model_copy(deep=True)

    def claim_for_execution(self, draft_id: str) -> Draft:
        """Durably make a pending draft non-retriable before touching X.

        `failed` is the safe fallback state. On success it advances to
        `executed`; on an exception or process death it stays visible as
        failed/possibly-unknown and can never be approved a second time.
        """

        return self.transition(draft_id, expected="pending", target="failed")


def draft_store() -> LockedBoundedDraftStore:
    return LockedBoundedDraftStore(X_USE_DATA_DIR / "drafts" / "drafts.jsonl")


def session_pool(loader: ConfigLoader) -> VerifiedSessionPool:
    return VerifiedSessionPool(
        loader,
        idle_timeout_seconds=600,
        cold_start_timeout_seconds=90,
        reap_interval_seconds=60,
        browser_factory=lambda account: SafeAttachedBrowserManager(account, loader),
    )


def validate_staged_text(text: object) -> str:
    if not isinstance(text, str):
        raise ToolError("Text must be a string.")
    if has_credential_like_content(text):
        raise ToolError("Text contains credential-like or unsafe content.")
    normalized = text.strip()
    if not normalized:
        raise ToolError("Text must not be empty.")
    if len(normalized) > MAX_X_TEXT_CHARS:
        raise ToolError(
            f"Text exceeds the {MAX_X_TEXT_CHARS}-character safe composer limit."
        )
    return normalized


def validate_search_query(query: object) -> str:
    """Keep search input bounded and prevent secrets/control data reaching X."""

    if not isinstance(query, str) or has_credential_like_content(query):
        raise ToolError("Search query contains credential-like or unsafe content.")
    normalized = query.strip()
    if not normalized:
        raise ToolError("Search query must not be empty.")
    if len(normalized) > MAX_SEARCH_QUERY_CHARS:
        raise ToolError(
            f"Search query exceeds the {MAX_SEARCH_QUERY_CHARS}-character limit."
        )
    return normalized


def canonical_draft_record(draft: Draft, expected_handle: str) -> dict[str, Any]:
    """Return only a validated execution payload, never persisted preview text."""

    validate_draft_for_approval(draft, expected_handle)
    if draft.status not in {"pending", "executed", "failed", "rejected"}:
        raise DraftConflictError("Draft status is not public")
    text = str(draft.payload["text"])
    if draft.action == "post_tweet":
        preview = f'Post as @{expected_handle}: "{text}"'
    else:
        preview = (
            f'Reply as @{expected_handle} to {draft.payload["tweet_url"]}: '
            f'"{text}"'
        )
    return {
        "draft_id": draft.draft_id,
        "account": expected_handle,
        "action": draft.action,
        "payload": dict(draft.payload),
        "preview": preview,
        "created_at": str(draft.created_at)[:128],
        "status": draft.status,
    }


def safe_metrics_summary(account: str) -> dict[str, Any]:
    expected = load_expected_handle()
    try:
        requested = normalize_handle(account)
    except Exception:
        raise ToolError("Requested X account is invalid.") from None
    if requested != expected:
        raise ToolError("Requested X account is not assigned to this runtime.")
    source: dict[str, Any] = {}
    path = X_USE_DATA_DIR / "metrics" / "data" / "metrics" / f"{expected}.json"
    if path.exists():
        if path.stat().st_size > MAX_METRICS_FILE_BYTES:
            raise ToolError("Metrics summary is unavailable.")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ToolError("Metrics summary is unavailable.") from None
        if isinstance(loaded, dict):
            source = loaded
    raw_counters = source.get("counters")
    counters: dict[str, int] = {}
    for name in ("posts", "replies", "retweets", "quote_tweets", "likes", "errors"):
        value = raw_counters.get(name, 0) if isinstance(raw_counters, dict) else 0
        counters[name] = (
            min(value, 2**31 - 1)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
            else 0
        )

    def bounded_timestamp(name: str) -> str | None:
        value = source.get(name)
        return value[:64] if isinstance(value, str) else None

    return ok_(
        account=expected,
        summary={
            "account_id": expected,
            "counters": counters,
            "last_run_started_at": bounded_timestamp("last_run_started_at"),
            "last_run_finished_at": bounded_timestamp("last_run_finished_at"),
        },
    )


async def reserve_persistent_action_pacing(
    ctx: Ctx,
    account: str,
    *,
    now=time.time,
    sleep=asyncio.sleep,
) -> None:
    """Reserve restart-safe action pacing while the browser lock is held."""

    from xuse.mcp import executor as ex

    account_id, _, model = ex.resolve_account(ctx, account)
    delay = max(
        0,
        int(
            ex.current_action_config(ctx, model).min_delay_between_actions_seconds
        ),
    )
    path = X_USE_DATA_DIR / "metrics" / "action-pacing.json"
    current = float(now())
    last: float | None = None
    if path.exists():
        # A corrupt state never creates a no-delay bypass: recover by waiting
        # one complete interval before replacing it.
        last = current
    if path.exists() and path.stat().st_size <= 4096:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            candidate = loaded.get("last_action_at") if isinstance(loaded, dict) else None
            if (
                isinstance(loaded, dict)
                and loaded.get("account") == account_id
                and isinstance(candidate, (int, float))
                and not isinstance(candidate, bool)
                and math.isfinite(float(candidate))
            ):
                last = min(current, float(candidate))
        except (OSError, ValueError, OverflowError):
            pass
    wait_seconds = (
        0.0
        if last is None
        else min(float(delay), max(0.0, delay - (current - last)))
    )
    if wait_seconds:
        await sleep(wait_seconds)
    reserved_at = float(now())
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    encoded = json.dumps(
        {"account": account_id, "last_action_at": reserved_at},
        separators=(",", ":"),
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        0o600,
    )
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _draft_envelope(draft: Draft) -> dict[str, Any]:
    return ok_(
        draft_id=draft.draft_id,
        account=draft.account,
        action=draft.action,
        payload=draft.payload,
        preview=draft.preview,
        status=draft.status,
        message="Draft created; nothing was posted. Approve it in the Hermes dashboard.",
    )


def _register_safe_stage_tools(server: Any, ctx: Ctx) -> None:
    @server.tool(name="get_metrics", annotations=READ_ONLY_LOCAL)
    @guard
    async def safe_get_metrics(account: str) -> dict[str, Any]:
        """Return only bounded typed counters; persistent event logs stay private."""

        return safe_metrics_summary(account)

    @server.tool(name="list_drafts", annotations=READ_ONLY_LOCAL)
    @guard
    async def safe_list_drafts(
        status: str | None = None,
        account: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List only canonical drafts for the single assigned X account."""

        allowed_statuses = {None, "pending", "executed", "failed", "rejected"}
        if status not in allowed_statuses:
            raise ToolError("Draft status is invalid.")
        expected = load_expected_handle()
        if account is not None:
            try:
                requested = normalize_handle(account)
            except Exception:
                raise ToolError("Requested X account is invalid.") from None
            if requested != expected:
                raise ToolError("Requested X account is not assigned to this runtime.")
        records: list[dict[str, Any]] = []
        for draft in ctx.draft_store.list(status=status):
            try:
                records.append(canonical_draft_record(draft, expected))
            except (DraftConflictError, WrongAccountError):
                continue
        records.sort(key=lambda item: str(item["created_at"]), reverse=True)
        exact_limit = max(1, min(int(limit), 100))
        return ok_(
            total=len(records),
            returned=min(len(records), exact_limit),
            drafts=records[:exact_limit],
        )

    @server.tool(name="get_draft", annotations=READ_ONLY_LOCAL)
    @guard
    async def safe_get_draft(draft_id: str) -> dict[str, Any]:
        """Return one canonical assigned-account draft without raw preview data."""

        try:
            draft = ctx.draft_store.get(draft_id)
        except KeyError:
            raise ToolError(f"Unknown draft_id '{draft_id}'.") from None
        try:
            public = canonical_draft_record(draft, load_expected_handle())
        except (DraftConflictError, WrongAccountError):
            raise ToolError("Draft is invalid or unavailable.") from None
        return ok_(draft=public)

    @server.tool(name="get_account_health", annotations=READ_ONLY_FROM_X)
    @guard
    async def safe_get_account_health(account: str) -> dict[str, Any]:
        """Return the live persistent-CDP X session state for one account.

        Unlike upstream x-use health, this adapter intentionally has no cookie
        file. The response therefore comes from the browser's live cookie jar
        and a strict identity probe, and never returns cookie names or values.
        """

        expected = load_expected_handle()
        try:
            requested = normalize_handle(account)
        except Exception:
            raise ToolError("Requested X account is invalid.") from None
        if requested != expected:
            raise ToolError("Requested X account is not assigned to this runtime.")
        snapshot = await asyncio.to_thread(live_status)
        status = str(snapshot.get("status") or "error")
        if status not in {"ready", "not_configured", "wrong_account", "error"}:
            status = "error"
        result: dict[str, Any] = {
            "account": expected,
            "status": status,
            "configured": bool(snapshot.get("configured")),
            "session_present": bool(snapshot.get("session_present")),
            "account_verified": bool(snapshot.get("account_verified")),
            "expected_handle": expected,
        }
        actual = snapshot.get("authenticated_handle")
        if isinstance(actual, str):
            try:
                result["authenticated_handle"] = normalize_handle(actual)
            except Exception:
                pass
        fixed_errors = {
            "not_configured": "X session is missing, expired, or could not be verified.",
            "wrong_account": "Persistent Chromium is authenticated as another X account.",
            "error": "Persistent Chromium X session check failed.",
        }
        if status in fixed_errors:
            result["error"] = fixed_errors[status]
        return ok_(**result)

    @server.tool(name="search_tweets", annotations=READ_ONLY_FROM_X)
    @guard
    async def safe_search_tweets(
        keywords: str,
        limit: int = 10,
        account: str | None = None,
        include_images: bool = False,
    ) -> dict[str, Any]:
        """Search X through the assigned verified account with bounded input."""

        from xuse.mcp import executor as ex
        from xuse.mcp.tools import attach_search_images, dump_tweet

        account_id, _, _ = ex.resolve_account(ctx, account)
        exact_query = validate_search_query(keywords)
        exact_limit = max(1, min(int(limit), 50))
        async with ctx.session_pool.session(account_id) as browser_manager:
            scraper, cancellation = await definitive_to_thread(
                TweetScraper, browser_manager, account_id
            )
            raise_deferred_cancellation(cancellation)
            tweets, cancellation = await definitive_to_thread(
                scraper.scrape_tweets_by_keyword, exact_query, exact_limit
            )
            raise_deferred_cancellation(cancellation)
        envelope = ok_(
            account=account_id,
            query=exact_query,
            count=len(tweets),
            tweets=[dump_tweet(tweet) for tweet in tweets],
        )
        if not include_images:
            return envelope
        return await attach_search_images(envelope, tweets)

    @server.tool(name="search_profile", annotations=READ_ONLY_FROM_X)
    @guard
    async def safe_search_profile(
        profile: str,
        limit: int = 10,
        account: str | None = None,
        include_images: bool = False,
    ) -> dict[str, Any]:
        """Read one canonical X profile through the verified browser target."""

        from xuse.mcp import executor as ex
        from xuse.mcp.tools import attach_search_images, dump_tweet

        if not isinstance(profile, str) or has_credential_like_content(profile):
            raise ToolError("Profile target is invalid.")
        account_id, _, _ = ex.resolve_account(ctx, account)
        handle = ex.profile_handle_from(profile)
        profile_url = f"https://x.com/{handle.lower()}"
        exact_limit = max(1, min(int(limit), 50))
        async with ctx.session_pool.session(account_id) as browser_manager:
            scraper, cancellation = await definitive_to_thread(
                TweetScraper, browser_manager, account_id
            )
            raise_deferred_cancellation(cancellation)
            tweets, cancellation = await definitive_to_thread(
                scraper.scrape_tweets_from_profile, profile_url, exact_limit
            )
            raise_deferred_cancellation(cancellation)
        envelope = ok_(
            account=account_id,
            profile=f"@{handle.lower()}",
            profile_url=profile_url,
            count=len(tweets),
            tweets=[dump_tweet(tweet) for tweet in tweets],
        )
        if not include_images:
            return envelope
        return await attach_search_images(envelope, tweets)

    async def safe_single_tweet(
        account: str,
        tweet_url: str,
    ) -> tuple[str, Any, str, str, Any]:
        """Resolve an account and fetch only a canonical public X status URL."""

        from xuse.mcp import executor as ex
        account_id, _, model = ex.resolve_account(ctx, account)
        try:
            canonical_url, tweet_id = canonical_x_status_url(tweet_url)
        except ValueError:
            raise ToolError("Tweet target must be an https://x.com tweet URL.")
        async with ctx.session_pool.session(account_id) as browser_manager:
            scraper, cancellation = await definitive_to_thread(
                TweetScraper, browser_manager, account_id
            )
            raise_deferred_cancellation(cancellation)
            tweets, cancellation = await definitive_to_thread(
                scraper.scrape_tweets_from_url,
                canonical_url,
                "tweet",
                MAX_SINGLE_TWEET_CANDIDATES,
            )
            raise_deferred_cancellation(cancellation)
        original = next(
            (
                tweet
                for tweet in tweets
                if tweet.tweet_id == tweet_id and tweet.text_content
            ),
            None,
        )
        if original is None:
            raise ToolError(f"Could not load tweet {tweet_id}.")
        return account_id, model, canonical_url, tweet_id, original

    @server.tool(name="get_tweet", annotations=READ_ONLY_FROM_X)
    @guard
    async def safe_get_tweet(
        account: str,
        tweet_url: str,
        include_images: bool = True,
    ) -> dict[str, Any]:
        """Fetch one tweet after replacing caller input with its canonical X URL."""

        account_id, model, canonical_url, tweet_id, original = (
            await safe_single_tweet(account, tweet_url)
        )
        handle = (original.user_handle or "").lstrip("@")
        envelope = ok_(
            account=account_id,
            tweet_id=tweet_id,
            tweet_url=canonical_url,
            author=f"@{handle}" if handle else None,
            text_content=original.text_content or "",
            like_count=original.like_count or 0,
            retweet_count=original.retweet_count or 0,
            reply_count=original.reply_count or 0,
            view_count=original.view_count or 0,
            media=media_envelope(original),
            persona=getattr(model, "persona", None),
        )
        if not include_images:
            return envelope
        images = await asyncio.to_thread(images_for_tweet, original)
        return with_images(envelope, images)

    @server.tool(name="prepare_reply", annotations=READ_ONLY_FROM_X)
    @guard
    async def safe_prepare_reply(
        account: str,
        tweet_url: str,
        include_images: bool = True,
    ) -> dict[str, Any]:
        """Prepare reply context for one canonical X status URL only."""

        from xuse.mcp import executor as ex

        account_id, model, canonical_url, tweet_id, original = (
            await safe_single_tweet(account, tweet_url)
        )
        handle = (original.user_handle or "").lstrip("@")
        envelope = ok_(
            account=account_id,
            tweet_id=tweet_id,
            tweet_url=canonical_url,
            author=f"@{handle}" if handle else None,
            text_content=original.text_content or "",
            media=media_envelope(original),
            persona=getattr(model, "persona", None),
            account_keywords=list(
                getattr(model, "target_keywords", None) or []
            ),
            max_reply_chars=ex.MAX_REPLY_CHARS,
            message=(
                "Write the reply with your own model, then call "
                "reply_to_tweet with the reviewed text."
            ),
        )
        if not include_images:
            return envelope
        images = await asyncio.to_thread(images_for_tweet, original)
        return with_images(envelope, images)

    @server.tool(name="post_tweet", annotations=PUBLISHES_TO_X)
    @guard
    async def safe_post_tweet(account: str, text: str) -> dict[str, Any]:
        """Stage a text-only X post for dashboard review. Nothing is posted."""

        from xuse.mcp import executor as ex

        account_id, _, _ = ex.resolve_account(ctx, account)
        exact_text = validate_staged_text(text)
        draft = ctx.draft_store.create(
            account=account_id,
            action="post_tweet",
            payload={"text": exact_text},
            preview=f'Post as @{account_id}: "{exact_text}"',
        )
        return _draft_envelope(draft)

    @server.tool(name="reply_to_tweet", annotations=PUBLISHES_TO_X)
    @guard
    async def safe_reply_to_tweet(
        account: str,
        tweet_url: str,
        text: str,
    ) -> dict[str, Any]:
        """Stage an explicit X reply for dashboard review. Nothing is posted."""

        from xuse.mcp import executor as ex

        account_id, _, _ = ex.resolve_account(ctx, account)
        try:
            canonical_url, tweet_id = canonical_x_status_url(tweet_url)
        except ValueError:
            raise ToolError("Reply target must be an https://x.com tweet URL.")
        exact_text = validate_staged_text(text)
        draft = ctx.draft_store.create(
            account=account_id,
            action="reply_to_tweet",
            payload={
                "tweet_url": canonical_url,
                "tweet_id": tweet_id,
                "text": exact_text,
            },
            preview=f'Reply as @{account_id} to {canonical_url}: "{exact_text}"',
        )
        return _draft_envelope(draft)

    @server.tool(name="reject_draft", annotations=LOCAL_WRITE_IDEMPOTENT)
    @guard
    async def safe_reject_draft(draft_id: str) -> dict[str, Any]:
        """Reject one pending local draft. This never touches X."""

        try:
            draft = ctx.draft_store.transition(
                draft_id, expected="pending", target="rejected"
            )
        except KeyError:
            raise ToolError(f"Unknown draft_id '{draft_id}'.") from None
        except DraftConflictError as exc:
            raise ToolError(str(exc)) from None
        return ok_(draft_id=draft.draft_id, status=draft.status)


def create_safe_server():
    loader = runtime_config_loader()
    pool = session_pool(loader)
    store = draft_store()
    server = create_upstream_server(
        config_loader=loader,
        draft_mode=True,
        session_pool=pool,
        draft_store=store,
    )
    tools = server._tool_manager  # FastMCP 1.x public manager has remove_tool().
    upstream_names = {item.name for item in tools.list_tools()}
    if not MCP_ALLOWED_TOOLS.issubset(upstream_names):
        missing = sorted(MCP_ALLOWED_TOOLS - upstream_names)
        raise RuntimeError(f"x-use MCP contract changed; missing tools: {missing}")
    for name in list(upstream_names):
        if name not in MCP_ALLOWED_TOOLS or name in {
            "get_metrics",
            "list_drafts",
            "get_draft",
            "get_account_health",
            "search_tweets",
            "search_profile",
            "get_tweet",
            "prepare_reply",
            "post_tweet",
            "reply_to_tweet",
            "reject_draft",
        }:
            tools.remove_tool(name)
    _register_safe_stage_tools(server, server.xuse_ctx)
    final_names = {item.name for item in tools.list_tools()}
    if final_names != MCP_ALLOWED_TOOLS or final_names & MCP_FORBIDDEN_TOOLS:
        raise RuntimeError("x-use MCP allowlist enforcement failed")
    # Nothing outside the curated tool surface is useful to Hermes. Clear the
    # upstream prompts/resources, which describe and reference forbidden gates.
    server._prompt_manager._prompts.clear()
    server._resource_manager._resources.clear()
    server._resource_manager._templates.clear()
    try:
        server._mcp_server.instructions = (
            "Read X through the assigned verified account. post_tweet and "
            "reply_to_tweet create dashboard-reviewed drafts only; this MCP "
            "has no publishing or queue-execution gate."
        )
    except Exception:
        pass
    return server


def validate_draft_for_approval(draft: Draft, expected_handle: str) -> None:
    if draft.account != expected_handle:
        raise WrongAccountError(expected_handle, draft.account)
    if draft.action not in {"post_tweet", "reply_to_tweet"}:
        raise DraftConflictError("Draft action is not dashboard-approvable")
    try:
        exact_text = validate_staged_text(draft.payload.get("text"))
    except ToolError as exc:
        raise DraftConflictError("Draft text is unsafe") from exc
    if exact_text != draft.payload.get("text"):
        raise DraftConflictError("Draft text does not match its execution payload")
    if draft.action == "post_tweet":
        if set(draft.payload) != {"text"}:
            raise DraftConflictError("Post draft payload is not canonical")
    else:
        if set(draft.payload) != {"text", "tweet_url", "tweet_id"}:
            raise DraftConflictError("Reply draft payload is not canonical")
        try:
            canonical_url, tweet_id = canonical_x_status_url(
                draft.payload.get("tweet_url")
            )
        except ValueError:
            raise DraftConflictError("Reply draft target is invalid")
        if (
            canonical_url != draft.payload.get("tweet_url")
            or tweet_id != draft.payload.get("tweet_id")
        ):
            raise DraftConflictError("Reply draft target is not canonical")


def canonical_execution_result(draft: Draft, result: object) -> dict[str, object]:
    """Reduce an upstream result to the dashboard's exact public contract."""

    source = result if isinstance(result, dict) else {}
    if source.get("success") is not True:
        raise RuntimeError("x-use returned an invalid execution result")
    account = normalize_handle(source.get("account"))
    if account != draft.account:
        raise WrongAccountError(draft.account, account)
    public: dict[str, object] = {
        "account": account,
        "action": draft.action,
        "success": True,
    }
    if draft.action == "reply_to_tweet":
        tweet_id = str(source.get("tweet_id") or "")
        if tweet_id != draft.payload.get("tweet_id"):
            raise RuntimeError("x-use returned an invalid reply result")
        public["tweet_id"] = tweet_id
    return public


def list_dashboard_drafts(status: str | None = "pending", limit: int = 100) -> dict[str, Any]:
    if status not in {None, "pending", "executed", "failed", "rejected"}:
        raise ValueError("Unsupported draft status")
    drafts = draft_store().list(status=status)  # type: ignore[arg-type]
    drafts.sort(key=lambda item: item.created_at, reverse=True)
    bounded_limit = max(1, min(int(limit), 100))
    selected = drafts[:bounded_limit]
    return {
        "drafts": [json.loads(item.model_dump_json()) for item in selected],
        "count": len(selected),
    }


def reject_dashboard_draft(draft_id: str) -> dict[str, object]:
    draft = draft_store().transition(
        draft_id, expected="pending", target="rejected"
    )
    return {"draft_id": draft.draft_id, "status": draft.status}


async def approve_dashboard_draft(draft_id: str) -> dict[str, object]:
    """Execute exactly one pending draft after two strict identity checks."""

    expected = load_expected_handle()
    loader = runtime_config_loader()
    pool = session_pool(loader)
    store = draft_store()
    ctx = Ctx(
        config_loader=loader,
        session_pool=pool,
        draft_store=store,
        draft_mode=True,
    )
    try:
        try:
            current = store.get(draft_id)
        except KeyError:
            raise KeyError(f"Unknown draft_id '{draft_id}'.") from None
        if current.status != "pending":
            raise DraftConflictError(
                f"Draft '{draft_id}' is {current.status}, expected pending"
            )
        validate_draft_for_approval(current, expected)
        # Verify identity and atomically reserve restart-safe pacing while the
        # cross-process browser lock is held. The durable draft transition
        # below is separately atomic, so the JSONL flock need not block draft
        # listing for the full external publish.
        async with pool.session(expected):
            latest = store.get(draft_id)
            if latest.status != "pending":
                raise DraftConflictError(
                    f"Draft '{draft_id}' is {latest.status}, expected pending"
                )
            validate_draft_for_approval(latest, expected)
            await reserve_persistent_action_pacing(ctx, expected)
            claimed = store.claim_for_execution(draft_id)
        try:
            result = await actions.execute_draft(ctx, claimed)
        except Exception as exc:
            message = str(exc)
            if isinstance(exc, ToolError) and (
                "dedup" in message or "Already" in message
            ):
                store.set_status(draft_id, "executed")
                recovered: dict[str, object] = {
                    "account": claimed.account,
                    "action": claimed.action,
                    "success": True,
                }
                if claimed.action == "reply_to_tweet":
                    recovered["tweet_id"] = str(
                        claimed.payload.get("tweet_id") or ""
                    )
                return {
                    "draft_id": draft_id,
                    "status": "executed",
                    "result": recovered,
                }
            raise
        public_result = canonical_execution_result(claimed, result)
        store.set_status(draft_id, "executed")
        return {
            "draft_id": draft_id,
            "status": "executed",
            "result": public_result,
        }
    finally:
        await pool.close_all()


async def shutdown_safe_server(server: Any) -> None:
    await shutdown_upstream_server(server)
