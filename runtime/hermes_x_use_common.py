"""Secret-safe X session import and live identity checks for Hermes x-use.

This module deliberately has no x-use or Selenium dependency.  Session exports
are accepted only in memory, applied to the already-running persistent Chromium
profile over loopback CDP, and then discarded.  Identity probes always create
and close their own background target, so operator and Hermes browser tabs are
never navigated or replaced.
"""

from __future__ import annotations

import json
import fcntl
import math
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from websockets.sync.client import connect


X_USE_VERSION = "2.4.1"
MAX_SESSION_BYTES = 512 * 1024
MAX_COOKIES = 512
MAX_COOKIE_NAME_BYTES = 256
MAX_COOKIE_VALUE_BYTES = 16 * 1024
X_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
ALLOWED_COOKIE_HOSTS = ("x.com", "twitter.com")
REQUIRED_SESSION_COOKIES = frozenset({"auth_token", "ct0"})
X_HOME_URL = "https://x.com/home"
MAX_X_TEXT_CHARS = 270
LOW_DATA_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
MEDIA_BLOCKED_URL_PATTERNS = (
    "*://pbs.twimg.com/*",
    "*://video.twimg.com/*",
    "*://upload.twitter.com/*",
    "*.mp4*",
    "*.m3u8*",
    "*.ts*",
)
_CREDENTIAL_PATTERNS = (
    re.compile(r"(?i)\bauth_token\b"),
    re.compile(r"(?i)(?:^|\W)ct0(?:$|\W)"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(
        r"(?i)\b(?:api[ _-]?(?:key|token|secret)|cookie|set-cookie|"
        r"proxy[ _-]?(?:password|pass|token|secret|username|url))\b\s*(?:=|:)\s*\S+"
    ),
    re.compile(
        r"(?i)\b(?:(?:sk|pk)-[A-Za-z0-9_=-]{12,}|"
        r"ghp_[A-Za-z0-9_=-]{12,}|github_pat_[A-Za-z0-9_=-]{12,})"
    ),
)
_URL_CANDIDATE_RE = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
_X_STATUS_PATH_RE = re.compile(
    r"^/(?:i(?:/web)?|(?P<handle>[A-Za-z0-9_]{1,15}))/status/"
    r"(?P<tweet_id>[1-9][0-9]{0,24})/?$"
)

SOCIAL_ACCOUNTS_PATH = Path(
    os.environ.get(
        "HERMES_SOCIAL_ACCOUNTS_PATH",
        "/tmp/hermes-secrets/social-accounts.json",
    )
)
X_USE_CONFIG_DIR = Path(
    os.environ.get("HERMES_X_USE_CONFIG_DIR", "/tmp/hermes-x-use/config")
)
X_USE_DATA_DIR = Path(
    os.environ.get("HERMES_X_USE_DATA_DIR", "/opt/data/x-use")
)
X_USE_SETTINGS_PATH = X_USE_CONFIG_DIR / "settings.json"
X_USE_ACCOUNTS_PATH = X_USE_CONFIG_DIR / "accounts.json"
CDP_VERSION_URL = os.environ.get(
    "BROWSER_CDP_VERSION_URL", "http://127.0.0.1:9222/json/version"
)
BROWSER_ACTION_LOCK_PATH = Path(
    os.environ.get(
        "HERMES_X_USE_ACTION_LOCK_PATH",
        "/tmp/hermes-x-use/browser-action.lock",
    )
)

# This expression returns only public session metadata. It never evaluates or
# serializes document.cookie, localStorage, request headers, or page HTML.
IDENTITY_EXPRESSION = r"""
(() => {
  const profile = document.querySelector('[data-testid="AppTabBar_Profile_Link"]');
  const text = (document.body?.innerText || '').toLowerCase();
  let challenge = null;
  if (text.includes('captcha')) challenge = 'challenge';
  else if (text.includes('authentication code') || text.includes('verification code')) challenge = 'challenge';
  else if (text.includes('unusual activity') || text.includes('verify your identity') ||
           text.includes('enter your phone') || text.includes('enter your email')) challenge = 'challenge';
  return {
    url: String(location.href || ''),
    app_ready: (location.hostname === 'x.com' || location.hostname.endsWith('.x.com')) &&
      document.readyState !== 'loading' && Boolean(document.querySelector('#react-root')),
    profile_href: profile?.getAttribute('href') || '',
    challenge,
  };
})()
"""


class SessionImportError(ValueError):
    """The uploaded cookie export is malformed or insufficient."""


class RuntimeConfigurationError(RuntimeError):
    """The runtime does not have exactly one valid active X assignment."""


class BrowserActionBusyError(RuntimeError):
    """Another process owns the dedicated X browser action slot."""


def require_assigned_proxy_bridge() -> str:
    """Return the only network route allowed for x-use, or fail closed."""

    if os.environ.get("HERMES_BROWSER_NETWORK_MODE") != "assigned_proxy":
        raise RuntimeConfigurationError("x-use requires the assigned proxy route")
    port = os.environ.get("HERMES_RESIDENTIAL_PROXY_PORT", "8899")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise RuntimeConfigurationError("x-use loopback proxy configuration is invalid")
    expected = f"http://127.0.0.1:{port}"
    if os.environ.get("RESIDENTIAL_PROXY_URL") != expected:
        raise RuntimeConfigurationError("x-use loopback proxy bridge is unavailable")
    return expected


def low_data_mode() -> bool:
    value = os.environ.get("HERMES_X_LOW_DATA_MODE", "true").strip().lower()
    return value not in LOW_DATA_FALSE_VALUES


def apply_cdp_low_data_controls(client: Any, session_id: str) -> None:
    """Block heavy media in one CDP target before navigating to X."""

    if not low_data_mode():
        return
    client.request("Network.enable", session_id=session_id, timeout=5)
    client.request(
        "Network.setBlockedURLs",
        {"urls": list(MEDIA_BLOCKED_URL_PATTERNS)},
        session_id=session_id,
        timeout=5,
    )


def acquire_browser_action_lock(timeout_seconds: float = 5.0):
    """Acquire the bounded cross-process MCP/dashboard browser action lock."""

    BROWSER_ACTION_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(BROWSER_ACTION_LOCK_PATH.parent, 0o700)
    handle = BROWSER_ACTION_LOCK_PATH.open("a+")
    os.chmod(BROWSER_ACTION_LOCK_PATH, 0o600)
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    try:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise BrowserActionBusyError("X browser action is busy")
                time.sleep(0.05)
    except Exception:
        handle.close()
        raise
    return handle


def release_browser_action_lock(handle: Any) -> None:
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def normalize_handle(value: object) -> str:
    candidate = str(value or "").strip().lstrip("@")
    if not X_HANDLE_RE.fullmatch(candidate):
        raise RuntimeConfigurationError("Assigned X handle is invalid")
    return candidate.lower()


def has_credential_like_content(value: object) -> bool:
    """Return true for content that could publish credentials or headers."""

    if not isinstance(value, str):
        return True
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return True
    if any(pattern.search(value) for pattern in _CREDENTIAL_PATTERNS):
        return True
    for match in _URL_CANDIDATE_RE.finditer(value):
        try:
            parsed = urlparse(match.group(0))
        except ValueError:
            return True
        if parsed.username is not None or parsed.password is not None:
            return True
    return False


def canonical_x_status_url(value: object) -> tuple[str, str]:
    """Validate and canonicalize one public X status URL and numeric id."""

    if not isinstance(value, str) or not value:
        raise ValueError("X status URL is required")
    try:
        parsed = urlparse(value)
    except ValueError as exc:
        raise ValueError("X status URL is invalid") from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").lower()
        not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("X status URL is invalid")
    match = _X_STATUS_PATH_RE.fullmatch(parsed.path)
    if match is None:
        raise ValueError("X status URL is invalid")
    tweet_id = match.group("tweet_id")
    handle = match.group("handle")
    if handle is None:
        return f"https://x.com/i/web/status/{tweet_id}", tweet_id
    return f"https://x.com/{normalize_handle(handle)}/status/{tweet_id}", tweet_id


def load_expected_handle(path: Path | None = None) -> str:
    source_path = path or SOCIAL_ACCOUNTS_PATH
    try:
        raw = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeConfigurationError(
            "Assigned X account configuration is unavailable"
        ) from exc
    if not isinstance(raw, list):
        raise RuntimeConfigurationError("Assigned X account configuration is invalid")
    matches = [
        item
        for item in raw
        if isinstance(item, dict)
        and item.get("type") == "x"
        and bool(item.get("is_active"))
    ]
    if len(matches) != 1:
        raise RuntimeConfigurationError(
            "Exactly one active X account must be assigned"
        )
    return normalize_handle(matches[0].get("login"))


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def configure_runtime() -> dict[str, object]:
    """Generate non-secret, ephemeral x-use config for one assigned account."""

    require_assigned_proxy_bridge()
    expected = load_expected_handle()
    drafts_dir = X_USE_DATA_DIR / "drafts"
    metrics_dir = X_USE_DATA_DIR / "metrics"
    media_dir = X_USE_DATA_DIR / "media"
    for directory in (
        X_USE_DATA_DIR,
        drafts_dir,
        metrics_dir,
        metrics_dir / "data",
        metrics_dir / "logs",
        media_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    settings: dict[str, object] = {
        "browser_settings": {
            "type": "chrome",
            "headless": False,
            "chrome_driver_path": "/usr/bin/chromedriver",
            "use_undetected_chromedriver": False,
            "enable_stealth": False,
            "cookie_domain_url": "https://x.com",
            "page_load_timeout_seconds": 45,
            "script_timeout_seconds": 30,
            "login_wait_seconds": 0,
            "webdriver_manager_cache_path": "/tmp/hermes-x-use/wdm-cache",
        },
        "mcp": {
            "draft_mode": True,
            "session_idle_timeout_seconds": 600,
            "cold_start_timeout_seconds": 90,
            "drafts_file": str(drafts_dir / "drafts.jsonl"),
        },
        "queue": {
            "store_file": "/tmp/hermes-x-use/disabled-queue.jsonl",
            "auto_drain": {"enabled": False},
        },
        "twitter_automation": {
            "media_directory": str(media_dir),
            "processed_tweets_file": str(metrics_dir / "processed-actions.csv"),
            "action_config": {
                "min_delay_between_actions_seconds": 60,
                "max_delay_between_actions_seconds": 180,
            },
        },
        "logging": {
            "level": "INFO",
            "console": True,
            "file_handler": {"enabled": False},
        },
    }
    accounts = [
        {
            "account_id": expected,
            "is_active": True,
            "target_keywords": [],
            "competitor_profiles": [],
            "persona": "",
        }
    ]
    _atomic_json(X_USE_SETTINGS_PATH, settings)
    _atomic_json(X_USE_ACCOUNTS_PATH, accounts)
    return {
        "configured": True,
        "expected_handle": expected,
        "version": X_USE_VERSION,
    }


def cookie_domain_allowed(value: object) -> bool:
    domain = str(value or "").strip().lower().lstrip(".")
    return any(domain == root or domain.endswith("." + root) for root in ALLOWED_COOKIE_HOSTS)


def _same_site(value: object) -> str | None:
    candidate = str(value or "").strip().lower().replace("-", "_")
    if candidate == "strict":
        return "Strict"
    if candidate == "lax":
        return "Lax"
    if candidate in {"none", "no_restriction"}:
        return "None"
    return None


def _cookie_list(document: object) -> list[object]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict) and isinstance(document.get("cookies"), list):
        return document["cookies"]
    raise SessionImportError("Cookie export must be a JSON array or a cookies array")


def validate_session_export(raw: bytes) -> list[dict[str, object]]:
    """Validate and normalize an uploaded browser-cookie export.

    Only X/Twitter-scoped cookies are retained. The result is intended to be
    passed directly to CDP and must never be serialized to disk or logs.
    """

    if not raw or len(raw) > MAX_SESSION_BYTES:
        raise SessionImportError("Cookie export must be between 1 byte and 512 KiB")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise SessionImportError("Cookie export is not valid UTF-8 JSON") from exc
    entries = _cookie_list(document)
    if not entries or len(entries) > MAX_COOKIES:
        raise SessionImportError("Cookie export has an invalid number of cookies")

    normalized: list[dict[str, object]] = []
    required: set[str] = set()
    for raw_cookie in entries:
        if not isinstance(raw_cookie, dict):
            raise SessionImportError("Every cookie entry must be an object")
        name = raw_cookie.get("name")
        value = raw_cookie.get("value")
        domain = raw_cookie.get("domain") or raw_cookie.get("host")
        if not isinstance(name, str) or not name:
            raise SessionImportError("Every cookie must have a non-empty name")
        if not isinstance(value, str):
            raise SessionImportError("Every cookie must have a string value")
        if len(name.encode("utf-8")) > MAX_COOKIE_NAME_BYTES:
            raise SessionImportError("Cookie name is too long")
        if len(value.encode("utf-8")) > MAX_COOKIE_VALUE_BYTES:
            raise SessionImportError("Cookie value is too long")
        if not cookie_domain_allowed(domain):
            # A whole-browser export may include unrelated sites. Never import
            # those cookies into the runtime, and never report their names.
            continue
        if name in REQUIRED_SESSION_COOKIES and not value:
            raise SessionImportError("Required X session cookies must be non-empty")
        if name in REQUIRED_SESSION_COOKIES and name in required:
            raise SessionImportError(
                "Cookie export must contain exactly one auth_token and ct0"
            )

        cookie: dict[str, object] = {
            "name": name,
            "value": value,
            "domain": str(domain).strip().lower(),
            "path": str(raw_cookie.get("path") or "/"),
            "secure": bool(raw_cookie.get("secure", True)),
            "httpOnly": bool(
                raw_cookie.get("httpOnly", raw_cookie.get("http_only", False))
            ),
        }
        same_site = _same_site(raw_cookie.get("sameSite", raw_cookie.get("same_site")))
        if same_site:
            cookie["sameSite"] = same_site
        expires = raw_cookie.get("expires", raw_cookie.get("expirationDate"))
        valid_expiry = (
            isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and math.isfinite(float(expires))
        )
        if valid_expiry and expires > 10_000_000_000:
            # Tolerate exporters that use milliseconds rather than CDP's
            # seconds, while still requiring a finite persistent expiration.
            expires = float(expires) / 1000
        if valid_expiry and expires > time.time() + 60:
            cookie["expires"] = float(expires)
        elif name in REQUIRED_SESSION_COOKIES:
            raise SessionImportError(
                "Required X session cookies must have a future expiration"
            )
        else:
            # Do not import already-expired optional cookies.
            continue
        normalized.append(cookie)
        if name in REQUIRED_SESSION_COOKIES:
            required.add(name)

    if required != REQUIRED_SESSION_COOKIES:
        raise SessionImportError("Cookie export must contain auth_token and ct0 for X")
    return normalized


def is_x_url(value: object) -> bool:
    hostname = (urlparse(str(value or "")).hostname or "").lower()
    return any(hostname == root or hostname.endswith("." + root) for root in ALLOWED_COOKIE_HOSTS)


def handle_from_snapshot(snapshot: object) -> str | None:
    if not isinstance(snapshot, dict):
        return None
    raw_href = snapshot.get("profile_href")
    if not isinstance(raw_href, str) or not raw_href:
        return None
    href = raw_href.strip()
    parsed = urlparse(href)
    if parsed.query or parsed.fragment or parsed.params:
        return None
    if parsed.scheme or parsed.netloc:
        try:
            port = parsed.port
        except ValueError:
            return None
        if (
            parsed.scheme != "https"
            or not is_x_url(href)
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            return None
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/?", parsed.path)
    if match:
        return match.group(1).lower()
    return None


class CdpClient:
    """Tiny browser-level CDP client that never exposes a network listener."""

    def __init__(self, version_url: str | None = None) -> None:
        with urllib.request.urlopen(  # nosec B310 - fixed loopback URL in runtime
            version_url or CDP_VERSION_URL, timeout=5
        ) as response:
            websocket_url = json.load(response)["webSocketDebuggerUrl"]
        self.websocket = connect(
            websocket_url,
            open_timeout=5,
            close_timeout=1,
            proxy=None,
        )
        self.request_id = 0

    def close(self) -> None:
        self.websocket.close()

    def request(
        self,
        method: str,
        params: dict[str, object] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = 15,
    ) -> dict[str, Any]:
        self.request_id += 1
        request_id = self.request_id
        message: dict[str, object] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            message["sessionId"] = session_id
        self.websocket.send(json.dumps(message))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = json.loads(
                self.websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
            )
            if response.get("id") != request_id:
                continue
            if response.get("error"):
                raise RuntimeError("Persistent Chromium CDP command failed")
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        raise RuntimeError("Persistent Chromium CDP command timed out")


def _identity_snapshot(client: CdpClient, session_id: str) -> dict[str, object]:
    response = client.request(
        "Runtime.evaluate",
        {
            "expression": IDENTITY_EXPRESSION,
            "awaitPromise": True,
            "returnByValue": True,
        },
        session_id=session_id,
    )
    if response.get("exceptionDetails"):
        raise RuntimeError("X identity check failed")
    value = (response.get("result") or {}).get("value")
    if not isinstance(value, dict):
        raise RuntimeError("X identity check returned an invalid result")
    return value


def inspect_identity(client: CdpClient, *, timeout: float = 35) -> str | None:
    """Return the current handle from a disposable background X target."""

    target_id = str(
        client.request(
            "Target.createTarget", {"url": "about:blank", "background": True}
        )["targetId"]
    )
    try:
        session_id = str(
            client.request(
                "Target.attachToTarget", {"targetId": target_id, "flatten": True}
            )["sessionId"]
        )
        apply_cdp_low_data_controls(client, session_id)
        client.request("Page.enable", session_id=session_id)
        client.request("Page.navigate", {"url": X_HOME_URL}, session_id=session_id)
        deadline = time.monotonic() + max(0, timeout)
        while True:
            snapshot = _identity_snapshot(client, session_id)
            handle = handle_from_snapshot(snapshot)
            if handle:
                return handle
            url = str(snapshot.get("url") or "")
            if is_x_url(url) and bool(snapshot.get("app_ready")):
                parsed = urlparse(url)
                if parsed.path.startswith(("/i/flow/login", "/i/jf/onboarding")):
                    return None
                if snapshot.get("challenge"):
                    return None
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)
    finally:
        try:
            client.request("Target.closeTarget", {"targetId": target_id}, timeout=5)
        except Exception:
            pass


def _has_session_cookies(client: CdpClient) -> bool:
    response = client.request("Storage.getCookies")
    found: set[str] = set()
    now = time.time()
    for item in response.get("cookies", []):
        if not isinstance(item, dict) or not cookie_domain_allowed(item.get("domain")):
            continue
        name = item.get("name")
        value = item.get("value")
        expires = item.get("expires")
        if (
            name in REQUIRED_SESSION_COOKIES
            and isinstance(value, str)
            and value
            and isinstance(expires, (int, float))
            and not isinstance(expires, bool)
            and math.isfinite(float(expires))
            and expires > now + 60
        ):
            found.add(str(name))
    return found == REQUIRED_SESSION_COOKIES


def _clear_existing_x_cookies(client: CdpClient) -> None:
    """Remove every existing X/Twitter cookie before applying a replacement."""

    response = client.request("Storage.getCookies")
    expired: list[dict[str, object]] = []
    for item in response.get("cookies", []):
        if not isinstance(item, dict) or not cookie_domain_allowed(item.get("domain")):
            continue
        name = item.get("name")
        domain = item.get("domain")
        if not isinstance(name, str) or not name or not isinstance(domain, str):
            continue
        params: dict[str, object] = {
            "name": name,
            "value": "",
            "domain": domain,
            "path": "/",
            "expires": 1,
        }
        path = item.get("path")
        if isinstance(path, str) and path:
            params["path"] = path
        expired.append(params)
    if expired:
        # Storage.setCookies is a browser-level CDP command. Writing an empty,
        # already-expired value deletes the exact name/domain/path tuple and
        # handles both x.com and .x.com duplicates without attaching to or
        # navigating any existing page target.
        client.request("Storage.setCookies", {"cookies": expired})


def _live_status_unlocked(
    *,
    client_factory: Callable[[], CdpClient] = CdpClient,
) -> dict[str, object]:
    require_assigned_proxy_bridge()
    expected = load_expected_handle()
    result: dict[str, object] = {
        "configured": True,
        "session_present": False,
        "account_verified": False,
        "expected_handle": expected,
        "version": X_USE_VERSION,
    }
    client: CdpClient | None = None
    try:
        client = client_factory()
        result["session_present"] = _has_session_cookies(client)
        if not result["session_present"]:
            result["status"] = "not_configured"
            result["error"] = "X session cookies are missing or expired"
            return result
        actual = inspect_identity(client)
        if actual:
            result["authenticated_handle"] = actual
        if actual == expected:
            result["status"] = "ready"
            result["account_verified"] = True
            return result
        if actual:
            result["status"] = "wrong_account"
            result["error"] = "Persistent Chromium is authenticated as another X account"
            return result
        result["status"] = "not_configured"
        result["error"] = "X session could not be verified"
        return result
    except RuntimeConfigurationError:
        raise
    except Exception:
        result["status"] = "error"
        result["error"] = "Persistent Chromium X session check failed"
        return result
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def live_status(
    *,
    client_factory: Callable[[], CdpClient] = CdpClient,
) -> dict[str, object]:
    lock = acquire_browser_action_lock()
    try:
        return _live_status_unlocked(client_factory=client_factory)
    finally:
        release_browser_action_lock(lock)


def import_session(
    raw: bytes,
    *,
    client_factory: Callable[[], CdpClient] = CdpClient,
) -> dict[str, object]:
    """Apply a validated export directly to Chromium and verify its handle."""

    require_assigned_proxy_bridge()
    cookies = validate_session_export(raw)
    lock = acquire_browser_action_lock()
    try:
        client: CdpClient | None = None
        try:
            client = client_factory()
            _clear_existing_x_cookies(client)
            response = client.request("Storage.setCookies", {"cookies": cookies})
            if response.get("success") is False:
                raise RuntimeError("Persistent Chromium rejected the X session")
        finally:
            # Drop the only normalized copy before the live identity request.
            # The original bytes are owned by the caller and never persisted.
            cookies.clear()
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        return _live_status_unlocked(client_factory=client_factory)
    finally:
        release_browser_action_lock(lock)
