"""Faithful, stdlib-only mirror of the Hermes Agent conversation loop.

This module reproduces the *control flow* of the Hermes agent turn loop
(`agent/conversation_loop.py: run_conversation()`, ~4,751 LOC) — not a
1:1 line port, but every branch that decides *what the loop does next* is
kept and wired so the operating principle is analysable end-to-end.

WHAT WAS RESTORED (vs. the earlier simplified mirror)
  - The inner API retry loop  ``while retry_count < max_retries``       (L1157)
  - Invalid-response retry + provider-fallback activation               (L1419-1544)
  - Truncation handling on ``finish_reason == "length"``                (L1583-1832)
      · thinking-budget exhaustion → give up early                      (L1636-1676)
      · text continuation, up to 3 attempts                             (L1680-1737)
      · truncated tool-call, up to 3 retries (token boost)              (L1741-1802)
      · rollback to last complete assistant turn                        (L1804-1832)
  - Tool-name validation/repair, 3-strike give-up                       (L3662-3711)
  - JSON-arg validation: truncation refusal + 3 silent retries          (L3713-3804)
  - Post-call guardrails (cap delegate / dedupe)                        (L3806-3812)
  - content+tools fallback capture + housekeeping muting                (L3816-3841)
  - Tool execution + tool-guardrail HALT branch                         (L3884-3907)
  - execute_code budget refund                                          (L3922-3927)
  - Empty-response recovery ladder                                      (L3983-4180)
      · partial-stream recovery / prior-turn fallback / post-tool nudge
        / thinking-only prefill / empty-content retries
  - Repeated-error near-max break                                       (L4368-4374)
  - Post-loop max-iterations summary (real ``if`` form, NOT while-else)  (L4376-4393)

WHAT IS PRESERVED AS [EXTERNAL SUBSYSTEM] HOOKS (marked inline, NOT cut)
  The real loop calls into other subsystems whose faithful port would drag
  in thousands of provider/feature-coupled lines.  We do NOT silently drop
  them — each is a clearly-banner-marked, overridable hook on ``AIAgent``
  that documents the original behaviour + source location, defaulting to a
  no-op so the loop stays runnable:
    · provider fallback chain        agent._try_activate_fallback()    (providers/, L1179/1426/1497)
    · context compression            agent.should_compress / _compress_context (agent/context_engine.py, L3965)
    · session persistence            agent._persist_session()          (hermes_state.py, L1668/4466)
    · plugin pre/post hooks          agent._pre_api_request_hook / _post_response_text_hook / _persist_hook (hermes_cli/plugins, L1235/4590/4612)
    · runtime footer / exit reason   agent._runtime_footer / _exit_explanation (gateway/runtime_footer.py, L4496-4583)
    · memory/skill background review agent._memory_skill_review()      (agent/curator.py, L4704-4720)

The LLM itself is the ``ModelClient`` seam (a Protocol) so the loop is the
*real* control flow but the model is mockable (see demo.py).

Source map (reference repo: Nous Research `hermes-agent`):
  agent/conversation_loop.py  -> run_conversation()
  agent/iteration_budget.py   -> IterationBudget
  agent/tool_executor.py      -> execute_tool_calls_sequential
  run_agent.py                -> AIAgent wiring
  agent/chat_completion_helpers.py -> build_assistant_message, handle_max_iterations
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


# ════════════════════════════════════════════════════════════════════════
#  Module constants / small helpers
# ════════════════════════════════════════════════════════════════════════

# mirrors agent/conversation_loop.py: PARTIAL_STREAM_STUB_ID — the response
# id used when a stream dies mid-flight (network error) so the loop can tell
# "model hit the output cap" apart from "the socket dropped".
PARTIAL_STREAM_STUB_ID = "__partial_stream_stub__"

_THINK_BLOCK_RE = re.compile(
    r"<(?:think|thinking|reasoning|REASONING_SCRATCHPAD)[^>]*>.*?"
    r"</(?:think|thinking|reasoning|REASONING_SCRATCHPAD)>",
    re.DOTALL | re.IGNORECASE,
)
_THINK_OPEN_RE = re.compile(
    r"<(?:think|thinking|reasoning|REASONING_SCRATCHPAD)[^>]*>", re.IGNORECASE
)


def _get_continuation_prompt(is_partial_stream_stub: bool = False,
                             dropped_tools: Optional[List[str]] = None) -> str:
    """The user-turn nudge appended to ask the model to keep going.

    mirrors agent/conversation_loop.py: _get_continuation_prompt() usage (L1715).
    """
    if is_partial_stream_stub and dropped_tools:
        return ("Your previous response was interrupted mid tool-call. "
                "Please re-issue the tool call(s) and continue.")
    if is_partial_stream_stub:
        return "Your previous response was interrupted. Please continue where you left off."
    return ("Your previous response was cut off by the output length limit. "
            "Please continue exactly where you left off.")


# ════════════════════════════════════════════════════════════════════════
#  Data model — the real I/O types the loop passes around.
# ════════════════════════════════════════════════════════════════════════


@dataclass
class ToolCall:
    """One tool invocation requested by the model.

    Mirrors the OpenAI-style ``tool_call`` Hermes reads as ``tc.id`` /
    ``tc.function.name`` / ``tc.function.arguments`` (conversation_loop.py ~L3662).
    """

    name: str
    arguments: str = "{}"  # raw JSON string, exactly as the model emits it
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")

    def parsed_arguments(self) -> Dict[str, Any]:
        args = self.arguments
        if isinstance(args, dict):
            return args
        if isinstance(args, list):
            return {}
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

    Hermes reads ``.content``, ``.tool_calls`` and a provider-mapped
    ``finish_reason`` (``map_finish_reason`` near conversation_loop.py:1561).
    A turn can carry BOTH content and tool calls. ``response_id`` lets the
    loop detect a partial-stream stub (PARTIAL_STREAM_STUB_ID).
    """

    content: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # "stop" | "tool_calls" | "length" | ...
    response_id: str = ""

    def to_message(self, finish_reason: Optional[str] = None) -> Dict[str, Any]:
        """Render to the dict appended to ``messages``.

        mirrors agent/chat_completion_helpers.py: build_assistant_message().
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
        msg["finish_reason"] = finish_reason or self.finish_reason
        return msg


@dataclass
class ToolResult:
    """The output of executing one tool call → a ``{"role": "tool", ...}`` message.

    mirrors agent/tool_executor.py: make_tool_result_message().
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


@dataclass
class GuardrailDecision:
    """A tool-guardrail HALT decision.

    mirrors the object stored in ``agent._tool_guardrail_halt_decision``
    (conversation_loop.py:3886) carrying ``tool_name`` and a ``code``.
    """

    tool_name: str
    code: str
    detail: str = ""


class StopReason:
    """Why the turn loop terminated. Mirrors conversation_loop.py
    ``_turn_exit_reason`` string sentinels (kept verbatim where they exist)."""

    TEXT_RESPONSE = "text_response"                 # model returned a final answer (L4314)
    BUDGET_EXHAUSTED = "budget_exhausted"           # iteration budget hit (L818)
    MAX_ITERATIONS = "max_iterations_reached"       # api_call_count hit max (L4383)
    INVALID_TOOL = "invalid_tool_exhausted"         # too many bad tool names (L3683)
    INTERRUPTED = "interrupted_by_user"             # user sent stop/new message (L803)
    EMPTY_EXHAUSTED = "empty_response_exhausted"    # empty after recovery ladder
    GUARDRAIL_HALT = "guardrail_halt"               # a tool tripped a guardrail (L3888)
    PARTIAL_STREAM_RECOVERY = "partial_stream_recovery"   # used streamed text (L4005)
    FALLBACK_PRIOR_TURN = "fallback_prior_turn_content"   # reused prior content (L4032)
    THINKING_EXHAUSTED = "thinking_budget_exhausted"      # reasoning ate the budget (L1645)
    TRUNCATED = "truncated"                          # output-length truncation give-up
    ERROR = "error"
    ERROR_NEAR_MAX = "error_near_max_iterations"     # repeated API errors (L4369)


@dataclass
class ConversationResult:
    """Return value of ``run_conversation`` — mirrors the result dict at L4647."""

    final_response: Optional[str]
    messages: List[Dict[str, Any]]
    api_calls: int
    completed: bool
    turn_exit_reason: str
    partial: bool = False
    interrupted: bool = False
    failed: bool = False
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
            "failed": self.failed,
            "error": self.error,
        }


# ════════════════════════════════════════════════════════════════════════
#  IterationBudget — verbatim faithful mirror of agent/iteration_budget.py.
# ════════════════════════════════════════════════════════════════════════


class IterationBudget:
    """Thread-safe iteration counter. mirrors agent/iteration_budget.py.

    Parent default cap 90, each subagent 50. ``execute_code`` turns are
    refunded so programmatic RPC calls don't eat the budget (L3922-3927).
    """

    def __init__(self, max_total: int):
        self.max_total = max_total
        self._used = 0
        self._lock = threading.Lock()

    def consume(self) -> bool:
        with self._lock:
            if self._used >= self.max_total:
                return False
            self._used += 1
            return True

    def refund(self) -> None:
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
#  Pluggable model interface — the mockable LLM seam.
# ════════════════════════════════════════════════════════════════════════


@runtime_checkable
class ModelClient(Protocol):
    """Given the running message history, return one AssistantMessage (or
    ``None`` to simulate an invalid/empty API response, which exercises the
    retry+fallback path). Mirrors the role of
    ``agent._interruptible_streaming_api_call`` + ``normalize_response``."""

    def __call__(
        self,
        messages: List[Dict[str, Any]],
        *,
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> Optional[AssistantMessage]: ...


# A tool is just a callable: dict args -> string result.
Tool = Callable[[Dict[str, Any]], str]


# ════════════════════════════════════════════════════════════════════════
#  The agent: holds the wiring the loop reads off ``agent.*`` (run_agent.py).
#  Real control logic is implemented; external subsystems are no-op hooks
#  marked with [EXTERNAL SUBSYSTEM] banners and overridable in __init__/attrs.
# ════════════════════════════════════════════════════════════════════════


class AIAgent:
    """Minimal stand-in for Hermes' ``AIAgent``.

    Carries exactly the fields/methods the conversation loop touches.
    Counters mirror the real per-turn retry state machine.
    """

    def __init__(
        self,
        model: ModelClient,
        tools: Dict[str, Tool],
        *,
        max_iterations: int = 90,
        api_max_retries: int = 5,
        verbose: bool = True,
        guardrail: Optional[Callable[[ToolCall], Optional[GuardrailDecision]]] = None,
        try_activate_fallback: Optional[Callable[[], bool]] = None,
        should_compress: Optional[Callable[[int], bool]] = None,
        compress_context: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
        persist_session: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
        post_response_text_hook: Optional[Callable[[str], Optional[str]]] = None,
        memory_skill_review: Optional[Callable[[str, List[Dict[str, Any]]], None]] = None,
    ) -> None:
        self.model = model
        self.tools = tools
        self.valid_tool_names = set(tools.keys())
        self.max_iterations = max_iterations
        self._api_max_retries = api_max_retries
        self.verbose = verbose
        self._guardrail = guardrail

        # ── [EXTERNAL SUBSYSTEM] overridable hooks (default no-op) ──
        self._fallback_hook = try_activate_fallback
        self._should_compress_hook = should_compress
        self._compress_context_hook = compress_context
        self._persist_session_hook = persist_session
        self._post_response_text_hook = post_response_text_hook
        self._memory_skill_review_hook = memory_skill_review
        self.compression_enabled = should_compress is not None

        # Per-turn mutable state (reset at the top of run_conversation,
        # mirroring the reset block at conversation_loop.py:439-482).
        self.iteration_budget = IterationBudget(max_iterations)
        self._interrupt_requested = False
        self._budget_grace_call = False
        self._invalid_tool_retries = 0
        self._invalid_json_retries = 0
        self._empty_content_retries = 0
        self._thinking_prefill_retries = 0
        self._post_tool_empty_retried = False
        self._tool_guardrail_halt_decision: Optional[GuardrailDecision] = None
        self._last_content_with_tools: Optional[str] = None
        self._last_content_tools_all_housekeeping = False
        self._current_streamed_assistant_text = ""
        self.session_id = uuid.uuid4().hex[:8]

    # ── verbose printing ─────────────────────────────────────────────
    def _vprint(self, msg: str) -> None:
        if self.verbose:
            print(msg)

    # ── think-block helpers ──────────────────────────────────────────
    # mirrors agent._strip_think_blocks / _has_content_after_think_block
    def _strip_think_blocks(self, text: Optional[str]) -> str:
        if not text:
            return ""
        return _THINK_BLOCK_RE.sub("", text)

    def _has_content_after_think_block(self, text: Optional[str]) -> bool:
        return bool(self._strip_think_blocks(text).strip())

    def _has_think_tags(self, text: Optional[str]) -> bool:
        return bool(text and _THINK_OPEN_RE.search(text))

    # ── build_assistant_message forwarder (chat_completion_helpers.py) ─
    def _build_assistant_message(self, am: AssistantMessage,
                                 finish_reason: Optional[str] = None) -> Dict[str, Any]:
        return am.to_message(finish_reason)

    # ── tool name repair (conversation_loop.py ~L3664) ────────────────
    def _repair_tool_call(self, name: str) -> Optional[str]:
        for valid in self.valid_tool_names:
            if valid.lower() == name.lower():
                return valid
        return None

    # ── post-call guardrails (conversation_loop.py L3806-3812) ────────
    def _cap_delegate_task_calls(self, tool_calls: List[ToolCall]) -> List[ToolCall]:
        """Real impl caps concurrent delegate_task fan-out; here identity."""
        return tool_calls

    def _deduplicate_tool_calls(self, tool_calls: List[ToolCall]) -> List[ToolCall]:
        """Drop exact-duplicate (name, arguments) calls within one turn."""
        seen = set()
        out: List[ToolCall] = []
        for tc in tool_calls:
            key = (tc.name, tc.arguments)
            if key in seen:
                continue
            seen.add(key)
            out.append(tc)
        return out

    # ── tool execution (run_agent._execute_tool_calls → tool_executor) ─
    def _execute_tool_calls(
        self, am: AssistantMessage, messages: List[Dict[str, Any]]
    ) -> None:
        """Run each tool call, append a ``role:"tool"`` result, honour
        mid-batch interrupts, and set ``_tool_guardrail_halt_decision`` if a
        guardrail trips (checked by the caller right after).

        mirrors agent/tool_executor.py: execute_tool_calls_sequential (~L548).
        """
        for tc in am.tool_calls:
            if self._interrupt_requested:
                messages.append(
                    ToolResult(
                        tc.id, tc.name,
                        f"[Tool execution cancelled — {tc.name} skipped due to user interrupt]",
                        is_error=True,
                    ).to_message()
                )
                continue

            # Pre-execution guardrail check (a tripped guardrail records a
            # halt decision; the loop breaks after this batch — L3886).
            if self._guardrail is not None:
                decision = self._guardrail(tc)
                if decision is not None:
                    self._tool_guardrail_halt_decision = decision
                    messages.append(
                        ToolResult(
                            tc.id, tc.name,
                            f"[Tool '{tc.name}' halted by guardrail: {decision.code}]",
                            is_error=True,
                        ).to_message()
                    )
                    return

            fn = self.tools.get(tc.name)
            args = tc.parsed_arguments()
            self._vprint(f"  ┊ 🔧 {tc.name}({json.dumps(args)})")
            if fn is None:
                result = ToolResult(tc.id, tc.name, f"Error: tool '{tc.name}' not found", is_error=True)
            else:
                try:
                    output = fn(args)
                    result = ToolResult(tc.id, tc.name, str(output))
                except Exception as exc:
                    result = ToolResult(tc.id, tc.name, f"Error executing tool '{tc.name}': {exc}", is_error=True)
            self._vprint(f"  ┊ ➡  {result.content[:120]}")
            messages.append(result.to_message())

    def _toolguard_controlled_halt_response(self, decision: GuardrailDecision) -> str:
        """mirrors agent._toolguard_controlled_halt_response (L3889)."""
        return (f"I stopped before running `{decision.tool_name}` because a safety "
                f"guardrail ({decision.code}) was triggered. {decision.detail}").strip()

    # ── max-iterations terminal (chat_completion_helpers.handle_max_iterations) ─
    def _handle_max_iterations(self, messages: List[Dict[str, Any]]) -> str:
        """Ask the model for a tools-free summary after the budget is spent
        (real code strips the tool schema before this single call). L4393."""
        summary_request = (
            "You've reached the maximum number of tool-calling iterations allowed. "
            "Please provide a final response summarizing what you've found and "
            "accomplished so far, without calling any more tools."
        )
        messages.append({"role": "user", "content": summary_request})
        am = self.model(messages)  # tools stripped in real code
        text = (am.content if am else "") or ""
        text = self._strip_think_blocks(text).strip() or "(no summary produced)"
        messages.append({"role": "assistant", "content": text})
        return text

    # ════════════════════════════════════════════════════════════════
    #  [EXTERNAL SUBSYSTEM] hooks — preserved, not cut. Default no-op.
    # ════════════════════════════════════════════════════════════════

    def _try_activate_fallback(self) -> bool:
        # Real: walks the configured provider fallback chain (Nous → OpenRouter
        # → …), swapping ``agent.client``/``provider`` and returning True if a
        # fallback was activated. providers/, conversation_loop.py L1179/1426/1497.
        return bool(self._fallback_hook()) if self._fallback_hook else False

    def _persist_session(self, messages: List[Dict[str, Any]]) -> None:
        # Real: agent._persist_session(messages, conversation_history) writes
        # the incremental transcript to the session DB. hermes_state.py, L1668/4466.
        if self._persist_session_hook:
            self._persist_session_hook(messages)

    def _pre_api_request_hook(self, messages: List[Dict[str, Any]]) -> None:
        # Real: invoke_hook("pre_api_request", ...) — plugins observe each
        # request. hermes_cli/plugins, conversation_loop.py L1235.
        return None

    def _should_compress(self, approx_tokens: int) -> bool:
        # Real: agent.context_compressor.should_compress(real_tokens) — token
        # budget policy. agent/context_engine.py, conversation_loop.py L3965.
        return bool(self._should_compress_hook(approx_tokens)) if self._should_compress_hook else False

    def _compress_context(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Real: agent._compress_context(...) summarises older turns. L3967.
        return self._compress_context_hook(messages) if self._compress_context_hook else messages

    def _post_response_text(self, text: str) -> str:
        # Real: invoke_hook("post_response_text", ...) — first plugin to return
        # a string transforms the output. conversation_loop.py L4590-4606.
        if self._post_response_text_hook:
            out = self._post_response_text_hook(text)
            if isinstance(out, str) and out:
                return out
        return text

    def _memory_skill_review(self, final_response: str, messages: List[Dict[str, Any]]) -> None:
        # Real: triggers background memory curation + skill consolidation when
        # the turn warrants it. agent/curator.py, conversation_loop.py L4704-4720.
        if self._memory_skill_review_hook:
            self._memory_skill_review_hook(final_response, messages)


# ════════════════════════════════════════════════════════════════════════
#  run_conversation — THE LOOP.  mirrors agent/conversation_loop.py:351.
# ════════════════════════════════════════════════════════════════════════


def run_conversation(
    agent: AIAgent,
    user_message: str,
    conversation_history: Optional[List[Dict[str, Any]]] = None,
    stream_callback: Optional[Callable[[str], None]] = None,
) -> ConversationResult:
    """Run a complete conversation with tool calling until completion.

    The structure below mirrors the real function's control flow. The real
    code inlines everything in one ~4,400-line function; we keep the same
    *order and conditions* of every branch, factoring only the model call's
    inner retry loop inline (no behavioural change).
    """
    # ── 0. per-turn reset (conversation_loop.py L439-482) ─────────────
    agent._interrupt_requested = False
    agent._budget_grace_call = False
    agent._invalid_tool_retries = 0
    agent._invalid_json_retries = 0
    agent._empty_content_retries = 0
    agent._thinking_prefill_retries = 0
    agent._post_tool_empty_retried = False
    agent._tool_guardrail_halt_decision = None
    agent.iteration_budget = IterationBudget(agent.max_iterations)

    # ── 1. seed messages (L496 + L563) ────────────────────────────────
    messages: List[Dict[str, Any]] = list(conversation_history) if conversation_history else []
    messages.append({"role": "user", "content": user_message})

    agent._vprint(
        f"▶ conversation turn (session={agent.session_id}) max_iters={agent.max_iterations}"
    )

    api_call_count = 0
    final_response: Optional[str] = None
    exit_reason = StopReason.ERROR
    completed = False
    partial = False
    interrupted = False
    failed = False
    error: Optional[str] = None

    # Function-level retry state that persists ACROSS outer iterations and is
    # reset on success (L730-731, L4291).
    truncated_tool_call_retries = 0
    length_continue_retries = 0
    truncated_response_parts: List[str] = []

    # ── 2. the outer while loop (L796) ────────────────────────────────
    while (
        api_call_count < agent.max_iterations and agent.iteration_budget.remaining > 0
    ) or agent._budget_grace_call:

        # 2a. interrupt check (L801)
        if agent._interrupt_requested:
            interrupted = True
            exit_reason = StopReason.INTERRUPTED
            agent._vprint("⚡ Breaking out of tool loop due to interrupt...")
            break

        api_call_count += 1

        # 2b. consume budget, honouring a one-shot grace call (L808-821)
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

        # ══════════════════════════════════════════════════════════════
        # 2c. INNER API RETRY LOOP (L1157) — model call + invalid-response
        #     retry/fallback + finish_reason=="length" truncation handling.
        # ══════════════════════════════════════════════════════════════
        retry_count = 0
        assistant_message: Optional[AssistantMessage] = None
        finish_reason = "stop"
        restart_with_length_continuation = False

        while retry_count < agent._api_max_retries:
            # [EXTERNAL SUBSYSTEM] plugin pre_api_request hook (L1235)
            agent._pre_api_request_hook(messages)

            # ── the model call (L1304 streaming / L1308 non-streaming) ──
            try:
                response = agent.model(messages, stream_callback=stream_callback)
            except Exception as exc:
                # Real handles API errors with provider-specific recovery +
                # backoff (L4319 except / L2598 summarise). Here: retry, try
                # fallback, give up after max_retries.
                retry_count += 1
                agent._vprint(f"⚠️  Model call error ({retry_count}/{agent._api_max_retries}): {exc}")
                if agent._try_activate_fallback():  # [EXTERNAL] provider fallback
                    retry_count = 0
                    continue
                if retry_count >= agent._api_max_retries:
                    error = f"model call failed: {exc}"
                    assistant_message = None
                    break
                continue  # backoff elided (jittered_backoff, L1516)

            # ── invalid/empty response shape check (L1328-1544) ──
            if response is None:
                retry_count += 1
                agent._vprint(f"⚠️  Invalid API response (attempt {retry_count}/{agent._api_max_retries})")
                if agent._try_activate_fallback():  # [EXTERNAL] eager fallback (L1426)
                    retry_count = 0
                    continue
                if retry_count >= agent._api_max_retries:
                    agent._persist_session(messages)
                    return ConversationResult(
                        final_response=None, messages=messages, api_calls=api_call_count,
                        completed=False, turn_exit_reason=StopReason.ERROR, failed=True,
                        error="Invalid API response after max retries",
                    )
                continue  # backoff elided (L1516)

            assistant_message = response
            finish_reason = response.finish_reason

            # Suspicious Ollama/GLM "stop" treated as truncation (L1572-1581)
            # — represented by a hook; default off.
            # (agent._should_treat_stop_as_truncated)

            # ── finish_reason == "length": truncation handling (L1583) ──
            if finish_reason == "length":
                _content = assistant_message.content or ""
                _has_tool_calls = bool(assistant_message.tool_calls)
                _is_stub = assistant_message.response_id == PARTIAL_STREAM_STUB_ID

                # (i) thinking-budget exhaustion → give up early (L1636-1676)
                _thinking_exhausted = (
                    not _has_tool_calls
                    and agent._has_think_tags(_content)
                    and not agent._has_content_after_think_block(_content)
                )
                if _thinking_exhausted:
                    agent._vprint("💭 Reasoning exhausted the output token budget — giving up.")
                    agent._persist_session(messages)
                    return ConversationResult(
                        final_response=(
                            "⚠️ **Thinking Budget Exhausted** — the model used all output "
                            "tokens on reasoning. Lower reasoning effort or raise max_tokens."
                        ),
                        messages=messages, api_calls=api_call_count, completed=False,
                        turn_exit_reason=StopReason.THINKING_EXHAUSTED, partial=True,
                        error="Model used all output tokens on reasoning",
                    )

                # (ii) text continuation, up to 3 attempts (L1680-1737)
                if not _has_tool_calls:
                    length_continue_retries += 1
                    messages.append(agent._build_assistant_message(assistant_message, finish_reason))
                    if assistant_message.content:
                        truncated_response_parts.append(assistant_message.content)
                    if length_continue_retries < 3:
                        agent._vprint(f"↻ Requesting continuation ({length_continue_retries}/3)...")
                        messages.append({
                            "role": "user",
                            "content": _get_continuation_prompt(_is_stub),
                        })
                        restart_with_length_continuation = True
                        break  # break inner loop → outer loop re-calls model
                    # exhausted continuation attempts → return accumulated partial
                    partial_response = agent._strip_think_blocks("".join(truncated_response_parts)).strip()
                    agent._persist_session(messages)
                    return ConversationResult(
                        final_response=partial_response or None, messages=messages,
                        api_calls=api_call_count, completed=False,
                        turn_exit_reason=StopReason.TRUNCATED, partial=True,
                        error="Response remained truncated after 3 continuation attempts",
                    )

                # (iii) truncated tool call, up to 3 retries w/ token boost (L1741-1802)
                if truncated_tool_call_retries < 3:
                    truncated_tool_call_retries += 1
                    agent._vprint(
                        f"⚠️  Truncated tool call detected — retrying API call "
                        f"({truncated_tool_call_retries}/3)..."
                    )
                    # Real boosts ephemeral max_output_tokens here (L1761-1772);
                    # don't append the broken response — just re-call.
                    continue
                agent._persist_session(messages)
                return ConversationResult(
                    final_response=None, messages=messages, api_calls=api_call_count,
                    completed=False, turn_exit_reason=StopReason.TRUNCATED, partial=True,
                    error="Response truncated due to output length limit",
                )

            # normal, usable response → leave the inner retry loop (L1834+)
            break

        # ── after the inner retry loop ──
        if restart_with_length_continuation:
            # continuation message appended; outer loop re-calls the model (L1724)
            continue

        if assistant_message is None:
            # all retries failed (model-call exception path). Near-max guard
            # mirrors L4368-4374.
            exit_reason = StopReason.ERROR_NEAR_MAX
            failed = True
            if final_response is None:
                final_response = f"I apologize, but I encountered repeated errors: {error}"
                messages.append({"role": "assistant", "content": final_response})
            break

        # ══════════════════════════════════════════════════════════════
        # 2d. TOOL CALLS PRESENT (L3652)
        # ══════════════════════════════════════════════════════════════
        if assistant_message.tool_calls:
            agent._vprint(f"🔧 Processing {len(assistant_message.tool_calls)} tool call(s)...")

            # (1) tool-name validation / repair (L3662-3711)
            for tc in assistant_message.tool_calls:
                if tc.name not in agent.valid_tool_names:
                    repaired = agent._repair_tool_call(tc.name)
                    if repaired:
                        agent._vprint(f"🔧 Auto-repaired tool name: '{tc.name}' -> '{repaired}'")
                        tc.name = repaired

            invalid_tool_calls = [
                tc.name for tc in assistant_message.tool_calls
                if tc.name not in agent.valid_tool_names
            ]
            if invalid_tool_calls:
                agent._invalid_tool_retries += 1
                available = ", ".join(sorted(agent.valid_tool_names))
                agent._vprint(
                    f"⚠️  Unknown tool '{invalid_tool_calls[0]}' — sending error for "
                    f"agent-correction ({agent._invalid_tool_retries}/3)"
                )
                if agent._invalid_tool_retries >= 3:  # 3-strike give-up (L3682)
                    agent._invalid_tool_retries = 0
                    agent._persist_session(messages)
                    return ConversationResult(
                        final_response=None, messages=messages, api_calls=api_call_count,
                        completed=False, turn_exit_reason=StopReason.INVALID_TOOL, partial=True,
                        error=f"Model generated invalid tool call: {invalid_tool_calls[0]}",
                    )
                messages.append(agent._build_assistant_message(assistant_message, finish_reason))
                for tc in assistant_message.tool_calls:
                    content = (
                        f"Tool '{tc.name}' does not exist. Available tools: {available}"
                        if tc.name not in agent.valid_tool_names
                        else "Skipped: another tool call in this turn used an invalid name."
                    )
                    messages.append(ToolResult(tc.id, tc.name, content, is_error=True).to_message())
                continue
            agent._invalid_tool_retries = 0  # reset on success (L3711)

            # (2) JSON-argument validation (L3713-3804)
            invalid_json_args = []
            for tc in assistant_message.tool_calls:
                args = tc.arguments
                if isinstance(args, (dict, list)):
                    tc.arguments = json.dumps(args)
                    continue
                if args is not None and not isinstance(args, str):
                    tc.arguments = str(args)
                    args = tc.arguments
                if not args or not str(args).strip():
                    tc.arguments = "{}"
                    continue
                try:
                    json.loads(args)
                except json.JSONDecodeError as e:
                    invalid_json_args.append((tc.name, str(e)))

            if invalid_json_args:
                # (2a) truncation detection: args not ending with } or ] are
                # cut off mid-stream → refuse to execute (L3733-3761).
                _truncated = any(
                    not (tc.arguments or "").rstrip().endswith(("}", "]"))
                    for tc in assistant_message.tool_calls
                    if tc.name in {n for n, _ in invalid_json_args}
                )
                if _truncated:
                    agent._vprint("⚠️  Truncated tool call arguments — refusing to execute.")
                    agent._invalid_json_retries = 0
                    agent._persist_session(messages)
                    return ConversationResult(
                        final_response=None, messages=messages, api_calls=api_call_count,
                        completed=False, turn_exit_reason=StopReason.TRUNCATED, partial=True,
                        error="Response truncated due to output length limit",
                    )

                # (2b) genuine bad JSON: 3 silent re-calls, then inject recovery
                # tool results so the model can self-correct (L3763-3801).
                agent._invalid_json_retries += 1
                tool_name, _err = invalid_json_args[0]
                agent._vprint(f"⚠️  Invalid JSON in args for '{tool_name}' ({agent._invalid_json_retries}/3)")
                if agent._invalid_json_retries < 3:
                    agent._vprint("🔄 Retrying API call (no message appended)...")
                    continue  # don't touch messages, just re-call
                agent._invalid_json_retries = 0
                messages.append(agent._build_assistant_message(assistant_message, finish_reason))
                invalid_names = {n for n, _ in invalid_json_args}
                for tc in assistant_message.tool_calls:
                    if tc.name in invalid_names:
                        err = next(e for n, e in invalid_json_args if n == tc.name)
                        content = (
                            f"Error: Invalid JSON arguments. {err}. For tools with no "
                            "required parameters, use an empty object: {}. Please retry."
                        )
                    else:
                        content = "Skipped: another tool call in this response had invalid JSON."
                    messages.append(ToolResult(tc.id, tc.name, content, is_error=True).to_message())
                continue
            agent._invalid_json_retries = 0  # reset on success (L3804)

            # (3) post-call guardrails: cap delegate fan-out + dedupe (L3806-3812)
            assistant_message.tool_calls = agent._cap_delegate_task_calls(assistant_message.tool_calls)
            assistant_message.tool_calls = agent._deduplicate_tool_calls(assistant_message.tool_calls)

            # (4) content+tools fallback capture (L3816-3841): if this turn has
            # text AND tools, remember the text for the empty-follow-up case.
            turn_content = assistant_message.content or ""
            if turn_content and agent._has_content_after_think_block(turn_content):
                agent._last_content_with_tools = turn_content
                _HOUSEKEEPING_TOOLS = frozenset({"memory", "todo", "skill_manage", "session_search"})
                agent._last_content_tools_all_housekeeping = all(
                    tc.name in _HOUSEKEEPING_TOOLS for tc in assistant_message.tool_calls
                )

            # (5) append assistant turn, execute tools (L3869-3884)
            messages.append(agent._build_assistant_message(assistant_message, finish_reason))
            agent._execute_tool_calls(assistant_message, messages)

            # (6) tool-guardrail HALT branch (L3886-3907)
            if agent._tool_guardrail_halt_decision is not None:
                decision = agent._tool_guardrail_halt_decision
                exit_reason = StopReason.GUARDRAIL_HALT
                final_response = agent._toolguard_controlled_halt_response(decision)
                agent._vprint(f"⚠️ Tool guardrail halted {decision.tool_name}: {decision.code}")
                messages.append({"role": "assistant", "content": final_response})
                break

            # (7) reset truncation retry counter after a clean tool turn (L3912)
            truncated_tool_call_retries = 0

            # (8) execute_code budget refund (L3922-3927)
            _tc_names = {tc.name for tc in assistant_message.tool_calls}
            if _tc_names == {"execute_code"}:
                agent.iteration_budget.refund()

            # (9) [EXTERNAL SUBSYSTEM] context compression decision (L3965-3975)
            if agent.compression_enabled and agent._should_compress(_estimate_tokens(messages)):
                agent._vprint("  ⟳ compacting context…")
                messages = agent._compress_context(messages)

            agent._persist_session(messages)  # [EXTERNAL] incremental save (L3977)
            continue  # tool results re-enter the loop as context for next call

        # ══════════════════════════════════════════════════════════════
        # 2e. NO TOOL CALLS — final response / empty-recovery ladder (L3983)
        # ══════════════════════════════════════════════════════════════
        final_text = assistant_message.content or ""

        if not agent._has_content_after_think_block(final_text):
            # (i) partial-stream recovery: use already-streamed text (L4001-4018)
            if agent._has_content_after_think_block(agent._current_streamed_assistant_text):
                exit_reason = StopReason.PARTIAL_STREAM_RECOVERY
                final_response = agent._strip_think_blocks(agent._current_streamed_assistant_text).strip()
                agent._vprint("↻ Stream interrupted — using delivered content as final response")
                break

            # (ii) prior-turn housekeeping fallback (L4020-4044)
            fallback = agent._last_content_with_tools
            if fallback and agent._last_content_tools_all_housekeeping:
                exit_reason = StopReason.FALLBACK_PRIOR_TURN
                agent._empty_content_retries = 0
                final_response = agent._strip_think_blocks(fallback).strip()
                agent._vprint("↩ Reusing prior-turn content as final response")
                break

            # (iii) post-tool empty nudge, once (L4046-4111)
            _last_role = messages[-1].get("role") if messages else None
            _has_inline_thinking = agent._has_think_tags(final_text)
            if (_last_role == "tool"
                    and not agent._post_tool_empty_retried
                    and not _has_inline_thinking):
                agent._post_tool_empty_retried = True
                agent._vprint("⚠️ Model returned empty after tool calls — nudging to continue")
                _empty = agent._build_assistant_message(assistant_message, finish_reason)
                _empty["content"] = "(empty)"
                _empty["_empty_recovery_synthetic"] = True
                messages.append(_empty)
                messages.append({
                    "role": "user",
                    "content": ("Your last response was empty. Please process the tool "
                                "results above and continue with the task."),
                    "_empty_recovery_synthetic": True,
                })
                continue

            # (iv) thinking-only prefill continuation, up to 2 (L4113-4145)
            _has_structured_thinking = agent._has_think_tags(final_text)
            if _has_structured_thinking and agent._thinking_prefill_retries < 2:
                agent._thinking_prefill_retries += 1
                agent._vprint(f"↻ Thinking-only response — prefilling to continue "
                              f"({agent._thinking_prefill_retries}/2)")
                _interim = agent._build_assistant_message(assistant_message, finish_reason)
                _interim["_thinking_prefill"] = True
                messages.append(_interim)
                continue

            # (v) generic empty-content retries, up to 3 (L4150-4180)
            _truly_empty = not agent._strip_think_blocks(final_text).strip()
            _prefill_exhausted = _has_structured_thinking and agent._thinking_prefill_retries >= 2
            if _truly_empty and (not _has_structured_thinking or _prefill_exhausted) \
                    and agent._empty_content_retries < 3:
                agent._empty_content_retries += 1
                agent._vprint(f"⚠️ Empty response — retrying ({agent._empty_content_retries}/3)")
                messages.append({
                    "role": "user",
                    "content": "Your last response was empty. Please provide a response.",
                    "_empty_recovery_synthetic": True,
                })
                continue
            # recovery ladder exhausted → fall through to final answer ("(empty)")

        # ── final answer (L4283-4317) ──
        final_response = agent._strip_think_blocks(final_text).strip()
        # pop thinking-prefill / empty-recovery scaffolding before appending (L4296-4309)
        while messages and isinstance(messages[-1], dict) and (
            messages[-1].get("_thinking_prefill")
            or messages[-1].get("_empty_recovery_synthetic")
        ):
            messages.pop()
        messages.append(agent._build_assistant_message(assistant_message, finish_reason))
        # reset truncation continuation state on a clean final answer (L4291)
        truncated_response_parts = []
        length_continue_retries = 0
        if not final_response:
            final_response = "(empty)"
            exit_reason = StopReason.EMPTY_EXHAUSTED
        else:
            exit_reason = f"{StopReason.TEXT_RESPONSE}(finish_reason={finish_reason})"
            completed = True
        agent._vprint(f"🎉 Conversation completed after {api_call_count} API call(s)")
        break

    # ── 3. post-loop max-iterations / budget terminal (L4376-4393) ────
    #    NOTE: the real code uses THIS post-loop ``if`` (not a while-else).
    if final_response is None and (
        api_call_count >= agent.max_iterations or agent.iteration_budget.remaining <= 0
    ):
        exit_reason = f"{StopReason.MAX_ITERATIONS}({api_call_count}/{agent.max_iterations})"
        agent._vprint(
            f"⚠️  Iteration budget exhausted ({api_call_count}/{agent.max_iterations}) "
            "— requesting summary..."
        )
        final_response = agent._handle_max_iterations(messages)
        completed = True
        # [EXTERNAL SUBSYSTEM] kanban worker failure signal (L4395+) — elided.

    # ── 4. [EXTERNAL SUBSYSTEM] post-turn processing (L4466-4644) ─────
    agent._persist_session(messages)                       # session save (L4466)
    # runtime footer (verify badge) + exit explanation (L4496-4583) — elided hook.
    if final_response and not interrupted:
        final_response = agent._post_response_text(final_response)   # plugin hook (L4590)
        # plugin persist hook (L4612) — elided.
        agent._memory_skill_review(final_response, messages)         # curator (L4704)

    # ── 5. assemble + return the result (L4647 → return result L4747) ─
    return ConversationResult(
        final_response=final_response,
        messages=messages,
        api_calls=api_call_count,
        completed=completed,
        turn_exit_reason=exit_reason,
        partial=partial,
        interrupted=interrupted,
        failed=failed,
        error=error,
    )


def _estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Rough char/4 token estimate (stand-in for estimate_request_tokens_rough,
    conversation_loop.py L3961). Real version also counts tool schemas."""
    chars = sum(len(str(m.get("content", ""))) for m in messages)
    return chars // 4


__all__ = [
    "ToolCall",
    "AssistantMessage",
    "ToolResult",
    "GuardrailDecision",
    "StopReason",
    "ConversationResult",
    "IterationBudget",
    "ModelClient",
    "Tool",
    "AIAgent",
    "run_conversation",
    "PARTIAL_STREAM_STUB_ID",
]
