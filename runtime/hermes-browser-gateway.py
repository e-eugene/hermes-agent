#!/opt/hermes/.venv/bin/python
"""Authenticated private WebSocket-to-loopback-VNC bridge.

The VNC server itself is bound to loopback and has no password because it is
never exposed directly.  This small gateway is the only network listener for
the remote-browser feature and requires the per-runtime bearer token.
"""

from __future__ import annotations

import asyncio
import os

from websockets.asyncio.server import serve


TOKEN = os.environ.get("API_SERVER_KEY", "")
PORT = int(os.environ.get("HERMES_BROWSER_GATEWAY_PORT", "6081"))
CONTROL = asyncio.Lock()

if not TOKEN:
    raise SystemExit("API_SERVER_KEY must be configured")


async def browser_connection(websocket: object) -> None:
    request = getattr(websocket, "request", None)
    headers = getattr(request, "headers", {})
    if headers.get("Authorization") != f"Bearer {TOKEN}":
        await websocket.close(code=1008, reason="unauthorized")
        return
    if CONTROL.locked():
        await websocket.close(code=1013, reason="browser already controlled")
        return

    async with CONTROL:
        reader, writer = await asyncio.open_connection("127.0.0.1", 5900)

        async def websocket_to_vnc() -> None:
            async for message in websocket:
                if not isinstance(message, bytes):
                    await websocket.close(code=1003, reason="binary frames only")
                    return
                writer.write(message)
                await writer.drain()

        async def vnc_to_websocket() -> None:
            while data := await reader.read(64 * 1024):
                await websocket.send(data)

        try:
            tasks = {
                asyncio.create_task(websocket_to_vnc()),
                asyncio.create_task(vnc_to_websocket()),
            }
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            writer.close()
            await writer.wait_closed()


async def main() -> None:
    async with serve(
        browser_connection,
        "::",
        PORT,
        max_size=8 * 1024 * 1024,
        compression=None,
        ping_interval=20,
        ping_timeout=20,
    ):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
