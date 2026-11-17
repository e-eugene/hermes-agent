"""Dashboard-managed model failover for the LLM7 endpoint."""

LLM7_BASE_URL = "https://api.llm7.io/v1"
LLM7_FALLBACK_MODELS = ("deepseek-v4-flash:0731", "claude-haiku-5-5")


def configure_llm7_fallback(config: dict, provider: str, base_url: str) -> None:
    """Use the same endpoint and credential for each fallback model.

    Configure failover at the model-request layer, preserving the conversation
    and avoiding resubmission of a completed X action.
    """
    chain = [
        {
            "provider": "custom",
            "model": model,
            "base_url": LLM7_BASE_URL,
            "key_env": "OPENAI_API_KEY",
        }
        for model in LLM7_FALLBACK_MODELS
    ]
    if provider == "openai-api" and base_url.rstrip("/") == LLM7_BASE_URL:
        config["fallback_providers"] = chain
        # Hermes merges the legacy field with the new list; do not allow an
        # unrelated legacy route to be used after the dashboard-managed chain.
        config.pop("fallback_model", None)
    elif config.get("fallback_providers") == chain:
        # Remove only the chain we own when this persistent volume is assigned
        # to a different provider.
        config.pop("fallback_providers")
