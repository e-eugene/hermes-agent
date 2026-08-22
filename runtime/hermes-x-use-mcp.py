#!/opt/x-use/.venv/bin/python
"""Curated stdio MCP entry point for Hermes."""

from __future__ import annotations

import asyncio
import logging
import os
import sys


sys.path.insert(0, "/opt/hermes-runtime")
os.umask(0o077)

from xuse.mcp.server import _enforce_stdio_stdout_hygiene  # noqa: E402

from hermes_x_use_adapter import create_safe_server, shutdown_safe_server  # noqa: E402


def configure_network_environment() -> None:
    """Force non-browser x-use HTTP reads onto the selected browser route."""

    proxy_keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    )
    for key in proxy_keys:
        os.environ.pop(key, None)
    no_proxy = "127.0.0.1,localhost,::1"
    os.environ["NO_PROXY"] = no_proxy
    os.environ["no_proxy"] = no_proxy
    mode = os.environ.get("HERMES_BROWSER_NETWORK_MODE", "")
    if mode != "assigned_proxy":
        raise RuntimeError("x-use requires the assigned proxy route")
    port = os.environ.get("HERMES_RESIDENTIAL_PROXY_PORT", "8899")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise RuntimeError("X loopback proxy port is invalid")
    expected = f"http://127.0.0.1:{port}"
    if os.environ.get("RESIDENTIAL_PROXY_URL") != expected:
        raise RuntimeError("X loopback proxy bridge is unavailable")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[key] = expected


def main() -> None:
    _enforce_stdio_stdout_hygiene()
    configure_network_environment()
    server = create_safe_server()
    try:
        server.run()
    finally:
        try:
            asyncio.run(shutdown_safe_server(server))
        except Exception:
            logging.getLogger(__name__).exception("x-use MCP cleanup failed")


if __name__ == "__main__":
    main()
