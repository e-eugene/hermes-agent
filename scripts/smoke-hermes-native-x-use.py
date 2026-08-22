#!/opt/hermes/.venv/bin/python
"""Container proof that Hermes discovers only the curated x-use schemas."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml


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
PREFIX = "mcp__x_use__"


def native_config() -> dict[str, object]:
    proxy = "http://127.0.0.1:8899"
    return {
        "mcp_servers": {
            "x_use": {
                "command": "/usr/local/bin/hermes-x-use-mcp",
                "args": [],
                "env": {
                    "HERMES_BROWSER_NETWORK_MODE": "assigned_proxy",
                    "HERMES_RESIDENTIAL_PROXY_PORT": "8899",
                    "RESIDENTIAL_PROXY_URL": proxy,
                    "HTTP_PROXY": proxy,
                    "HTTPS_PROXY": proxy,
                    "http_proxy": proxy,
                    "https_proxy": proxy,
                    "NO_PROXY": "127.0.0.1,localhost,::1",
                    "no_proxy": "127.0.0.1,localhost,::1",
                },
                "connect_timeout": 90,
                "timeout": 150,
                "tools": {
                    "include": sorted(ALLOWED),
                    "prompts": False,
                    "resources": False,
                },
            }
        }
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--configure",
        action="store_true",
        help="write the same minimal native MCP config used by the runtime",
    )
    args = parser.parse_args()

    if args.configure:
        path = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "config.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".yaml.tmp")
        temporary.write_text(
            yaml.safe_dump(native_config(), sort_keys=False), encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)

    sys.path.insert(0, "/opt/hermes")
    from tools.mcp_tool import discover_mcp_tools, shutdown_mcp_servers
    from tools.registry import registry

    expected = {PREFIX + name for name in ALLOWED}
    try:
        names = set(discover_mcp_tools())
        if names != expected:
            raise RuntimeError(f"Unexpected Hermes MCP schemas: {sorted(names)}")
        schemas = {name: registry.get_schema(name) for name in names}
        if any(not isinstance(schema, dict) for schema in schemas.values()):
            raise RuntimeError("Hermes did not register every discovered schema")
        post_properties = set(
            schemas[PREFIX + "post_tweet"]["parameters"]["properties"]
        )
        reply_properties = set(
            schemas[PREFIX + "reply_to_tweet"]["parameters"]["properties"]
        )
        if post_properties != {"account", "text"}:
            raise RuntimeError("Hermes post_tweet schema widened")
        if reply_properties != {"account", "tweet_url", "text"}:
            raise RuntimeError("Hermes reply_to_tweet schema widened")
        print(
            json.dumps(
                {
                    "status": "ok",
                    "native_hermes_discovery": True,
                    "tool_count": len(names),
                    "draft_approval_exposed": PREFIX + "approve_draft" in names,
                    "utility_tools_exposed": any(
                        name.endswith(
                            (
                                "__list_resources",
                                "__read_resource",
                                "__list_prompts",
                                "__get_prompt",
                            )
                        )
                        for name in names
                    ),
                },
                sort_keys=True,
            )
        )
    finally:
        shutdown_mcp_servers()


if __name__ == "__main__":
    main()
