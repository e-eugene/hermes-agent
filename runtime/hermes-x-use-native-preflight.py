#!/opt/hermes/.venv/bin/python
"""Fail startup unless installed Hermes discovers the exact safe x-use MCP."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml


X_USE_COMMIT = "e57e215e45b3e68cbd8cd7c46799cd932c234eac"
X_USE_VERSION = "2.4.1"
ALLOWED = {
    "list_accounts",
    "get_account",
    "get_account_health",
    "get_metrics",
    "search_tweets",
    "search_profile",
    "get_tweet",
    "prepare_reply",
    "like_tweet",
    "post_tweet",
    "reply_to_tweet",
    "list_drafts",
    "get_draft",
    "reject_draft",
}
PREFIX = "mcp__x_use__"
MARKER = Path(
    os.environ.get(
        "HERMES_X_USE_PREFLIGHT_MARKER",
        "/tmp/hermes-x-use/native-mcp-ready.json",
    )
)


def write_marker() -> None:
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(MARKER.parent, 0o700)
    temporary = MARKER.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "commit": X_USE_COMMIT,
                "tool_count": len(ALLOWED),
                "version": X_USE_VERSION,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, MARKER)


def main() -> None:
    os.umask(0o077)
    MARKER.unlink(missing_ok=True)
    if os.environ.get("HERMES_BROWSER_NETWORK_MODE") != "assigned_proxy":
        raise RuntimeError("x-use native preflight requires assigned proxy mode")
    config_path = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    servers = config.get("mcp_servers")
    source = servers.get("x_use") if isinstance(servers, dict) else None
    if not isinstance(source, dict):
        raise RuntimeError("Hermes x_use MCP config is missing")
    if source.get("command") != "/usr/local/bin/hermes-x-use-mcp":
        raise RuntimeError("Hermes x_use MCP command is invalid")
    tool_filter = source.get("tools")
    if not isinstance(tool_filter, dict) or set(tool_filter.get("include") or []) != ALLOWED:
        raise RuntimeError("Hermes x_use MCP tool filter is invalid")
    if tool_filter.get("prompts") is not False or tool_filter.get("resources") is not False:
        raise RuntimeError("Hermes x_use MCP utilities must be disabled")

    sys.path.insert(0, "/opt/hermes")
    from tools.mcp_tool import register_mcp_servers, shutdown_mcp_servers
    from tools.registry import registry

    expected = {PREFIX + name for name in ALLOWED}
    try:
        names = set(register_mcp_servers({"x_use": source}))
        if names != expected:
            raise RuntimeError("Hermes x_use MCP discovery surface is invalid")
        schemas = {name: registry.get_schema(name) for name in names}
        if any(not isinstance(schema, dict) for schema in schemas.values()):
            raise RuntimeError("Hermes x_use MCP schema registration is incomplete")
        post = schemas[PREFIX + "post_tweet"]["parameters"]["properties"]
        reply = schemas[PREFIX + "reply_to_tweet"]["parameters"]["properties"]
        like = schemas[PREFIX + "like_tweet"]["parameters"]["properties"]
        if set(post) != {"account", "text"}:
            raise RuntimeError("Hermes post_tweet schema is not draft-only")
        if set(reply) != {"account", "tweet_url", "text"}:
            raise RuntimeError("Hermes reply_to_tweet schema is not draft-only")
        if set(like) != {"account", "tweet_url"}:
            raise RuntimeError("Hermes like_tweet schema is invalid")
    finally:
        shutdown_mcp_servers()
    write_marker()
    print(
        json.dumps(
            {
                "status": "ok",
                "native_hermes_discovery": True,
                "tool_count": len(expected),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
