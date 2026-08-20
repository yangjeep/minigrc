"""One bounded OpenAI-compatible chat-completion call (issue #15).

Deliberately the only place in this codebase that makes an outbound HTTP
request to a BYOK provider. No retry loop, no streaming, no tool-calling,
no multi-turn state — see the design doc §2 for why this stays a single
bounded call rather than any kind of agent runtime.

Never raises: a timeout, connection error, non-2xx response, or malformed
JSON body all resolve to `ProviderCallResult(ok=False, ...)` so a broken
or misbehaving provider can never crash a caller (issue #15's "provider/
model failures cannot corrupt or partially mutate compliance state" and
"core miniGRC works normally ... when unavailable").
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.ai_provider import ResolvedAiProviderConfig

_MAX_OUTPUT_CHARS = 4000  # bound a runaway/malicious completion before it is ever stored/displayed


@dataclass(frozen=True)
class ProviderCallResult:
    ok: bool
    content: str
    error: str | None
    usage: dict | None = None


def call_chat_completion(
    config: ResolvedAiProviderConfig,
    *,
    system_prompt: str,
    user_prompt: str,
    transport: httpx.BaseTransport | None = None,
) -> ProviderCallResult:
    """`transport` is test-only (an `httpx.MockTransport` or a real
    `httpx.HTTPTransport` pointed at a fake local server) — production
    callers never pass it, so `httpx.Client` uses its normal networking."""
    if not config.usable:
        return ProviderCallResult(ok=False, content="", error="No usable AI provider is configured.")

    headers = {"Content-Type": "application/json"}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    try:
        with httpx.Client(timeout=config.timeout_seconds, transport=transport) as client:
            response = client.post(
                f"{config.base_url.rstrip('/')}/chat/completions", json=payload, headers=headers
            )
    except httpx.TimeoutException:
        return ProviderCallResult(ok=False, content="", error="Provider request timed out.")
    except httpx.HTTPError as exc:
        return ProviderCallResult(ok=False, content="", error=f"Provider request failed: {exc}")

    if response.status_code != 200:
        return ProviderCallResult(
            ok=False, content="", error=f"Provider returned HTTP {response.status_code}."
        )

    try:
        body = response.json()
        content = body["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ProviderCallResult(ok=False, content="", error="Provider returned a malformed response.")

    if not isinstance(content, str) or not content.strip():
        return ProviderCallResult(ok=False, content="", error="Provider returned an empty completion.")

    return ProviderCallResult(
        ok=True, content=content.strip()[:_MAX_OUTPUT_CHARS], error=None, usage=body.get("usage")
    )
