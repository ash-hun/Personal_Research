"""demo.py — run the SAME normalized request through every fake provider adapter.

    python3 demo.py

No external deps, no API keys, no network. Demonstrates:
  1. provider-agnosticism — one ModelRequest, N providers
  2. tool-call format translation — each provider gets a different wire shape
  3. response normalization — every provider's reply comes back identical-shaped
"""

from __future__ import annotations

import json
from typing import Any

from provider_adapters import (
    Message,
    ModelRequest,
    ModelResponse,
    ToolSpec,
    get_model_capabilities,
    list_providers,
    select_adapter,
)


def _pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _hr(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def build_sample_request(model: str) -> ModelRequest:
    """One provider-agnostic request, reused for every adapter."""
    weather_tool = ToolSpec(
        name="get_weather",
        description="Get the current weather for a city.",
        parameters={
            "type": "object",
            "additionalProperties": False,  # gets stripped only for Gemini
            "properties": {
                "city": {"type": "string", "description": "City name, e.g. Seoul"},
            },
            "required": ["city"],
        },
    )
    messages = [
        Message(role="system", content="You are a concise weather assistant."),
        Message(role="user", content="What's the weather in Seoul right now?"),
    ]
    return ModelRequest(
        model=model,
        messages=messages,
        tools=[weather_tool],
        tool_choice="auto",
        temperature=0.2,
        max_tokens=512,
    )


def show_normalized(resp: ModelResponse) -> None:
    print("  finish_reason :", resp.finish_reason)
    print("  content       :", resp.content)
    if resp.tool_calls:
        for tc in resp.tool_calls:
            print(f"  tool_call     : {tc.name}({tc.parsed_arguments()})  [id={tc.id}]")
    print(
        "  usage         : prompt={p} completion={c} total={t}".format(
            p=resp.usage.prompt_tokens,
            c=resp.usage.completion_tokens,
            t=resp.usage.total_tokens,
        )
    )


def main() -> None:
    _hr("REGISTERED PROVIDERS  (mirror of `hermes model` / list_providers)")
    for adapter in list_providers():
        caps = get_model_capabilities(adapter.name, adapter.name)
        cap_note = f"tools={caps.supports_tools} vision={caps.supports_vision}" if caps else "n/a"
        print(f"  {adapter.name:10s} → {adapter.display_name:28s} [{cap_note}]")

    # Same logical request, addressed at three providers via `provider:model`.
    model_refs = [
        "openai:gpt-4o",
        "anthropic:claude-sonnet-4",
        "gemini:gemini-2.0-flash",
    ]

    normalized_results: dict[str, ModelResponse] = {}

    for ref in model_refs:
        adapter = select_adapter(ref)
        _, model = ref.split(":", 1)
        request = build_sample_request(model)

        _hr(f"PROVIDER: {adapter.display_name}   (model ref: {ref})")

        wire_request, normalized = adapter.complete(request)

        print("\n-- WIRE-FORMAT REQUEST (what this provider's API actually receives) --")
        print(_pretty(wire_request))

        print("\n-- NORMALIZED RESPONSE (what the agent loop sees — identical shape) --")
        show_normalized(normalized)

        normalized_results[ref] = normalized

    # Prove provider-agnosticism: every adapter produced the SAME normalized
    # tool call from the SAME request, despite three different wire formats.
    _hr("PROVIDER-AGNOSTICISM CHECK")
    tool_signatures = {
        ref: (
            resp.finish_reason,
            tuple((tc.name, tuple(sorted(tc.parsed_arguments().items()))) for tc in resp.tool_calls),
        )
        for ref, resp in normalized_results.items()
    }
    distinct = set(tool_signatures.values())
    for ref, sig in tool_signatures.items():
        print(f"  {ref:30s} → finish={sig[0]} tool_calls={sig[1]}")
    if len(distinct) == 1:
        print("\n  OK: all three providers normalized to ONE identical tool call.")
        print("      The agent loop never had to know which provider answered.")
    else:
        print("\n  WARNING: normalized outputs diverged across providers.")

    _hr("DONE")


if __name__ == "__main__":
    main()
