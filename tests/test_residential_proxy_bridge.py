import asyncio
import base64
import importlib.util
from pathlib import Path

import pytest


def load_bridge_module():
    path = Path(__file__).parents[1] / "runtime" / "residential-proxy.py"
    spec = importlib.util.spec_from_file_location("residential_proxy_bridge", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_persists_mode_and_builds_upstream_auth(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_BASE_USERNAME", "res-user")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_COUNTRY", "gb")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_CITY", "london")
    monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PASSWORD", "proxy-pass")
    bridge = load_bridge_module()
    bridge.STATE_PATH = tmp_path / "state.json"

    bridge.write_selector("26819")

    assert bridge.selector() == "26819"
    encoded = bridge.upstream_authorization().removeprefix("Basic ")
    assert base64.b64decode(encoded).decode() == "res-user-gb-city_london-26819:proxy-pass"

    headers = bridge.with_proxy_authorization(
        ["Host: example.test", "Proxy-Authorization: stale"], "Basic fresh"
    ).decode()
    assert "stale" not in headers
    assert "Proxy-Authorization: Basic fresh" in headers


def test_bridge_forwards_http_request_with_upstream_auth(monkeypatch, tmp_path: Path) -> None:
    async def exercise() -> bytes:
        captured: list[bytes] = []

        async def upstream_handler(reader, writer) -> None:
            captured.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
            await writer.drain()
            writer.close()

        bridge = load_bridge_module()
        bridge.STATE_PATH = tmp_path / "state.json"
        bridge.write_selector("26819")
        try:
            upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        except PermissionError:
            pytest.skip("sandbox does not permit loopback TCP listeners")
        upstream_port = upstream.sockets[0].getsockname()[1]
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_HOST", "127.0.0.1")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT", str(upstream_port))
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_BASE_USERNAME", "res-user")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_COUNTRY", "gb")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_CITY", "london")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PASSWORD", "proxy-pass")
        local = await asyncio.start_server(bridge.handle_client, "127.0.0.1", 0)
        local_port = local.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
        writer.write(b"GET http://example.test/ HTTP/1.1\r\nHost: example.test\r\n\r\n")
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        local.close()
        upstream.close()
        await local.wait_closed()
        await upstream.wait_closed()
        assert len(captured) == 1
        return captured[0] + b"\n---\n" + response

    result = asyncio.run(exercise())

    forwarded, response = result.split(b"\n---\n", 1)
    assert forwarded.startswith(b"GET http://example.test/ HTTP/1.1")
    assert b"Proxy-Authorization: Basic " in forwarded
    assert response.endswith(b"\r\n\r\nOK")


def test_bridge_connect_sends_exactly_one_host_header(monkeypatch, tmp_path: Path) -> None:
    async def exercise() -> tuple[bytes, bytes, bytes]:
        captured: list[bytes] = []

        async def upstream_handler(reader, writer) -> None:
            captured.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            assert await reader.readexactly(4) == b"ping"
            writer.write(b"pong")
            await writer.drain()
            writer.close()

        bridge = load_bridge_module()
        bridge.STATE_PATH = tmp_path / "state.json"
        bridge.write_selector("26819")
        try:
            upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        except PermissionError:
            pytest.skip("sandbox does not permit loopback TCP listeners")
        upstream_port = upstream.sockets[0].getsockname()[1]
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_HOST", "127.0.0.1")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT", str(upstream_port))
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_BASE_USERNAME", "res-user")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_COUNTRY", "gb")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_CITY", "london")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PASSWORD", "proxy-pass")
        local = await asyncio.start_server(bridge.handle_client, "127.0.0.1", 0)
        local_port = local.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
        writer.write(
            b"CONNECT example.test:443 HTTP/1.1\r\n"
            b"Host: example.test:443\r\n"
            b"Proxy-Connection: keep-alive\r\n\r\n"
        )
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        writer.write(b"ping")
        await writer.drain()
        tunneled = await reader.readexactly(4)
        writer.close()
        await writer.wait_closed()
        local.close()
        upstream.close()
        await local.wait_closed()
        await upstream.wait_closed()
        assert len(captured) == 1
        return captured[0], response, tunneled

    forwarded, response, tunneled = asyncio.run(exercise())

    assert forwarded.count(b"\r\nHost: example.test:443\r\n") == 1
    assert b"Proxy-Authorization: Basic " in forwarded
    assert response == b"HTTP/1.1 200 Connection Established\r\n\r\n"
    assert tunneled == b"pong"


def test_bridge_maps_upstream_rejection_to_safe_error(monkeypatch, tmp_path: Path) -> None:
    async def exercise() -> bytes:
        async def upstream_handler(reader, writer) -> None:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(
                b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                b"X-Upstream-Secret: must-not-leak\r\n\r\n"
            )
            await writer.drain()
            writer.close()

        bridge = load_bridge_module()
        bridge.STATE_PATH = tmp_path / "state.json"
        bridge.write_selector("26819")
        try:
            upstream = await asyncio.start_server(upstream_handler, "127.0.0.1", 0)
        except PermissionError:
            pytest.skip("sandbox does not permit loopback TCP listeners")
        upstream_port = upstream.sockets[0].getsockname()[1]
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_HOST", "127.0.0.1")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_ENDPOINT_PORT", str(upstream_port))
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_BASE_USERNAME", "res-user")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_COUNTRY", "gb")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_CITY", "london")
        monkeypatch.setenv("HERMES_RESIDENTIAL_PROXY_PASSWORD", "proxy-pass")
        local = await asyncio.start_server(bridge.handle_client, "127.0.0.1", 0)
        local_port = local.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
        writer.write(
            b"CONNECT example.test:443 HTTP/1.1\r\n"
            b"Host: example.test:443\r\n\r\n"
        )
        await writer.drain()
        response = await reader.read()
        writer.close()
        await writer.wait_closed()
        local.close()
        upstream.close()
        await local.wait_closed()
        await upstream.wait_closed()
        return response

    response = asyncio.run(exercise())

    assert response.startswith(b"HTTP/1.1 502 Bad Gateway")
    assert b"X-Residential-Proxy-Upstream-Status: 407" in response
    assert b"must-not-leak" not in response
