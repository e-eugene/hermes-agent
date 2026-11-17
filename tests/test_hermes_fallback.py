from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "runtime"))

from hermes_fallback import configure_llm7_fallback


def test_llm7_chain_uses_same_endpoint_and_environment_key() -> None:
    config = {"fallback_model": {"provider": "other", "model": "stale"}}
    configure_llm7_fallback(config, "openai-api", "https://api.llm7.io/v1/")
    assert config == {
        "fallback_providers": [
            {
                "provider": "custom",
                "model": model,
                "base_url": "https://api.llm7.io/v1",
                "key_env": "OPENAI_API_KEY",
            }
            for model in ("deepseek-v4-flash:0731", "claude-haiku-5-5")
        ]
    }


def test_non_llm7_keeps_unrelated_fallback_and_removes_our_stale_chain() -> None:
    other = {"fallback_providers": [{"provider": "openrouter", "model": "x"}]}
    configure_llm7_fallback(other, "openai-api", "https://example.com/v1")
    assert other == {"fallback_providers": [{"provider": "openrouter", "model": "x"}]}
    managed = {}
    configure_llm7_fallback(managed, "openai-api", "https://api.llm7.io/v1")
    configure_llm7_fallback(managed, "anthropic", "https://api.anthropic.com")
    assert managed == {}
