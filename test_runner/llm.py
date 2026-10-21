"""Thin wrapper around the Azure AI Foundry (OpenAI-compatible) endpoint."""

import os

from . import config


class LLMError(Exception):
    pass


def ask(prompt: str) -> str:
    """Send one prompt to the configured deployment and return the reply text."""
    api_key = os.environ.get(config.LLM_API_KEY_ENV) or config.LLM_API_KEY
    if not api_key:
        raise LLMError(f"Set the {config.LLM_API_KEY_ENV} environment variable to your Foundry API key.")
    try:
        from openai import OpenAI
    except ImportError:
        raise LLMError("The openai package is missing: pip install openai")

    client = OpenAI(base_url=config.LLM_ENDPOINT, api_key=api_key, timeout=config.LLM_TIMEOUT_S)
    from openai import BadRequestError

    extra = {"reasoning": {"effort": config.LLM_REASONING_EFFORT}} if config.LLM_REASONING_EFFORT else {}
    try:
        try:
            response = client.responses.create(model=config.LLM_DEPLOYMENT, input=prompt, **extra)
        except BadRequestError:
            if not extra:
                raise
            # This deployment does not take a reasoning setting - use its default.
            response = client.responses.create(model=config.LLM_DEPLOYMENT, input=prompt)
    except Exception as e:
        raise LLMError(f"Foundry call failed: {e}") from e
    return response.output_text
