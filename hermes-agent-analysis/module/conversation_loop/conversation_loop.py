"""Self-contained, stdlib-only mirror of the Hermes Agent conversation loop.

This module distills the *mechanism* of the Hermes agent turn loop — how a
user message becomes a series of assistant turns, how the loop streams model
output, detects tool calls, executes them, feeds results back, handles stop
reasons, and enforces an iteration budget.

It is a *mirror*, not a literal port: the real code lives in a ~4751-LOC file
with provider adapters, prompt caching, streaming health checks, fallback
chains and dozens of recovery branches. Here we keep the load-bearing skeleton
and faithful names/data model so a researcher can read it top-to-bottom in one
sitting and then go spelunking in the real source.

Source map (reference repo: Nous Research `hermes-agent`):
  - agent/conversation_loop.py     -> run_conversation(): the main while loop
  - agent/iteration_budget.py      -> IterationBudget: consume/refund counter
  - agent/tool_executor.py         -> execute_tool_calls_sequential/concurrent
  - run_agent.py                   -> AIAgent wiring (_execute_tool_calls,
                                      _build_assistant_message, _handle_max_iterations)
  - agent/chat_completion_helpers.py -> build_assistant_message, handle_max_iterations

The LLM is abstracted behind a small `ModelClient` Protocol so the loop is the
*real* control flow but the model is mockable (see demo.py).
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


# ════════════════════════════════════════════════════════════════════════
#  Data model — the real I/O types the loop passes around.
#  In Hermes these are plain dicts in the OpenAI Chat Completions shape;
#  here we make them type-annotated dataclasses so the contract is explicit,
#  with .to_message()/.from_* helpers to round-trip to the dict form the
#  real loop uses (messages list of {"role": ..., "content": ..., ...}).
# ════════════════════════════════════════════════════════════════════════


@dataclass
class ToolCall:
    """One tool invocation requested by the model.

    Mirrors the OpenAI-style ``tool_call`` object Hermes reads as
    ``tc.id`` / ``tc.function.name`` / ``tc.function.arguments``
    (see agent/conversation_loop.py: the ``for tc in assistant_message.tool_calls``
    validation loops around line 3662).
    """

    name: str
    arguments: str = "{}"  # raw JSON string, exactly as the model emits it
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")

    def parsed_arguments(self) -> Dict[str, Any]:
        """Parse arguments JSON, tolerating the empty-string quirk.

        Mirrors agent/conversation_loop.py JSON-validation block (~3716): empty
        / whitespace args become ``{}``; dict/list args get re-serialized.
        """
        args = self.arguments
        if isinstance(args, (dict, list)):
            return args if isinstance(args, dict) else {}
        if not args or not str(args).strip():
            return {}
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


@dataclass
class AssistantMessage:
    """A single model response: free-text content and/or tool calls.

    Hermes reads ``assistant_message.content``, ``.tool_calls`` and a
    provider-mapped ``finish_reason`` (see ``map_finish_reason`` usage near
    conversation_loop.py:1561). A turn can carry BOTH content and tool calls.
    """

    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # "stop" | "tool_calls" | "length" | ...

    def to_message(self) -> Dict[str, Any]:
        """Render to the dict form appended to ``messages``.

        Mirrors agent/chat_completion_helpers.py: build_assistant_message().
        """
        msg: Dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        msg["finish_reason"] = self.finish_reason
        return msg


@dataclass
class ToolResult:
    """The output of executing one tool call.

    Becomes a ``{"role": "tool", ...}`` message that re-enters the loop as
    context for the next API call. Mirrors agent/tool_executor.py:
    make_tool_result_message().
    """

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False

    def to_message(self) -> Dict[str, Any]:
        return {
            "role": "tool",
            "name": self.name,
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }


class StopReason:
    """Why the turn loop terminated. Mirrors conversation_loop.py
    ``_turn_exit_reason`` string sentinels."""

    TEXT_RESPONSE = "text_response"          # model returned a final answer
    BUDGET_EXHAUSTED = "budget_exhausted"    # iteration budget hit
    MAX_ITERATIONS = "max_iterations"        # api_call_count hit max
    INVALID_TOOL = "invalid_tool_exhausted"  # too many bad tool names
    INTERRUPTED = "interrupted_by_user"      # user sent stop/new message
    EMPTY_EXHAUSTED = "empty_response_exhausted"
    ERROR = "error"


@dataclass
class ConversationResult:
    """Return value of ``run_conversation``.

    Mirrors the result dict assembled at conversation_loop.py:4647 — only the
    load-bearing keys are kept here.
    """

    final_response: Optional[str]
    messages: List[Dict[str, Any]]
    api_calls: int
    completed: bool
    turn_exit_reason: str
    partial: bool = False
    interrupted: bool = False
    error: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "final_response": self.final_response,
            "messages": self.messages,
            "api_calls": self.api_calls,
            "completed": self.completed,
            "turn_exit_reason": self.turn_exit_reason,
            "partial": self.partial,
            "interrupted": self.interrupted,
            "error": self.error,
        }


# ════════════════════════════════════════════════════════════════════════
#  IterationBudget — verbatim faithful mirror.
#  mirrors agent/iteration_budget.py: IterationBudget
# ════════════════════════════════════════════════════════════════════════


class IterationBudget:
    """Thread-safe iteration counter for an agent.

    mirrors agent/iteration_budget.py: IterationBudget

    Each agent (parent or subagent) gets its own budget. The parent's cap is
    ``max_iterations`` (default 90); each subagent gets an independent cap
    (default 50). ``execute_code`` (programmatic) turns are refunded so they
    don't eat the budget.
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        """Try to consume one iteration. Returns True if allowed."""
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
        """Give back one iteration (e.g. for execute_code turns)."""
        with self._lock:
            if self._used > 0:
                self._used -= 1

    @property
    def used(self) -> int:
        with self._lock:
            return self._used

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self.max_total - self._used)


# ════════════════════════════════════════════════════════════════════════
#  Pluggable model + tool interfaces.
#  In Hermes the "model" is an OpenAI/Anthropic/etc. client behind
#  ``agent._interruptible_streaming_api_call`` (conversation_loop.py:1304);
#  here it is a single callable Protocol so the loop is real but mockable.
# ════════════════════════════════════════════════════════════════════════


@runtime_checkable
class ModelClient(Protocol):
    """The mockable LLM seam.

    Given the running message history, return one AssistantMessage. A real
    implementation calls a provider API; the demo plugs in a scripted fake.

    Mirrors the role of ``agent._interruptible_streaming_api_call`` +
    response parsing in conversation_loop.py — collapsed to one call here.
    """

    def __call__(
        self,
        messages: List[Dict[str, Any]],
        *,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> AssistantMessage: ...


# A tool is just a callable: dict args -> string result.
Tool = Callable[[Dict[str, Any]], str]


# ════════════════════════════════════════════════════════════════════════
#  The agent: holds the wiring the loop reads off ``agent.*``.
#  In Hermes this is the giant ``AIAgent`` (run_agent.py). We keep only the
#  attributes/methods the conversation loop actually touches.
# ════════════════════════════════════════════════════════════════════════


class AIAgent:
    """Minimal stand-in for Hermes' ``AIAgent`` (run_agent.py).

    Carries exactly the fields the conversation loop reads: the model client,
    the tool registry, valid tool names, the iteration cap, the budget, and a
    couple of per-turn flags (interrupt, exit reason).
    """

    def __init__(
        self,
        model: ModelClient,
        tools: Dict[str, Tool],
        *,
        max_iterations: int = 90,
        verbose: bool = True,
    ) -> None:
        self.model = model
        self.tools = tools
        self.valid_tool_names = set(tools.keys())
        self.max_iterations = max_iterations
        self.verbose = verbose

        # Per-turn mutable state (reset at the top of run_conversation, mirroring
        # the big reset block at conversation_loop.py:439-482).
        self.iteration_budget = IterationBudget(max_iterations)
        self._interrupt_requested = False
        self._invalid_tool_retries = 0
        self._budget_grace_call = False
        self.session_id = uuid.uuid4().hex[:8]

    # ── verbose printing ─────────────────────────────────────────────
    def _vprint(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    # ── build_assistant_message forwarder ────────────────────────────
    # mirrors run_agent.py: AIAgent._build_assistant_message ->
    # agent/chat_completion_helpers.py: build_assistant_message
    def _build_assistant_message(self, am: AssistantMessage) -> Dict[str, Any]:
        return am.to_message()

    # ── tool name repair / validation ────────────────────────────────
    def _repair_tool_call(self, name: str) -> Optional[str]:
        """Best-effort fix for a hallucinated tool name (case-only here).

        mirrors conversation_loop.py: agent._repair_tool_call usage (~3664).
        The real version does fuzzy matching against the registry.
        """
        for valid in self.valid_tool_names:
            if valid.lower() == name.lower():
                return valid
        return None

    # ── the tool-execution dispatch ──────────────────────────────────
    # mirrors run_agent.py: AIAgent._execute_tool_calls ->
    # agent/tool_executor.py: execute_tool_calls_sequential
    def _execute_tool_calls(
        self, am: AssistantMessage, messages: List[Dict[str, Any]]
    ) -> None:
        """Run each tool call and append a ``role:"tool"`` result message.

        Hermes parallelizes independent read-only batches
        (execute_tool_calls_concurrent) and runs the rest sequentially. We
        mirror only the sequential path — the semantics that matter for the
        loop (one tool result per call, appended in order) are identical.
        """
        for tc in am.tool_calls:
            # SAFETY: honor an interrupt mid-batch — skip remaining calls.
            # mirrors tool_executor.py: execute_tool_calls_sequential (~548)
            if self._interrupt_requested:
                messages.append(
                    ToolResult(
                        tool_call_id=tc.id,
                        name=tc.name,
                        content=f"[Tool execution cancelled — {tc.name} skipped due to user interrupt]",
                        is_error=True,
                    ).to_message()
                )
                continue

            fn = self.tools.get(tc.name)
            args = tc.parsed_arguments()
            self._vprint(f"  ┊ 🔧 {tc.name}({json.dumps(args)})")
            if fn is None:
                result = ToolResult(
                    tc.id, tc.name, f"Error: tool '{tc.name}' not found", is_error=True
                )
            else:
                try:
                    output = fn(args)
                    result = ToolResult(tc.id, tc.name, str(output))
                except Exception as exc:  # mirrors _invoke_tool try/except
                    result = ToolResult(
                        tc.id,
                        tc.name,
                        f"Error executing tool '{tc.name}': {exc}",
                        is_error=True,
                    )
            self._vprint(f"  ┊ ➡  {result.content[:120]}")
            messages.append(result.to_message())

    # ── max-iterations terminal ──────────────────────────────────────
    # mirrors run_agent.py: _handle_max_iterations ->
    # agent/chat_completion_helpers.py: handle_max_iterations
    def _handle_max_iterations(self, messages: List[Dict[str, Any]]) -> str:
        """Ask the model for a tools-free summary after the budget is spent.

        The real version strips tools, appends a "you've hit the limit, just
        summarize" user message, and makes one final no-tools API call.
        """
        summary_request = (
            "You've reached the maximum number of tool-calling iterations allowed. "
            "Please provide a final response summarizing what you've found and "
            "accomplished so far, without calling any more tools."
        )
        messages.append({"role": "user", "content": summary_request})
        am = self.model(messages)
        # Force a no-tools final answer (real code drops the tool schema).
        text = (am.content or "").strip() or "(no summary produced)"
        messages.append({"role": "assistant", "content": text})
        return text


# ════════════════════════════════════════════════════════════════════════
#  run_conversation — THE LOOP.
#  mirrors agent/conversation_loop.py: run_conversation()
# ════════════════════════════════════════════════════════════════════════


def run_conversation(
    agent: AIAgent,
    user_message: str,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    stream_callback: Optional[Callable[[str], None]] = None,
) -> ConversationResult:
    """Run a complete conversation with tool calling until completion.

    mirrors agent/conversation_loop.py: run_conversation()

    Control flow (the load-bearing skeleton of the real ~4751-LOC function):

      1. Reset per-turn state + fresh IterationBudget        (real: ~439-482)
      2. Seed ``messages`` with history + the user message   (real: ~496-563)
      3. while api_call_count < max and budget.remaining > 0: (real: 796)
           a. check interrupt -> break                        (real: 801)
           b. consume one budget unit (or grace call)         (real: 815-821)
           c. call the model (streams text via callback)      (real: 1304)
           d. if response has tool_calls:
                - validate names; repair/retry on hallucination(real: 3662-3709)
                - validate JSON args                          (real: 3716-3801)
                - append assistant msg, execute tools,
                  append tool results, ``continue`` the loop  (real: 3869-3884)
           e. else: it's the final text answer -> append + break (real: 4293-4317)
      4. if the loop fell through (budget/iters exhausted):
           ask the model to summarize without tools          (real: 4393)
      5. assemble + return the result dict                   (real: 4647)
    """
    # ── 1. per-turn reset (mirrors conversation_loop.py:439-482) ──────
    agent._interrupt_requested = False
    agent._invalid_tool_retries = 0
    agent._budget_grace_call = False
    agent.iteration_budget = IterationBudget(agent.max_iterations)

    # ── 2. seed messages (mirrors conversation_loop.py:496 + 563) ─────
    messages: List[Dict[str, Any]] = list(conversation_history) if conversation_history else []
    messages.append({"role": "user", "content": user_message})

    agent._vprint(
        f"▶ conversation turn (session={agent.session_id}) "
        f"max_iters={agent.max_iterations}"
    )

    api_call_count = 0
    final_response: Optional[str] = None
    exit_reason = StopReason.ERROR
    completed = False
    partial = False
    interrupted = False
    error: Optional[str] = None

    # ── 3. the while loop (mirrors conversation_loop.py:796) ──────────
    while (
        api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0
    ) or agent._budget_grace_call:

        # 3a. interrupt check (mirrors conversation_loop.py:801)
        if agent._interrupt_requested:
            interrupted = True
            exit_reason = StopReason.INTERRUPTED
            agent._vprint("⚡ Breaking out of tool loop due to interrupt...")
            break

        api_call_count += 1

        # 3b. consume one budget unit, honoring a one-shot grace call
        #     (mirrors conversation_loop.py:815-821)
        if agent._budget_grace_call:
            agent._budget_grace_call = False
        elif not agent.iteration_budget.consume():
            exit_reason = StopReason.BUDGET_EXHAUSTED
            agent._vprint(
                f"⚠️  Iteration budget exhausted "
                f"({agent.iteration_budget.used}/{agent.iteration_budget.max_total})"
            )
            break

        agent._vprint(f"\n🔄 API call #{api_call_count}/{agent.max_iterations}")

        # 3c. call the model (mirrors the streaming API call ~1304)
        try:
            am = agent.model(messages, stream_callback=stream_callback)
        except Exception as exc:
            error = f"model call failed: {exc}"
            exit_reason = StopReason.ERROR
            final_response = f"I encountered an error: {exc}"
            messages.append({"role": "assistant", "content": final_response})
            break

        # 3d. tool calls present? (mirrors conversation_loop.py:3652)
        if am.tool_calls:
            agent._vprint(f"🔧 Processing {len(am.tool_calls)} tool call(s)...")

            # validate / repair tool names (mirrors ~3662-3709)
            for tc in am.tool_calls:
                if tc.name not in agent.valid_tool_names:
                    repaired = agent._repair_tool_call(tc.name)
                    if repaired:
                        agent._vprint(f"🔧 Auto-repaired tool name: '{tc.name}' -> '{repaired}'")
                        tc.name = repaired

            invalid = [tc.name for tc in am.tool_calls if tc.name not in agent.valid_tool_names]
            if invalid:
                agent._invalid_tool_retries += 1
                available = ", ".join(sorted(agent.valid_tool_names))
                agent._vprint(
                    f"⚠️  Unknown tool '{invalid[0]}' "
                    f"({agent._invalid_tool_retries}/3) — sending error for self-correction"
                )
                # Too many bad turns -> stop as partial (mirrors ~3682-3694)
                if agent._invalid_tool_retries >= 3:
                    exit_reason = StopReason.INVALID_TOOL
                    partial = True
                    error = f"Model generated invalid tool call: {invalid[0]}"
                    break
                # Feed an error tool result back so the model can fix itself
                # next turn, then ``continue`` (mirrors ~3696-3709).
                messages.append(agent._build_assistant_message(am))
                for tc in am.tool_calls:
                    content = (
                        f"Tool '{tc.name}' does not exist. Available tools: {available}"
                        if tc.name not in agent.valid_tool_names
                        else "Skipped: another tool call in this turn used an invalid name."
                    )
                    messages.append(ToolResult(tc.id, tc.name, content, is_error=True).to_message())
                continue
            agent._invalid_tool_retries = 0  # reset on success (mirrors 3711)

            # validate JSON arguments (mirrors ~3716-3801). On a truncated
            # ("length") tool call, refuse to execute and stop as partial.
            bad_json = []
            for tc in am.tool_calls:
                args = tc.arguments
                if isinstance(args, (dict, list)):
                    tc.arguments = json.dumps(args)
                    continue
                if not args or not str(args).strip():
                    tc.arguments = "{}"
                    continue
                try:
                    json.loads(args)
                except json.JSONDecodeError as exc:
                    bad_json.append((tc.name, str(exc)))
            if bad_json:
                truncated = am.finish_reason == "length"
                if truncated:
                    exit_reason = StopReason.TEXT_RESPONSE
                    partial = True
                    error = "Response truncated due to output length limit"
                    agent._vprint("⚠️  Truncated tool call arguments — refusing to execute.")
                    break
                # Inject recovery tool-error results so the model retries.
                agent._vprint(f"⚠️  Invalid JSON in tool args for '{bad_json[0][0]}' — injecting recovery")
                messages.append(agent._build_assistant_message(am))
                bad_names = {n for n, _ in bad_json}
                for tc in am.tool_calls:
                    if tc.name in bad_names:
                        err = next(e for n, e in bad_json if n == tc.name)
                        content = (
                            f"Error: Invalid JSON arguments. {err}. "
                            "For tools with no required parameters, use {}. Please retry."
                        )
                    else:
                        content = "Skipped: another tool call in this response had invalid JSON."
                    messages.append(ToolResult(tc.id, tc.name, content, is_error=True).to_message())
                continue

            # append assistant turn, execute tools, append results, loop again
            # (mirrors conversation_loop.py:3869-3884)
            messages.append(agent._build_assistant_message(am))
            agent._execute_tool_calls(am, messages)
            continue  # tool results re-enter the loop as context for the next call

        # 3e. no tool calls -> this is the final text answer
        #     (mirrors conversation_loop.py:4293-4317)
        final_response = (am.content or "").strip()
        messages.append(agent._build_assistant_message(am))
        if not final_response:
            final_response = "(empty)"
            exit_reason = StopReason.EMPTY_EXHAUSTED
        else:
            exit_reason = f"{StopReason.TEXT_RESPONSE}(finish_reason={am.finish_reason})"
            completed = True
        agent._vprint(f"🎉 Conversation completed after {api_call_count} API call(s)")
        break

    else:
        # 4. loop condition went false without break: iterations/budget spent.
        #    Ask the model for a tools-free summary (mirrors ~4393).
        if final_response is None:
            exit_reason = StopReason.MAX_ITERATIONS
            agent._vprint(
                f"⚠️  Reached max iterations ({api_call_count}/{agent.max_iterations}) "
                "— requesting summary..."
            )
            final_response = agent._handle_max_iterations(messages)
            completed = True

    # If we broke out via budget exhaustion (not the while-else), still give
    # the model a chance to summarize (mirrors the post-loop branch ~4385).
    if final_response is None and exit_reason == StopReason.BUDGET_EXHAUSTED:
        final_response = agent._handle_max_iterations(messages)
        completed = True

    # ── 5. assemble the result (mirrors conversation_loop.py:4647) ────
    return ConversationResult(
        final_response=final_response,
        messages=messages,
        api_calls=api_call_count,
        completed=completed,
        turn_exit_reason=exit_reason,
        partial=partial,
        interrupted=interrupted,
        error=error,
    )


__all__ = [
    "ToolCall",
    "AssistantMessage",
    "ToolResult",
    "StopReason",
    "ConversationResult",
    "IterationBudget",
    "ModelClient",
    "Tool",
    "AIAgent",
    "run_conversation",
]
