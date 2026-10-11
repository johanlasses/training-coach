"""Thin wrapper around the Anthropic SDK for the coach and analysis calls."""
from __future__ import annotations

import anthropic

MODEL = "claude-opus-5-5"


class LLMError(Exception):
    pass


async def run(api_key: str, system: str, messages: list[dict], *, schema: dict | None = None,
              effort: str = "high", max_tokens: int = 32000):
    """Streams one request and returns the final message.

    Server-side refusal fallback is on: if Opus declines, the API reroutes to its recommended
    fallback model inside the same call.
    """
    if not api_key:
        raise LLMError("No Anthropic API key configured")
    output_config: dict = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        async with client.beta.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config=output_config,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = await stream.get_final_message()
    except anthropic.RateLimitError as e:
        raise LLMError(f"Rate limited by Anthropic API: {e}") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"Could not reach Anthropic API: {e}") from e
    finally:
        await client.close()

    if msg.stop_reason == "refusal":
        raise LLMError("The model declined this request")
    if msg.stop_reason == "max_tokens":
        raise LLMError("Response hit max_tokens before finishing")
    return msg


async def check_key(api_key: str) -> str | None:
    """Validates a key with a free models lookup. Returns an error message, or None if it works."""
    client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)
    try:
        await client.models.retrieve(MODEL)
        return None
    except anthropic.AuthenticationError:
        return "invalid API key"
    except anthropic.PermissionDeniedError:
        return f"key has no access to {MODEL}"
    except anthropic.APIStatusError as e:
        return f"API error {e.status_code}"
    except anthropic.APIConnectionError:
        return "could not reach the Anthropic API"
    finally:
        await client.close()


def text_of(msg) -> str:
    return "".join(b.text for b in msg.content if b.type == "text")
