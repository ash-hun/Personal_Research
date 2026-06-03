"""End-to-end demo of the Hermes conversation-loop mirror.

Run with:  python3 demo.py   (no external deps, no API keys)

Each scenario plugs a scripted FAKE model into the ``ModelClient`` seam and
drives ``run_conversation`` end-to-end, printing each step so the *restored*
control flow is observable:

  1. tool calls → final answer            (happy path)
  2. runaway tool loop → budget terminal   (iteration budget + summary)
  3. unknown tool name → self-correction   (3-strike validation)
  4. finish_reason="length" → continuation (truncation text-continuation)
  5. tool guardrail HALT                    (post-execution halt branch)
  6. empty after tools → post-tool nudge    (empty-response recovery ladder)
  7. invalid JSON args → silent retries     (3 silent re-calls then recover)
  8. invalid API response → fallback        (retry + provider fallback hook)
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from conversation_loop import (
    AIAgent,
    AssistantMessage,
    GuardrailDecision,
    ToolCall,
    run_conversation,
)


# ════════════════════════════════════════════════════════════════════════
#  FAKE tools — dict args -> string result.
# ════════════════════════════════════════════════════════════════════════


def calculator(args: Dict[str, Any]) -> str:
    a = float(args.get("a", 0))
    b = float(args.get("b", 0))
    return json.dumps({"sum": a + b})


def clock(args: Dict[str, Any]) -> str:
    return json.dumps({"now": "2026-06-03T09:00:00Z"})


def delete_everything(args: Dict[str, Any]) -> str:  # pragma: no cover - guarded
    return "(should never run — guardrail halts it)"


TOOLS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "calculator": calculator,
    "clock": clock,
    "delete_everything": delete_everything,
}


# ════════════════════════════════════════════════════════════════════════
#  FAKE model — a scripted ModelClient. Returns the next scripted item per
#  call; a ``None`` item simulates an invalid/empty API response.
# ════════════════════════════════════════════════════════════════════════


class ScriptedModel:
    def __init__(self, script: List[Optional[AssistantMessage]]) -> None:
        self._script = script
        self._i = 0

    def __call__(
        self,
        messages: List[Dict[str, Any]],
        *,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[AssistantMessage]:
        am = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if am is not None and am.content and stream_callback:
            for chunk in am.content.split(" "):
                stream_callback(chunk + " ")
        return am


def banner(title: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {title}")
    print("═" * 70)


def show(result) -> None:
    print(f"\n>>> final_response : {result.final_response!r}")
    print(f">>> completed      : {result.completed}  partial={result.partial}  failed={result.failed}")
    print(f">>> api_calls      : {result.api_calls}")
    print(f">>> exit_reason    : {result.turn_exit_reason}")


# ── 1. tool calls then final answer ──────────────────────────────────────
def scenario_tool_loop() -> None:
    banner("1: tool calls then final answer")
    script = [
        AssistantMessage(content="Let me check the time.",
                         tool_calls=[ToolCall(name="clock", arguments="{}")],
                         finish_reason="tool_calls"),
        AssistantMessage(tool_calls=[ToolCall(name="calculator", arguments='{"a": 19, "b": 23}')],
                         finish_reason="tool_calls"),
        AssistantMessage(content="It is 09:00 UTC and 19 + 23 = 42.", finish_reason="stop"),
    ]
    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=90)
    result = run_conversation(agent, "What time is it, and what's 19 + 23?")
    show(result)
    assert result.completed and result.api_calls == 3, result.as_dict()


# ── 2. runaway tool loop hits the iteration budget ───────────────────────
def scenario_budget_exhausted() -> None:
    banner("2: runaway tool loop hits the iteration budget")
    looping = AssistantMessage(content="checking again...",
                               tool_calls=[ToolCall(name="clock", arguments="{}")],
                               finish_reason="tool_calls")
    summary = AssistantMessage(content="Ran out of iterations. Final: ~09:00 UTC.", finish_reason="stop")
    agent = AIAgent(model=ScriptedModel([looping, looping, looping, summary]),
                    tools=TOOLS, max_iterations=3)
    result = run_conversation(agent, "Keep telling me the time forever.")
    show(result)
    assert "Ran out of iterations" in (result.final_response or ""), result.as_dict()
    assert result.turn_exit_reason.startswith("max_iterations_reached"), result.as_dict()


# ── 3. unknown tool name → error fed back, then recovery ──────────────────
def scenario_invalid_tool() -> None:
    banner("3: unknown tool name → error fed back, then recovery")
    script = [
        AssistantMessage(tool_calls=[ToolCall(name="weather", arguments="{}")], finish_reason="tool_calls"),
        AssistantMessage(content="Sorry, I don't have a weather tool. The time is 09:00 UTC.",
                         finish_reason="stop"),
    ]
    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=90)
    result = run_conversation(agent, "What's the weather?")
    show(result)
    assert result.completed, result.as_dict()


# ── 4. finish_reason="length" → text continuation ────────────────────────
def scenario_length_continuation() -> None:
    banner("4: truncated output (finish_reason='length') → continuation")
    script = [
        AssistantMessage(content="The answer to your question is", finish_reason="length"),
        AssistantMessage(content=" forty-two.", finish_reason="stop"),
    ]
    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=90)
    result = run_conversation(agent, "Tell me the answer.")
    show(result)
    # The continuation re-call produces the rest; loop completes normally.
    assert result.completed and "forty-two" in (result.final_response or ""), result.as_dict()
    assert result.api_calls == 2, result.as_dict()


# ── 5. tool guardrail HALT ────────────────────────────────────────────────
def scenario_guardrail_halt() -> None:
    banner("5: tool guardrail HALT after execution attempt")

    def guardrail(tc: ToolCall):
        if tc.name == "delete_everything":
            return GuardrailDecision(tool_name=tc.name, code="DESTRUCTIVE_OP",
                                     detail="Refusing to delete without confirmation.")
        return None

    script = [
        AssistantMessage(tool_calls=[ToolCall(name="delete_everything", arguments="{}")],
                         finish_reason="tool_calls"),
        AssistantMessage(content="(should not be reached)", finish_reason="stop"),
    ]
    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=90, guardrail=guardrail)
    result = run_conversation(agent, "Delete everything.")
    show(result)
    assert result.turn_exit_reason == "guardrail_halt", result.as_dict()
    assert "guardrail" in (result.final_response or "").lower(), result.as_dict()


# ── 6. empty after tools → post-tool nudge → recovery ─────────────────────
def scenario_empty_post_tool_nudge() -> None:
    banner("6: empty response after tools → post-tool nudge → recovery")
    script = [
        AssistantMessage(tool_calls=[ToolCall(name="clock", arguments="{}")], finish_reason="tool_calls"),
        AssistantMessage(content="", finish_reason="stop"),          # empty after tool result
        AssistantMessage(content="It is 09:00 UTC.", finish_reason="stop"),  # recovers after nudge
    ]
    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=90)
    result = run_conversation(agent, "What time is it?")
    show(result)
    assert result.completed and "09:00" in (result.final_response or ""), result.as_dict()
    assert result.api_calls == 3, result.as_dict()


# ── 7. invalid JSON args → 3 silent retries → success ─────────────────────
def scenario_invalid_json_retry() -> None:
    banner("7: invalid JSON arguments → silent API retries → success")
    # Note: args END with '}' (not truncated) but are invalid JSON, so the
    # loop re-calls the API silently (does NOT append messages) up to 3 times.
    bad = AssistantMessage(
        tool_calls=[ToolCall(name="calculator", arguments='{"a": 1 "b": 2}')],  # missing comma
        finish_reason="tool_calls",
    )
    good = AssistantMessage(
        tool_calls=[ToolCall(name="calculator", arguments='{"a": 1, "b": 2}')],
        finish_reason="tool_calls",
    )
    final = AssistantMessage(content="1 + 2 = 3.", finish_reason="stop")
    agent = AIAgent(model=ScriptedModel([bad, good, final]), tools=TOOLS, max_iterations=90)
    result = run_conversation(agent, "Add 1 and 2.")
    show(result)
    assert result.completed and "= 3" in (result.final_response or ""), result.as_dict()


# ── 8. invalid API response (None) → retry + provider fallback ────────────
def scenario_invalid_response_fallback() -> None:
    banner("8: invalid API response (None) → retry + provider-fallback hook")
    activated = {"count": 0}

    def fallback() -> bool:
        activated["count"] += 1
        return True  # pretend a fallback provider was activated

    script = [
        None,  # first call: invalid/empty response → triggers fallback + retry
        AssistantMessage(content="Recovered via fallback provider.", finish_reason="stop"),
    ]
    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=90,
                    try_activate_fallback=fallback)
    result = run_conversation(agent, "Hello?")
    show(result)
    assert result.completed and "fallback" in (result.final_response or "").lower(), result.as_dict()
    assert activated["count"] >= 1, "fallback hook should have been invoked"


if __name__ == "__main__":
    scenario_tool_loop()
    scenario_budget_exhausted()
    scenario_invalid_tool()
    scenario_length_continuation()
    scenario_guardrail_halt()
    scenario_empty_post_tool_nudge()
    scenario_invalid_json_retry()
    scenario_invalid_response_fallback()
    banner("All scenarios passed ✅")
