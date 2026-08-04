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
from pathlib import Path


LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = int(os.environ.get("HERMES_RESIDENTIAL_PROXY_PORT", "8899"))
STATE_PATH = Path(os.environ.get("HERMES_RESIDENTIAL_PROXY_STATE_PATH", "/opt/data/residential-proxy/state.json"))
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


def upstream_authorization() -> str:
    base_username = os.environ["HERMES_RESIDENTIAL_PROXY_BASE_USERNAME"]
    country = os.environ["HERMES_RESIDENTIAL_PROXY_COUNTRY"]
    city = os.environ["HERMES_RESIDENTIAL_PROXY_CITY"]
    password = os.environ.get("HERMES_RESIDENTIAL_PROXY_PASSWORD", "")
    username = f"{base_username}-{country}-city_{city}-{selector()}"
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


async def read_headers(reader: asyncio.StreamReader) -> tuple[str, list[str]]:
    raw = await reader.readuntil(b"\r\n\r\n")
    lines = raw.decode("iso-8859-1").split("\r\n")
    return lines[0], [line for line in lines[1:] if line]


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


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while chunk := await reader.read(64 * 1024):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    upstream_writer: asyncio.StreamWriter | None = None
    try:
        request_line, headers = await asyncio.wait_for(read_headers(client_reader), timeout=15)
        method, target, _ = request_line.split(" ", 2)
        upstream_reader, upstream_writer = await asyncio.open_connection(
            os.environ["HERMES_RESIDENTIAL_PROXY_HOST"],
            int(os.environ["HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT"]),
        )
        authorization = upstream_authorization()
        if method.upper() == "CONNECT":
            upstream_writer.write(
                f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n".encode()
                + with_proxy_authorization(
                    headers,
                    authorization,
                    drop_host=True,
                )
            )
            await upstream_writer.drain()
            status, _ = await read_headers(upstream_reader)
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
                client_writer.write(
                    (
                        "HTTP/1.1 502 Bad Gateway\r\n"
                        f"X-Residential-Proxy-Upstream-Status: {upstream_status}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode()
                )
                await client_writer.drain()
                return
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
        else:
            upstream_writer.write(request_line.encode("iso-8859-1") + b"\r\n")
            upstream_writer.write(with_proxy_authorization(headers, authorization))
            await upstream_writer.drain()
        await asyncio.gather(
            pipe(client_reader, upstream_writer),
            pipe(upstream_reader, client_writer),
        )
    except (OSError, ValueError, asyncio.LimitOverrunError, asyncio.IncompleteReadError, TimeoutError):
        try:
            client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await client_writer.drain()
        except ConnectionError:
            pass
    finally:
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
