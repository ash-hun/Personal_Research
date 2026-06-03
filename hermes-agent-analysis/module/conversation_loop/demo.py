"""End-to-end demo of the Hermes conversation-loop mirror.

Run with:  python3 demo.py   (no external deps, no API keys)

We plug in:
  - a FAKE model that scripts a couple of tool-call turns then a final answer,
  - two FAKE tools (a calculator and a clock),
and run ``run_conversation`` end-to-end, printing each step so the mechanism
(model call -> tool detection -> tool execution -> result feedback -> stop) is
visible. A second scenario shows the iteration-budget terminal.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from conversation_loop import (
    AIAgent,
    AssistantMessage,
    ToolCall,
    run_conversation,
)


# ════════════════════════════════════════════════════════════════════════
#  FAKE tools — dict args -> string result.
# ════════════════════════════════════════════════════════════════════════


def calculator(args: Dict[str, Any]) -> str:
    """Add two numbers. (Trivially safe stand-in for a real tool.)"""
    a = float(args.get("a", 0))
    b = float(args.get("b", 0))
    return json.dumps({"sum": a + b})


def clock(args: Dict[str, Any]) -> str:
    """Return a fixed timestamp so the demo is deterministic."""
    return json.dumps({"now": "2026-06-03T09:00:00Z"})


TOOLS: Dict[str, Callable[[Dict[str, Any]], str]] = {
    "calculator": calculator,
    "clock": clock,
}


# ════════════════════════════════════════════════════════════════════════
#  FAKE model — a scripted ModelClient.
#  It walks through a fixed list of turns regardless of message content,
#  which is enough to exercise the full loop deterministically.
# ════════════════════════════════════════════════════════════════════════


class ScriptedModel:
    """Emits a pre-baked sequence of AssistantMessages, one per call.

    This is the mockable LLM seam (the ``ModelClient`` Protocol). A real
    implementation would call a provider API and parse the response.
    """

    def __init__(self, script: List[AssistantMessage]) -> None:
        self._script = script
        self._i = 0

    def __call__(
        self,
        messages: List[Dict[str, Any]],
        *,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> AssistantMessage:
        # In the real loop, the model sees the growing ``messages`` history
        # (including tool results). We just print how many it sees, then
        # return the next scripted turn.
        am = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        if am.content and stream_callback:
            # Mimic streaming: emit the text in chunks via the callback.
            for chunk in am.content.split(" "):
                stream_callback(chunk + " ")
        return am


def banner(title: str) -> None:
    print("\n" + "═" * 70)
    print(f"  {title}")
    print("═" * 70)


def dump_messages(messages: List[Dict[str, Any]]) -> None:
    print("\n── final message history ──")
    for m in messages:
        role = m["role"]
        if role == "assistant" and m.get("tool_calls"):
            names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
            print(f"  [assistant] (tool_calls: {names})  content={m.get('content')!r}")
        elif role == "tool":
            print(f"  [tool:{m['name']}] {m['content']}")
        else:
            print(f"  [{role}] {m['content']!r}")


# ════════════════════════════════════════════════════════════════════════
#  Scenario 1 — two tool rounds, then a final answer.
# ════════════════════════════════════════════════════════════════════════


def scenario_tool_loop() -> None:
    banner("Scenario 1: tool calls then final answer")

    script = [
        # Turn 1: model asks for the time.
        AssistantMessage(
            content="Let me check the time first.",
            tool_calls=[ToolCall(name="clock", arguments="{}")],
            finish_reason="tool_calls",
        ),
        # Turn 2: model does a calculation (note the empty-string args quirk
        # is handled too — here we pass real args).
        AssistantMessage(
            tool_calls=[ToolCall(name="calculator", arguments='{"a": 19, "b": 23}')],
            finish_reason="tool_calls",
        ),
        # Turn 3: no tool calls -> final answer. Loop terminates.
        AssistantMessage(
            content="It is 09:00 UTC and 19 + 23 = 42.",
            finish_reason="stop",
        ),
    ]
    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=90)
    result = run_conversation(agent, "What time is it, and what's 19 + 23?")

    print(f"\n>>> final_response : {result.final_response!r}")
    print(f">>> completed      : {result.completed}")
    print(f">>> api_calls      : {result.api_calls}")
    print(f">>> exit_reason    : {result.turn_exit_reason}")
    dump_messages(result.messages)
    assert result.completed and result.api_calls == 3, result.as_dict()


# ════════════════════════════════════════════════════════════════════════
#  Scenario 2 — model never stops calling tools -> budget terminal.
# ════════════════════════════════════════════════════════════════════════


def scenario_budget_exhausted() -> None:
    banner("Scenario 2: runaway tool loop hits the iteration budget")

    # The model ALWAYS asks for the clock again -> never produces a final
    # answer. The budget (max_iterations=3) forces termination, then
    # _handle_max_iterations asks for a tools-free summary.
    looping = AssistantMessage(
        content="checking again...",
        tool_calls=[ToolCall(name="clock", arguments="{}")],
        finish_reason="tool_calls",
    )
    summary = AssistantMessage(
        content="I kept checking the clock and ran out of iterations. Final: ~09:00 UTC.",
        finish_reason="stop",
    )
    # First N calls loop; the final (summary) call happens inside
    # _handle_max_iterations after the budget is spent.
    script = [looping, looping, looping, summary]

    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=3)
    result = run_conversation(agent, "Keep telling me the time forever.")

    print(f"\n>>> final_response : {result.final_response!r}")
    print(f">>> completed      : {result.completed}")
    print(f">>> api_calls      : {result.api_calls}")
    print(f">>> exit_reason    : {result.turn_exit_reason}")
    assert "ran out of iterations" in (result.final_response or ""), result.as_dict()


# ════════════════════════════════════════════════════════════════════════
#  Scenario 3 — hallucinated tool name -> self-correction error feedback.
# ════════════════════════════════════════════════════════════════════════


def scenario_invalid_tool() -> None:
    banner("Scenario 3: unknown tool name -> error fed back, then recovery")

    script = [
        # Turn 1: model calls a tool that does not exist.
        AssistantMessage(
            tool_calls=[ToolCall(name="weather", arguments="{}")],
            finish_reason="tool_calls",
        ),
        # Turn 2: model corrects itself and answers.
        AssistantMessage(
            content="Sorry, I don't have a weather tool. The time is 09:00 UTC.",
            finish_reason="stop",
        ),
    ]
    agent = AIAgent(model=ScriptedModel(script), tools=TOOLS, max_iterations=90)
    result = run_conversation(agent, "What's the weather?")

    print(f"\n>>> final_response : {result.final_response!r}")
    print(f">>> exit_reason    : {result.turn_exit_reason}")
    dump_messages(result.messages)
    assert result.completed, result.as_dict()


if __name__ == "__main__":
    scenario_tool_loop()
    scenario_budget_exhausted()
    scenario_invalid_tool()
    banner("All scenarios passed ✅")
