#!/opt/x-use/.venv/bin/python
"""Authenticated private runtime control API.

Only a trusted dashboard can reach this listener. Responses deliberately expose
capability, account state and browser egress information, never credentials,
proxy endpoints, local paths or helper stderr.
"""

from __future__ import annotations

import ipaddress
import asyncio
import json
import os
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import AF_INET6
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen


for module_path in (Path(__file__).resolve().parent, Path("/opt/hermes-runtime")):
    if str(module_path) not in sys.path:
        sys.path.insert(0, str(module_path))

from hermes_x_use_common import (  # noqa: E402
    MAX_X_TEXT_CHARS,
    MAX_SESSION_BYTES,
    RuntimeConfigurationError,
    SessionImportError,
    canonical_x_status_url,
    has_credential_like_content,
    import_session,
    live_status,
    normalize_handle,
    require_assigned_proxy_bridge,
)


TOKEN = os.environ.get("API_SERVER_KEY", "")
TUI_PORT = os.environ.get("HERMES_TUI_PORT", "9119")
BROWSER_GATEWAY_PORT = int(os.environ.get("HERMES_BROWSER_GATEWAY_PORT", "6081"))
NETWORK_MODES = {"assigned_proxy", "direct"}
if not TOKEN:
    raise SystemExit("API_SERVER_KEY must be non-empty")

# Do not make regular readiness probes depend on an external IP-echo provider.
# This snapshot is refreshed when the dashboard explicitly requests the network
# diagnostic and remains deliberately non-persistent.
NETWORK_SNAPSHOT: dict[str, object] = {}
X_USE_ACTION_LOCK = threading.Lock()
X_USE_COMMIT = "e57e215e45b3e68cbd8cd7c46799cd932c234eac"
X_USE_PREFLIGHT_MARKER = Path(
    os.environ.get(
        "HERMES_X_USE_PREFLIGHT_MARKER",
        "/tmp/hermes-x-use/native-mcp-ready.json",
    )
)


def browser_network_mode() -> str:
    configured = os.environ.get("HERMES_BROWSER_NETWORK_MODE")
    if configured in NETWORK_MODES:
        return configured
    return (
        "assigned_proxy"
        if os.environ.get("HERMES_RESIDENTIAL_PROXY_ENABLED") == "true"
        else "direct"
    )


def x_use_preflight_ready() -> bool:
    """Trust only the private, exact marker written by native discovery."""

    try:
        metadata = X_USE_PREFLIGHT_MARKER.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & 0o077
            or metadata.st_size > 512
        ):
            return False
        payload = json.loads(X_USE_PREFLIGHT_MARKER.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    return payload == {
        "commit": X_USE_COMMIT,
        "tool_count": 13,
        "version": "2.4.1",
    }


def capabilities() -> list[str]:
    base = [
        "persistent_browser_profile",
        "remote_chromium",
        "network_status",
    ]
    try:
        require_assigned_proxy_bridge()
    except RuntimeConfigurationError:
        return base
    if not x_use_preflight_ready():
        return base
    return ["x_use_mcp", "x_session_import", "x_draft_approval", *base]


def safe_x_use_status(payload: object) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    status = str(source.get("status") or "error")
    if status not in {"ready", "not_configured", "wrong_account", "error"}:
        status = "error"
    result: dict[str, object] = {
        "status": status,
        "configured": bool(source.get("configured")),
        "session_present": bool(source.get("session_present")),
        "account_verified": bool(source.get("account_verified")),
        "version": "2.4.1",
    }
    if expected := safe_login(source.get("expected_handle")):
        result["expected_handle"] = expected
    if actual := safe_login(source.get("authenticated_handle")):
        result["authenticated_handle"] = actual
    fixed_errors = {
        "not_configured": "X session is missing, expired, or could not be verified",
        "wrong_account": "Persistent Chromium is authenticated as another X account",
        "error": "Persistent Chromium X session check failed",
    }
    if status in fixed_errors:
        result["error"] = fixed_errors[status]
    return result


def x_use_status() -> tuple[int, dict[str, object]]:
    try:
        payload = safe_x_use_status(live_status())
    except RuntimeConfigurationError:
        payload = safe_x_use_status(
            {"status": "error", "configured": False, "session_present": False}
        )
    status = str(payload["status"])
    # Status is a state snapshot, not an action result. A missing session or a
    # wrong account is valid dashboard data and must not be discarded by an
    # HTTP client that treats non-2xx responses as transport failures.
    return (200 if status in {"ready", "not_configured", "wrong_account"} else 503, payload)


def x_use_import_session(raw: bytes) -> tuple[int, dict[str, object]]:
    try:
        payload = safe_x_use_status(import_session(raw))
    except SessionImportError:
        return 422, {
            "status": "error",
            "configured": False,
            "session_present": False,
            "account_verified": False,
            "version": "2.4.1",
            "error": "Invalid X cookie export",
        }
    except RuntimeConfigurationError:
        return 503, {
            "status": "error",
            "configured": False,
            "session_present": False,
            "account_verified": False,
            "version": "2.4.1",
            "error": "X account assignment is invalid",
        }
    except Exception:
        return 503, safe_x_use_status({"status": "error"})
    status = str(payload["status"])
    return ({"ready": 200, "not_configured": 409, "wrong_account": 409}.get(status, 503), payload)


def safe_drafts_payload(payload: object) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    result: list[dict[str, object]] = []
    for item in source.get("drafts", []):
        if not isinstance(item, dict):
            continue
        draft_id = str(item.get("draft_id") or "")
        action = str(item.get("action") or "")
        status = str(item.get("status") or "")
        if (
            not draft_id
            or len(draft_id) > 128
            or not draft_id.isascii()
            or not all(char.isalnum() or char in "_-" for char in draft_id)
            or action not in {"post_tweet", "reply_to_tweet"}
        ):
            continue
        if status not in {"pending", "executed", "failed", "rejected"}:
            continue
        raw_payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        text = raw_payload.get("text")
        if (
            not isinstance(text, str)
            or not text
            or text != text.strip()
            or len(text) > MAX_X_TEXT_CHARS
            or has_credential_like_content(text)
        ):
            continue
        if action == "post_tweet":
            if set(raw_payload) != {"text"}:
                continue
            public_payload: dict[str, object] = {"text": text}
        else:
            if set(raw_payload) != {"text", "tweet_url", "tweet_id"}:
                continue
            try:
                canonical_url, canonical_id = canonical_x_status_url(
                    raw_payload.get("tweet_url")
                )
            except ValueError:
                continue
            if (
                raw_payload.get("tweet_url") != canonical_url
                or raw_payload.get("tweet_id") != canonical_id
            ):
                continue
            public_payload = {
                "text": text,
                "tweet_url": canonical_url,
                "tweet_id": canonical_id,
            }
        try:
            account = normalize_handle(item.get("account"))
        except RuntimeConfigurationError:
            continue
        result.append(
            {
                "draft_id": draft_id,
                "account": account,
                "action": action,
                "payload": public_payload,
                "created_at": str(item.get("created_at") or "")[:128],
                "status": status,
            }
        )
    return {"drafts": result, "count": len(result)}


def x_use_drafts(status: str | None, limit: int) -> tuple[int, dict[str, object]]:
    try:
        from hermes_x_use_adapter import list_dashboard_drafts

        payload = list_dashboard_drafts(status=status, limit=limit)
    except (RuntimeError, ValueError):
        return 422, {"status": "error", "error": "Invalid draft query"}
    except Exception:
        return 503, {"status": "error", "error": "Draft store is unavailable"}
    return 200, safe_drafts_payload(payload)


def safe_draft_action_payload(payload: object) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    result: dict[str, object] = {
        "draft_id": str(source.get("draft_id") or "")[:128],
        "status": str(source.get("status") or "error"),
    }
    raw_result = source.get("result")
    if isinstance(raw_result, dict):
        try:
            account = normalize_handle(raw_result.get("account"))
        except RuntimeConfigurationError:
            account = ""
        action = str(raw_result.get("action") or "")
        if action == "reply":
            action = "reply_to_tweet"
        success = raw_result.get("success")
        if (
            account
            and action in {"post_tweet", "reply_to_tweet"}
            and isinstance(success, bool)
        ):
            public_result: dict[str, object] = {
                "account": account,
                "action": action,
                "success": success,
            }
            tweet_id = raw_result.get("tweet_id")
            if action == "reply_to_tweet":
                if (
                    not isinstance(tweet_id, str)
                    or not tweet_id.isascii()
                    or not tweet_id.isdigit()
                ):
                    return result
                public_result["tweet_id"] = tweet_id
            result["result"] = public_result
    return result


def x_use_draft_action(draft_id: str, action: str) -> tuple[int, dict[str, object]]:
    if not X_USE_ACTION_LOCK.acquire(blocking=False):
        return 409, {"status": "busy", "error": "Another X action is in progress"}
    try:
        from hermes_x_use_adapter import (
            DraftConflictError,
            WrongAccountError,
            approve_dashboard_draft,
            reject_dashboard_draft,
        )

        try:
            if action == "approve":
                # Do not wrap live Selenium publishing in asyncio.wait_for:
                # cancellation cannot stop its worker thread and would report
                # failure while a post could still be completing. The handler
                # and action lock stay alive until the definitive result.
                payload = asyncio.run(approve_dashboard_draft(draft_id))
            else:
                payload = reject_dashboard_draft(draft_id)
        except KeyError:
            return 404, {"status": "error", "error": "Draft was not found"}
        except (DraftConflictError, WrongAccountError):
            return 409, {"status": "error", "error": "Draft cannot be executed"}
        except Exception:
            return 503, {"status": "error", "error": "X action failed"}
        return 200, safe_draft_action_payload(payload)
    finally:
        X_USE_ACTION_LOCK.release()


def healthy() -> bool:
    if browser_network_mode() == "assigned_proxy" and not x_use_preflight_ready():
        return False
    checks = (
        ("http://[::1]:8642/health", {"Authorization": f"Bearer {TOKEN}"}),
        ("http://127.0.0.1:9222/json/version", {}),
        (
            f"http://127.0.0.1:{TUI_PORT}/api/status",
            {"Authorization": f"Bearer {TOKEN}", "Host": f"localhost:{TUI_PORT}"},
        ),
    )
    try:
        for url, headers in checks:
            with urlopen(Request(url, headers=headers), timeout=2) as response:  # nosec B310
                if response.status != 200:
                    return False
        with socket.create_connection(("127.0.0.1", 5900), timeout=2):
            pass
        with socket.create_connection(("::1", BROWSER_GATEWAY_PORT), timeout=2):
            pass
    except (OSError, URLError):
        return False
    return True


def safe_login(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    # Account names are model-visible metadata, but cap/control characters avoid
    # turning an unexpected helper response into an information-disclosure path.
    candidate = value.strip()
    if not candidate or len(candidate) > 128 or any(ord(char) < 32 for char in candidate):
        return None
    return candidate


def safe_network_payload(payload: object) -> dict[str, object]:
    source = payload if isinstance(payload, dict) else {}
    mode = browser_network_mode()
    result: dict[str, object] = {
        "status": "unhealthy",
        "mode": mode,
    }
    if source.get("status") == "ok":
        try:
            exit_ip = ipaddress.ip_address(str(source.get("exit_ip") or ""))
        except ValueError:
            exit_ip = None
        if exit_ip and exit_ip.is_global:
            result["status"] = "healthy"
            result["exit_ip"] = str(exit_ip)
            return result
    result["error"] = "Browser network diagnostic failed"
    return result


def browser_network_status() -> tuple[int, dict[str, object]]:
    global NETWORK_SNAPSHOT
    try:
        result = subprocess.run(
            ["/usr/local/bin/hermes-browser-network-status"],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        payload = json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        payload = {
            "status": "unavailable",
            "mode": browser_network_mode(),
            "error": "Browser network diagnostic failed",
        }
        NETWORK_SNAPSHOT = payload
        return 503, payload
    sanitized = safe_network_payload(payload)
    NETWORK_SNAPSHOT = sanitized
    return (200 if result.returncode == 0 and sanitized["status"] == "healthy" else 503), sanitized


def network_snapshot() -> dict[str, object]:
    snapshot = NETWORK_SNAPSHOT.copy()
    snapshot.setdefault("status", "unavailable")
    snapshot["mode"] = browser_network_mode()
    return snapshot


class Handler(BaseHTTPRequestHandler):
    def authorized(self) -> bool:
        return self.headers.get("Authorization") == f"Bearer {TOKEN}"

    def respond(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self.authorized():
            self.respond(404, {"status": "not_found"})
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            ready = healthy()
            self.respond(
                200 if ready else 503,
                {
                    "status": "ok" if ready else "unhealthy",
                    "capabilities": capabilities(),
                    "network": network_snapshot(),
                },
            )
            return
        if parsed.path == "/network/status":
            status, payload = browser_network_status()
            self.respond(status, payload)
            return
        if parsed.path == "/x-use/status":
            status, payload = x_use_status()
            self.respond(status, payload)
            return
        if parsed.path == "/x-use/drafts":
            query = parse_qs(parsed.query)
            raw_status = query.get("status", ["pending"])[0]
            draft_status = None if raw_status == "all" else raw_status
            try:
                limit = int(query.get("limit", ["100"])[0])
            except (TypeError, ValueError):
                self.respond(422, {"status": "error", "error": "Invalid draft query"})
                return
            status, payload = x_use_drafts(draft_status, limit)
            self.respond(status, payload)
            return
        self.respond(404, {"status": "not_found"})

    def do_PUT(self) -> None:  # noqa: N802
        if not self.authorized():
            self.respond(404, {"status": "not_found"})
            return
        if urlsplit(self.path).path != "/x-use/session":
            self.respond(404, {"status": "not_found"})
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self.respond(415, {"status": "error", "error": "application/json required"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = 0
        if content_length <= 0:
            self.respond(422, {"status": "error", "error": "Invalid X cookie export"})
            return
        if content_length > MAX_SESSION_BYTES:
            self.respond(413, {"status": "error", "error": "X cookie export is too large"})
            return
        raw = self.rfile.read(content_length)
        try:
            status, payload = x_use_import_session(raw)
        finally:
            del raw
        self.respond(status, payload)

    def do_POST(self) -> None:  # noqa: N802
        if not self.authorized():
            self.respond(404, {"status": "not_found"})
            return
        parts = urlsplit(self.path).path.strip("/").split("/")
        if (
            len(parts) == 4
            and parts[:2] == ["x-use", "drafts"]
            and parts[3] in {"approve", "reject"}
        ):
            status, payload = x_use_draft_action(parts[2], parts[3])
            self.respond(status, payload)
            return
        self.respond(404, {"status": "not_found"})

    def log_message(self, format: str, *args: object) -> None:
        return


class Server(ThreadingHTTPServer):
    address_family = AF_INET6
    daemon_threads = True


def main() -> None:
    # Dashboard approvals write x-use metrics/dedup state from this process.
    # Keep every newly-created persistent runtime file private by default.
    os.umask(0o077)
    Server(("::", 8643), Handler).serve_forever()


if __name__ == "__main__":
    main()
