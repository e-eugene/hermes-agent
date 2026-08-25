#!/usr/bin/env python3
"""Local authenticated forward proxy for residential upstreams.

Chromium connects without credentials to this loopback proxy.  The bridge reads
the current selector from a mode file and supplies the provider credentials to
the configured upstream proxy for each new client connection.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit


LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = int(os.environ.get("HERMES_RESIDENTIAL_PROXY_PORT", "8899"))
STATE_PATH = Path(os.environ.get("HERMES_RESIDENTIAL_PROXY_STATE_PATH", "/opt/data/residential-proxy/state.json"))
DEFAULT_BLOCKED_MEDIA_HOSTS = ("pbs.twimg.com", "video.twimg.com", "upload.twitter.com")
logger = logging.getLogger("residential-proxy")


def session_id() -> str:
    return str(secrets.randbelow(900_000_000) + 100_000_000)


def selector() -> str:
    try:
        value = json.loads(STATE_PATH.read_text()).get("selector", "")
        if value == "rotate" or (isinstance(value, str) and value.isdecimal()):
            return value
    except (OSError, ValueError, TypeError):
        pass
    return session_id()


def write_selector(value: str) -> None:
    if value != "rotate" and (not value.isdecimal() or not value):
        raise ValueError("selector must be rotate or a numeric session ID")
    STATE_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = STATE_PATH.with_name(
        f".{STATE_PATH.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    try:
        temporary.write_text(json.dumps({"selector": value}) + "\n")
        temporary.chmod(0o600)
        temporary.replace(STATE_PATH)
    finally:
        temporary.unlink(missing_ok=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def blocked_media_hosts() -> tuple[str, ...]:
    configured = os.environ.get("HERMES_RESIDENTIAL_PROXY_BLOCKED_MEDIA_HOSTS")
    if not configured:
        return DEFAULT_BLOCKED_MEDIA_HOSTS
    return tuple(
        host.strip().lower()
        for host in configured.split(",")
        if host.strip()
    )


def normalize_host(value: object) -> str:
    text = str(value or "").strip().lower().rstrip(".")
    if not text:
        return ""
    if "://" in text:
        try:
            return (urlsplit(text).hostname or "").lower().rstrip(".")
        except ValueError:
            return ""
    if "@" in text:
        text = text.rsplit("@", 1)[1]
    for separator in ("/", "?", "#"):
        text = text.split(separator, 1)[0]
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")].rstrip(".")
    if text.count(":") == 1:
        text = text.rsplit(":", 1)[0]
    return text.rstrip(".")


def request_host(method: str, target: str, headers: list[str]) -> str:
    if method.upper() == "CONNECT":
        return normalize_host(target)
    try:
        parsed_host = urlsplit(target).hostname
    except ValueError:
        parsed_host = None
    if parsed_host:
        return normalize_host(parsed_host)
    for header in headers:
        name, separator, value = header.partition(":")
        if separator and name.strip().lower() == "host":
            return normalize_host(value)
    return ""


def media_host_blocked(host: str) -> bool:
    normalized = normalize_host(host)
    return any(
        normalized == blocked or normalized.endswith("." + blocked)
        for blocked in blocked_media_hosts()
    )


def safe_log_text(value: object, *, limit: int = 255) -> str:
    text = str(value or "")[:limit]
    return "".join(char for char in text if 32 <= ord(char) < 127)


def log_traffic(
    *,
    method: str,
    target_host: str,
    selector_value: str,
    started_at: float,
    bytes_to_upstream: int,
    bytes_from_upstream: int,
    bytes_to_client: int,
    blocked: bool,
    outcome: str,
    upstream_status: str | None = None,
    block_reason: str | None = None,
) -> None:
    if not env_bool("HERMES_RESIDENTIAL_PROXY_LOG_TRAFFIC", default=True):
        return
    payload: dict[str, object] = {
        "event": "residential_proxy_traffic",
        "method": safe_log_text(method, limit=16),
        "target_host": safe_log_text(normalize_host(target_host), limit=255),
        "bytes_to_upstream": max(0, int(bytes_to_upstream)),
        "bytes_from_upstream": max(0, int(bytes_from_upstream)),
        "bytes_to_client": max(0, int(bytes_to_client)),
        "duration_ms": round((time.monotonic() - started_at) * 1000, 1),
        "selector": safe_log_text(selector_value, limit=32),
        "blocked": bool(blocked),
        "outcome": safe_log_text(outcome, limit=32),
    }
    if upstream_status:
        payload["upstream_status"] = safe_log_text(upstream_status, limit=16)
    if block_reason:
        payload["block_reason"] = safe_log_text(block_reason, limit=64)
    logger.info(
        "traffic %s",
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
    )


def upstream_authorization(selection: str | None = None) -> str:
    base_username = os.environ["HERMES_RESIDENTIAL_PROXY_BASE_USERNAME"]
    country = os.environ["HERMES_RESIDENTIAL_PROXY_COUNTRY"]
    city = os.environ["HERMES_RESIDENTIAL_PROXY_CITY"]
    password = os.environ.get("HERMES_RESIDENTIAL_PROXY_PASSWORD", "")
    username = f"{base_username}-{country}-city_{city}-{selection or selector()}"
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


async def read_headers(reader: asyncio.StreamReader) -> tuple[str, list[str], bytes]:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("iso-8859-1").split("\r\n")
    return lines[0], [line for line in lines[1:] if line], raw


def with_proxy_authorization(
    headers: list[str],
    value: str,
    *,
    drop_host: bool = False,
) -> bytes:
    blocked_prefixes = ["proxy-authorization:", "proxy-connection:"]
    if drop_host:
        blocked_prefixes.append("host:")
    safe_headers = [
        line
        for line in headers
        if not line.lower().startswith(tuple(blocked_prefixes))
    ]
    safe_headers.append(f"Proxy-Authorization: {value}")
    return ("\r\n".join(safe_headers) + "\r\n\r\n").encode("iso-8859-1")


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    byte_count = 0
    try:
        while chunk := await reader.read(64 * 1024):
            byte_count += len(chunk)
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
    return byte_count


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    started_at = time.monotonic()
    upstream_writer: asyncio.StreamWriter | None = None
    method = "unknown"
    target_host = ""
    selected_selector = ""
    bytes_to_upstream = 0
    bytes_from_upstream = 0
    bytes_to_client = 0
    blocked = False
    block_reason: str | None = None
    upstream_status: str | None = None
    outcome = "error"
    try:
        request_line, headers, _ = await asyncio.wait_for(read_headers(client_reader), timeout=15)
        method, target, _ = request_line.split(" ", 2)
        method = method.upper()
        target_host = request_host(method, target, headers)
        selected_selector = selector()
        if media_host_blocked(target_host):
            blocked = True
            block_reason = "media_host"
            outcome = "blocked"
            response = (
                "HTTP/1.1 403 Forbidden\r\n"
                "X-Residential-Proxy-Blocked: media\r\n"
                "Connection: close\r\n\r\n"
            ).encode()
            bytes_to_client += len(response)
            client_writer.write(response)
            await client_writer.drain()
            return
        upstream_reader, upstream_writer = await asyncio.open_connection(
            os.environ["HERMES_RESIDENTIAL_PROXY_HOST"],
            int(os.environ["HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT"]),
        )
        authorization = upstream_authorization(selected_selector)
        if method == "CONNECT":
            request_bytes = (
                f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode()
                + with_proxy_authorization(headers, authorization, drop_host=True)
            )
            upstream_writer.write(request_bytes)
            bytes_to_upstream += len(request_bytes)
            await upstream_writer.drain()
            status, _, raw_status = await read_headers(upstream_reader)
            bytes_from_upstream += len(raw_status)
            if not status.startswith("HTTP/") or " 2" not in status[:12]:
                parts = status.split(" ", 2)
                upstream_status = (
                    parts[1]
                    if len(parts) > 1 and parts[1].isdigit()
                    else "unknown"
                )
                logger.warning(
                    "Upstream residential proxy rejected CONNECT with status %s",
                    upstream_status,
                )
                response = (
                    "HTTP/1.1 502 Bad Gateway\r\n"
                    f"X-Residential-Proxy-Upstream-Status: {upstream_status}\r\n"
                    "Connection: close\r\n\r\n"
                )
                bytes_to_client += len(response.encode())
                client_writer.write(response.encode())
                await client_writer.drain()
                return
            response = b"HTTP/1.1 200 Connection Established\r\n\r\n"
            bytes_to_client += len(response)
            client_writer.write(response)
            await client_writer.drain()
        else:
            request_bytes = (
                request_line.encode("iso-8859-1")
                + b"\r\n"
                + with_proxy_authorization(headers, authorization)
            )
            upstream_writer.write(request_bytes)
            bytes_to_upstream += len(request_bytes)
            await upstream_writer.drain()
        client_to_upstream, upstream_to_client = await asyncio.gather(
            pipe(client_reader, upstream_writer),
            pipe(upstream_reader, client_writer),
        )
        bytes_to_upstream += client_to_upstream
        bytes_from_upstream += upstream_to_client
        bytes_to_client += upstream_to_client
        outcome = "ok"
    except (OSError, ValueError, UnicodeError, asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError):
        try:
            response = b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n"
            bytes_to_client += len(response)
            client_writer.write(response)
            await client_writer.drain()
        except ConnectionError:
            pass
    finally:
        log_traffic(
            method=method,
            target_host=target_host,
            selector_value=selected_selector,
            started_at=started_at,
            bytes_to_upstream=bytes_to_upstream,
            bytes_from_upstream=bytes_from_upstream,
            bytes_to_client=bytes_to_client,
            blocked=blocked,
            outcome=outcome,
            upstream_status=upstream_status,
            block_reason=block_reason,
        )
        if upstream_writer is not None:
            upstream_writer.close()
        client_writer.close()


async def serve() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not STATE_PATH.exists():
        write_selector(session_id())
    server = await asyncio.start_server(handle_client, LOCAL_HOST, LOCAL_PORT)
    async with server:
        await server.serve_forever()


def command() -> None:
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    if action == "status":
        print(json.dumps({"selector": selector(), "proxy_url": f"http://{LOCAL_HOST}:{LOCAL_PORT}"}))
    elif action == "url":
        print(f"http://{LOCAL_HOST}:{LOCAL_PORT}")
    elif action == "rotate":
        write_selector("rotate")
        print("Residential proxy mode: rotate")
    elif action == "sticky":
        value = sys.argv[2] if len(sys.argv) > 2 else session_id()
        write_selector(value)
        print(f"Residential proxy mode: sticky ({value})")
    else:
        raise SystemExit("usage: residential-proxy [status|url|sticky [session-id]|rotate]")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        asyncio.run(serve())
    else:
        command()
