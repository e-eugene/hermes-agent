#!/opt/x-use/.venv/bin/python
"""Container-only stdio proof for the curated Hermes x-use MCP facade."""

from __future__ import annotations

import asyncio
import json
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ALLOWED = {
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


def text_payload(result) -> dict[str, object]:
    for block in result.content:
        if getattr(block, "type", None) == "text":
            value = json.loads(block.text)
            if isinstance(value, dict):
                return value
    raise RuntimeError("MCP tool returned no JSON object")


async def run() -> None:
    # Hermes intentionally gives native MCP subprocesses a filtered env. Keep
    # this smoke honest by passing only the declared, non-secret network keys.
    env = {
        key: os.environ[key]
        for key in (
            "HERMES_BROWSER_NETWORK_MODE",
            "HERMES_RESIDENTIAL_PROXY_PORT",
            "RESIDENTIAL_PROXY_URL",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "http_proxy",
            "https_proxy",
            "NO_PROXY",
            "no_proxy",
        )
        if key in os.environ
    }
    params = StdioServerParameters(
        command="/usr/local/bin/hermes-x-use-mcp",
        args=[],
        env=env,
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            if names != ALLOWED:
                raise RuntimeError(f"Unexpected MCP surface: {sorted(names)}")
            accounts = text_payload(await session.call_tool("list_accounts", {}))
            staged = text_payload(
                await session.call_tool(
                    "post_tweet",
                    {"account": "expected_user", "text": "local MCP smoke draft"},
                )
            )
            if accounts.get("ok") is not True or staged.get("ok") is not True:
                raise RuntimeError("Curated MCP smoke tool failed")
            if staged.get("payload") != {"text": "local MCP smoke draft"}:
                raise RuntimeError("Draft payload was not canonical")
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "server_version": initialized.serverInfo.version,
                        "tool_count": len(names),
                        "forbidden_approval_exposed": "approve_draft" in names,
                        "draft_only": staged.get("status") == "pending",
                    },
                    sort_keys=True,
                )
            )


if __name__ == "__main__":
    asyncio.run(run())
