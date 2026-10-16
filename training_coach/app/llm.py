"""Thin wrapper around the Anthropic SDK for the coach and analysis calls."""
from __future__ import annotations

from datetime import datetime

import anthropic

MODEL = "claude-opus-5-5"  # the key check and anything without its own entry below
# Opus where the reasoning is the product and it runs about weekly: the plan (hard constraints, validated) and the
# deep analysis. Sonnet for chat: interactive, plan edits are validated and retried. Haiku for the post-ride
# feedback: a few sentences from numbers we compute, after every ride, so it's the high-volume call.
MODELS = {"plan": MODEL, "analysis": MODEL, "chat": "claude-sonnet-5-5", "ride": "claude-haiku-5-5"}
# Haiku has no server-side refusal fallback
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
# $ per million tokens: input, output, cache read. For the usage estimate on Settings.
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
          "claude-haiku-5-5": (0.10, 0.50, 0.01)}


class LLMError(Exception):
    pass


async def run(api_key: str, system: str, messages: list[dict], *, schema: dict | None = None,
              effort: str = "high", max_tokens: int = 32000, db=None, purpose: str = ""):
    """Streams one request on the purpose's model and returns the final message. With a db, the call's token
    usage is logged.

    Server-side refusal fallback is on where the model has it: if Opus or Sonnet declines, the API reroutes to
    its recommended fallback model inside the same call.
    """
    if not api_key:
        raise LLMError("No Anthropic API key configured")
    output_config: dict = {"effort": effort}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    model = MODELS.get(purpose, MODEL)
    fallback = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    client = anthropic.AsyncAnthropic(api_key=api_key)
    try:
        async with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config=output_config,
            **fallback,
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

    if db is not None:
        db.log_llm(datetime.now().isoformat(timespec="seconds"), purpose, msg)
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


def cost(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0) -> float:
    """Estimated $ for logged usage; unknown models priced as Opus."""
    i, o, c = PRICES.get(model, PRICES[MODEL])
    return (input_tokens * i + output_tokens * o + cache_read * c) / 1e6


def text_of(msg) -> str:
    return "".join(b.text for b in msg.content if b.type == "text")
