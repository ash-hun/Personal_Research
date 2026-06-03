#!/usr/bin/env python3
"""
subagent_delegation.py — A self-contained, stdlib-only mirror of the Hermes Agent
(Nous Research) *subagent delegation* feature.

This distills, in faithful-but-runnable form, how the parent Hermes agent spawns
ISOLATED child agents for parallel workstreams and collects their results back as
tool results, plus the Mixture-of-Agents (MoA) fan-out/aggregate pattern.

Source files mirrored (paths are relative to the hermes-agent reference root):
  - tools/delegate_tool.py
        * _build_child_system_prompt()  -> build_child_system_prompt()
        * _build_child_agent()          -> build_child_agent()
        * _run_single_child()           -> run_single_child()
        * delegate_task()               -> delegate_task()
        * _strip_blocked_tools()        -> strip_blocked_tools()
        * _normalize_role()             -> normalize_role()
  - tools/mixture_of_agents_tool.py
        * mixture_of_agents_tool()      -> mixture_of_agents()
        * _construct_aggregator_prompt()-> construct_aggregator_prompt()
        * AGGREGATOR_SYSTEM_PROMPT       (verbatim from the paper)
  - run_agent.py
        * AIAgent.run_conversation(user_message=goal, task_id=...)
                                        -> the RunAgent callable contract
        * AIAgent._dispatch_delegate_task() -> shows parent dispatch contract

The Hermes original spins up a real `AIAgent` (an LLM-backed agent) per child.
Here the LLM is abstracted behind a single pluggable callable — `RunAgent` — so the
whole flow runs offline with a fake child. Swap in your own `run_agent` to drive a
real model.

Design fidelity notes
---------------------
* CONTEXT ISOLATION: in Hermes, the child `AIAgent` is built with
  `ephemeral_system_prompt=child_prompt`, `skip_context_files=True`,
  `skip_memory=True`, and a *fresh* iteration budget. The child NEVER receives the
  parent's conversation history — only the delegated brief (goal + optional
  context). We reproduce that exactly: the child only ever sees `DelegateSpec`.
  (delegate_tool.py:1106-1137 — the `AIAgent(...)` construction.)

* ALLOWED-TOOLS RESTRICTION: requested `toolsets` are intersected with the parent's
  toolsets (a child can never gain a tool the parent lacks), then
  `strip_blocked_tools` removes delegation/clarify/memory/code_execution. An
  'orchestrator' role re-adds 'delegation' so it can spawn its own workers.
  (delegate_tool.py:945-968, 672-680.)

* SPAWN -> RUN -> COLLECT: `build_child_agent` constructs the child on the calling
  thread; `run_single_child` runs it via `child.run_conversation(user_message=goal,
  task_id=...)` and extracts `final_response` as the `summary` returned to the
  parent. (delegate_tool.py:1507-1510, 1620-1700.)

* PARALLEL FAN-OUT: a single task runs directly; a batch runs through a
  `ThreadPoolExecutor(max_workers=max_concurrent_children)`. Results are sorted by
  `task_index` so they match input order, then serialized to a JSON tool result.
  (delegate_tool.py:2091-2213, 2303-2309.)

* MIXTURE OF AGENTS: Layer 1 fans N reference agents out in parallel; Layer 2 feeds
  all of their responses into an aggregator with the paper's synthesis prompt.
  (mixture_of_agents_tool.py:312-356, 90-102, 83-85.)
"""

from __future__ import annotations

import enum
import json
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol


# ---------------------------------------------------------------------------
# Configuration constants — mirrors of delegate_tool.py module-level defaults.
# In Hermes these come from `delegation.*` config keys; here they are plain
# constants you can tune. (delegate_tool.py:_get_max_concurrent_children /
# _get_max_spawn_depth / _get_child_timeout.)
# ---------------------------------------------------------------------------
DEFAULT_MAX_ITERATIONS: int = 50          # per-subagent iteration budget
MAX_CONCURRENT_CHILDREN: int = 8          # ThreadPoolExecutor width for batch fan-out
MAX_SPAWN_DEPTH: int = 2                   # how deep orchestrator->worker nesting may go
DEFAULT_TOOLSETS: List[str] = ["terminal", "file", "web", "delegation"]

# Toolsets a leaf subagent must never receive — verbatim set from
# delegate_tool.py:_strip_blocked_tools (672-680). 'delegation' is stripped so a
# leaf cannot spawn grandchildren; an orchestrator role re-adds it later.
BLOCKED_TOOLSET_NAMES = {"delegation", "clarify", "memory", "code_execution"}

# The aggregator system prompt is quoted VERBATIM from the MoA paper, exactly as
# embedded in mixture_of_agents_tool.py:83-85.
AGGREGATOR_SYSTEM_PROMPT: str = (
    "You have been provided with a set of responses from various open-source "
    "models to the latest user query. Your task is to synthesize these responses "
    "into a single, high-quality response. It is crucial to critically evaluate "
    "the information provided in these responses, recognizing that some of it may "
    "be biased or incorrect. Your response should not simply replicate the given "
    "answers but should offer a refined, accurate, and comprehensive reply to the "
    "instruction. Ensure your response is well-structured, coherent, and adheres "
    "to the highest standards of accuracy and reliability.\n\nResponses from models:"
)


# ---------------------------------------------------------------------------
# Roles — mirror of delegate_tool.py:_normalize_role (312-329) and DelegateEvent.
# ---------------------------------------------------------------------------
class Role(str, enum.Enum):
    """Whether a child may further delegate.

    'leaf' (default) cannot spawn its own workers; 'orchestrator' retains the
    'delegation' toolset (subject to depth bounds) and can decompose further.
    Mirrors the role semantics in delegate_tool.py:_build_child_agent (904-913).
    """

    LEAF = "leaf"
    ORCHESTRATOR = "orchestrator"


def normalize_role(role: Optional[str]) -> Role:
    """Coerce an arbitrary role string to a known Role, defaulting to LEAF.

    Mirror of delegate_tool.py:_normalize_role (312-329): unknown values degrade
    to 'leaf' so a typo can never silently grant spawning power.
    """
    if isinstance(role, Role):
        return role
    text = (role or "").strip().lower()
    return Role.ORCHESTRATOR if text == "orchestrator" else Role.LEAF


# ---------------------------------------------------------------------------
# I/O dataclasses — the typed contract a researcher customizes.
# ---------------------------------------------------------------------------
@dataclass
class DelegateSpec:
    """A single delegated task brief — the ONLY thing a child agent ever sees.

    This is the isolated context boundary. The parent's full conversation history
    is deliberately absent: a child receives `goal` + optional `context` and
    nothing else. Mirrors the per-task dict consumed by
    delegate_tool.py:delegate_task (task_list entries, 2018-2021) and the prompt
    inputs of _build_child_system_prompt (569-577).

    Attributes:
        goal:        The objective the subagent must accomplish (becomes "YOUR TASK").
        context:     Optional background hand-off text (becomes "CONTEXT" block).
        toolsets:    Requested toolset names. Intersected with the parent's
                     toolsets, then blocked sets stripped. None => inherit parent's.
        role:        'leaf' or 'orchestrator' (controls re-delegation capability).
        max_iterations: Per-subagent iteration budget (fresh, not shared w/ parent).
        workspace_path: Optional absolute workdir hint injected into the prompt.
    """

    goal: str
    context: Optional[str] = None
    toolsets: Optional[List[str]] = None
    role: Role = Role.LEAF
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    workspace_path: Optional[str] = None


@dataclass
class SubagentResult:
    """Structured result collected back from one child run.

    Mirror of the per-task entry dict assembled in
    delegate_tool.py:_run_single_child (1684-1700) and returned (sorted by
    task_index) inside delegate_task's JSON tool result (2303-2309).

    Attributes:
        task_index:       Position in the batch (used to restore input order).
        status:           'completed' | 'failed' | 'timeout' | 'error' | 'interrupted'.
        summary:          The child's final answer — its `final_response`. THIS is
                          what flows back to the parent as the tool result.
        exit_reason:      'completed' | 'max_iterations' | 'timeout' | 'error'.
        api_calls:        How many model calls the child made (mock-friendly).
        duration_seconds: Wall-clock time of the child run.
        model:            The child's effective model name (if any).
        role:             The post-degrade role the child actually ran as.
        tool_trace:       Lightweight record of tools the child invoked.
        error:            Error text when status is not 'completed'.
    """

    task_index: int
    status: str
    summary: Optional[str]
    exit_reason: str = "completed"
    api_calls: int = 0
    duration_seconds: float = 0.0
    model: Optional[str] = None
    role: Role = Role.LEAF
    tool_trace: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ChildAgent:
    """A constructed-but-not-yet-run child agent.

    The stdlib stand-in for the real `AIAgent` object that
    delegate_tool.py:_build_child_agent returns (870-1174). It captures exactly the
    isolated inputs the child will run on — proving the child cannot reach back into
    the parent's state. The `run_agent` callable is the child's "brain".
    """

    task_index: int
    goal: str
    system_prompt: str          # ephemeral_system_prompt — the isolated brief
    toolsets: List[str]         # the restricted, intersected, de-blocked toolset
    role: Role
    depth: int                  # _delegate_depth of THIS child (parent depth + 1)
    max_iterations: int
    model: Optional[str]
    run_agent: "RunAgent"       # pluggable child "brain" (mockable)


class RunAgent(Protocol):
    """The pluggable child-run contract.

    Hermes calls `child.run_conversation(user_message=goal, task_id=...)` and reads
    `result["final_response"]` (delegate_tool.py:1507-1510, 1620). We collapse that
    into one callable so the LLM can be mocked. A faithful implementation returns a
    dict shaped like Hermes' `run_conversation` result.

    Args (as keywords):
        user_message:    the delegated goal (the child's only "user" turn).
        system_prompt:   the isolated ephemeral system prompt.
        toolsets:        the restricted toolset the child may use.
        task_id:         a stable id (mirrors child_task_id == subagent_id).

    Returns a dict with at least:
        final_response (str), completed (bool), api_calls (int),
        and optionally interrupted (bool) and messages (list).
    """

    def __call__(
        self,
        *,
        user_message: str,
        system_prompt: str,
        toolsets: List[str],
        task_id: str,
    ) -> Dict[str, Any]: ...


# ---------------------------------------------------------------------------
# Parent context — the minimal slice of `AIAgent` that delegation reads.
# ---------------------------------------------------------------------------
@dataclass
class ParentAgent:
    """Minimal parent-agent surface that delegation needs.

    The real Hermes `parent_agent` is a full `AIAgent`; delegation only reads a
    handful of attributes off it (enabled toolsets, model, delegate depth, the
    child "brain" factory). We capture exactly those.

    `history` is included ONLY to demonstrate isolation: it is intentionally NEVER
    passed to any child. (delegate_tool.py builds the child from goal/context only.)
    """

    model: str = "hermes-parent"
    enabled_toolsets: List[str] = field(default_factory=lambda: list(DEFAULT_TOOLSETS))
    delegate_depth: int = 0
    run_agent: Optional[RunAgent] = None
    # Parent's private conversation — the thing the child must NOT see.
    history: List[Dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Toolset restriction — mirror of delegate_tool.py:_strip_blocked_tools (672-680)
# and the intersection logic in _build_child_agent (945-968).
# ---------------------------------------------------------------------------
def strip_blocked_tools(toolsets: List[str]) -> List[str]:
    """Remove toolsets a subagent must never receive.

    Verbatim policy from delegate_tool.py:_strip_blocked_tools (672-680).
    """
    return [t for t in toolsets if t not in BLOCKED_TOOLSET_NAMES]


def resolve_child_toolsets(
    requested: Optional[List[str]],
    parent_toolsets: List[str],
    role: Role,
) -> List[str]:
    """Compute the child's effective toolset.

    Mirror of delegate_tool.py:_build_child_agent (945-968):
      1. If `requested` given, INTERSECT with the parent's toolsets — a subagent
         must never gain a tool the parent lacks.
      2. Otherwise inherit the parent's toolsets.
      3. Strip blocked toolsets (delegation/clarify/memory/code_execution).
      4. An 'orchestrator' re-adds 'delegation' (capability granted by role, not
         inherited) so it can spawn its own workers.
    """
    parent_set = set(parent_toolsets)
    if requested:
        child = [t for t in requested if t in parent_set]  # intersection
    else:
        child = list(parent_toolsets)                       # inherit
    child = strip_blocked_tools(child)
    if role == Role.ORCHESTRATOR and "delegation" not in child:
        child.append("delegation")
    return child


# ---------------------------------------------------------------------------
# Child system prompt — mirror of delegate_tool.py:_build_child_system_prompt
# (569-642). The wording is condensed but structurally faithful: TASK, optional
# CONTEXT, optional WORKSPACE PATH, a summary instruction, and (for orchestrators)
# a delegation-capability block with a literal depth note.
# ---------------------------------------------------------------------------
def build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
    role: Role = Role.LEAF,
    max_spawn_depth: int = MAX_SPAWN_DEPTH,
    child_depth: int = 1,
) -> str:
    """Build a focused, ISOLATED system prompt for a child agent.

    The child's entire worldview is constructed here from the brief alone — there
    is no path by which the parent's history could enter. Mirrors
    delegate_tool.py:_build_child_system_prompt (569-642).
    """
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    if workspace_path and str(workspace_path).strip():
        parts.append(
            "\nWORKSPACE PATH:\n"
            f"{workspace_path}\n"
            "Use this exact path for local repository/workdir operations unless "
            "the task explicitly says otherwise."
        )
    parts.append(
        "\nComplete this task using the tools available to you. When finished, "
        "provide a clear, concise summary of what you did, what you found, and any "
        "issues encountered. Your response is returned to the parent agent as a "
        "summary."
    )
    if role == Role.ORCHESTRATOR:
        if child_depth + 1 >= max_spawn_depth:
            child_note = (
                "Your own children MUST be leaves (cannot delegate further) "
                "because they would be at the depth floor."
            )
        else:
            child_note = (
                "Your own children can themselves be orchestrators or leaves, "
                "depending on the role you pass to delegate_task."
            )
        parts.append(
            "\n## Subagent Spawning (Orchestrator Role)\n"
            "You have access to the delegate_task tool and CAN spawn your own "
            "subagents to parallelize independent work. Coordinate your workers' "
            "results and synthesize them before reporting back to your parent.\n"
            f"NOTE: You are at depth {child_depth}. The delegation tree is capped "
            f"at max_spawn_depth={max_spawn_depth}. {child_note}"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Build child — mirror of delegate_tool.py:_build_child_agent (870-1174).
# Constructs (but does not run) an isolated child. This is the single point where
# role degrades to 'leaf' if depth would be exceeded.
# ---------------------------------------------------------------------------
def build_child_agent(
    task_index: int,
    spec: DelegateSpec,
    parent_agent: ParentAgent,
    *,
    run_agent: Optional[RunAgent] = None,
    model: Optional[str] = None,
) -> ChildAgent:
    """Construct an isolated child agent from a `DelegateSpec`.

    Mirrors the construction half of delegate_tool.py:_build_child_agent:
      * child_depth = parent depth + 1; role degrades to LEAF past max depth
        (delegate_tool.py:910-913).
      * toolsets intersected with parent + blocked-stripped + orchestrator re-add
        (delegate_tool.py:945-968).
      * ephemeral_system_prompt built from the brief ONLY (delegate_tool.py:971-978,
        1121).
      * fresh iteration budget; skip_context_files / skip_memory are implied by the
        fact that we pass nothing but the brief (delegate_tool.py:1124-1126, 1136).
    """
    child_depth = parent_agent.delegate_depth + 1

    # Role resolution: honor 'orchestrator' only while depth allows it. Single
    # point of degradation (delegate_tool.py:910-913).
    requested_role = normalize_role(spec.role)
    orchestrator_ok = child_depth < MAX_SPAWN_DEPTH
    effective_role = (
        Role.ORCHESTRATOR
        if (requested_role == Role.ORCHESTRATOR and orchestrator_ok)
        else Role.LEAF
    )

    child_toolsets = resolve_child_toolsets(
        spec.toolsets, parent_agent.enabled_toolsets, effective_role
    )

    child_prompt = build_child_system_prompt(
        spec.goal,
        spec.context,
        workspace_path=spec.workspace_path,
        role=effective_role,
        max_spawn_depth=MAX_SPAWN_DEPTH,
        child_depth=child_depth,
    )

    # Resolve the child "brain": explicit > parent's > module default fake.
    effective_run = run_agent or parent_agent.run_agent or fake_run_agent
    effective_model = model or parent_agent.model

    return ChildAgent(
        task_index=task_index,
        goal=spec.goal,
        system_prompt=child_prompt,
        toolsets=child_toolsets,
        role=effective_role,
        depth=child_depth,
        max_iterations=spec.max_iterations,
        model=effective_model,
        run_agent=effective_run,
    )


# ---------------------------------------------------------------------------
# Run child — mirror of delegate_tool.py:_run_single_child (1321-1700).
# Runs a pre-built child via its RunAgent callable, with a wall-clock timeout, and
# distills the raw result into a SubagentResult.
# ---------------------------------------------------------------------------
def run_single_child(
    child: ChildAgent,
    *,
    timeout_seconds: float = 60.0,
) -> SubagentResult:
    """Run one pre-built child agent and collect a structured result.

    Mirrors delegate_tool.py:_run_single_child:
      * runs the child under a ThreadPoolExecutor with a hard timeout so a wedged
        child cannot block the parent forever (1492-1514).
      * extracts `final_response` as the `summary` (1620).
      * derives status/exit_reason from completed/interrupted flags (1625-1677).
    """
    child_start = time.monotonic()
    task_id = f"sa-{child.task_index}"  # stable id (mirrors subagent_id reuse, 1482)

    def _invoke() -> Dict[str, Any]:
        return child.run_agent(
            user_message=child.goal,
            system_prompt=child.system_prompt,
            toolsets=child.toolsets,
            task_id=task_id,
        )

    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_invoke)
        try:
            result = future.result(timeout=timeout_seconds)
        except Exception as exc:  # timeout or child raised
            duration = round(time.monotonic() - child_start, 2)
            return SubagentResult(
                task_index=child.task_index,
                status="timeout" if isinstance(exc, TimeoutError) else "error",
                summary=None,
                exit_reason="timeout" if isinstance(exc, TimeoutError) else "error",
                api_calls=0,
                duration_seconds=duration,
                model=child.model,
                role=child.role,
                error=str(exc) or type(exc).__name__,
            )
    finally:
        # Don't wait — a stuck child thread must not hang the parent (1606-1609).
        executor.shutdown(wait=False)

    duration = round(time.monotonic() - child_start, 2)

    summary = result.get("final_response") or ""
    completed = bool(result.get("completed", False))
    interrupted = bool(result.get("interrupted", False))
    api_calls = int(result.get("api_calls", 0) or 0)

    # status derivation — delegate_tool.py:1625-1633.
    if interrupted:
        status = "interrupted"
    elif summary:
        status = "completed"
    else:
        status = "failed"

    # exit_reason — delegate_tool.py:1671-1677.
    if interrupted:
        exit_reason = "interrupted"
    elif completed:
        exit_reason = "completed"
    else:
        exit_reason = "max_iterations"

    # Lightweight tool trace from the child's messages — delegate_tool.py:1635-1669.
    tool_trace: List[Dict[str, Any]] = []
    for msg in result.get("messages") or []:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                tool_trace.append({"tool": fn.get("name", "unknown")})

    return SubagentResult(
        task_index=child.task_index,
        status=status,
        summary=summary,
        exit_reason=exit_reason,
        api_calls=api_calls,
        duration_seconds=duration,
        model=child.model,
        role=child.role,
        tool_trace=tool_trace,
    )


# ---------------------------------------------------------------------------
# delegate_task — mirror of delegate_tool.py:delegate_task (1918-2309).
# The parent-facing tool. Single task runs directly; a batch fans out across a
# thread pool. Returns a JSON tool result (results sorted by task_index).
# ---------------------------------------------------------------------------
def delegate_task(
    parent_agent: ParentAgent,
    *,
    goal: Optional[str] = None,
    context: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    role: Optional[str] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    max_concurrent_children: int = MAX_CONCURRENT_CHILDREN,
    timeout_seconds: float = 60.0,
    return_json: bool = True,
) -> Any:
    """Spawn one or more isolated child agents and collect their results.

    Two modes, mirroring delegate_tool.py:delegate_task (1932-1934):
      * Single: pass `goal` (+ optional context/toolsets/role).
      * Batch:  pass `tasks=[{goal, context, toolsets, role}, ...]`.

    Depth guard (delegate_tool.py:1958-1972): a parent already at MAX_SPAWN_DEPTH
    cannot delegate. Single tasks run directly; batches use a ThreadPoolExecutor
    capped at `max_concurrent_children` (2091-2213).

    Returns the JSON tool-result string (default), or the raw list of
    SubagentResult objects when `return_json=False`.
    """
    # Depth limit — delegate_tool.py:1958-1972.
    if parent_agent.delegate_depth >= MAX_SPAWN_DEPTH:
        payload = {
            "error": (
                f"Delegation depth limit reached (depth={parent_agent.delegate_depth}, "
                f"max_spawn_depth={MAX_SPAWN_DEPTH})."
            )
        }
        return json.dumps(payload, ensure_ascii=False) if return_json else []

    top_role = normalize_role(role)

    # Normalize to a list of DelegateSpec (single vs batch) — delegate_tool.py:2008-2026.
    specs: List[DelegateSpec] = []
    if tasks:
        if len(tasks) > max_concurrent_children:
            payload = {
                "error": (
                    f"Too many tasks: {len(tasks)} provided, but "
                    f"max_concurrent_children is {max_concurrent_children}."
                )
            }
            return json.dumps(payload, ensure_ascii=False) if return_json else []
        for t in tasks:
            g = (t.get("goal") or "").strip()
            if not g:
                payload = {"error": "A task is missing a 'goal'."}
                return json.dumps(payload, ensure_ascii=False) if return_json else []
            specs.append(
                DelegateSpec(
                    goal=g,
                    context=t.get("context"),
                    toolsets=t.get("toolsets") or toolsets,
                    role=normalize_role(t.get("role") or top_role),
                    max_iterations=int(t.get("max_iterations", DEFAULT_MAX_ITERATIONS)),
                    workspace_path=t.get("workspace_path"),
                )
            )
    elif goal and goal.strip():
        specs.append(
            DelegateSpec(
                goal=goal,
                context=context,
                toolsets=toolsets,
                role=top_role,
            )
        )
    else:
        payload = {"error": "Provide either 'goal' (single) or 'tasks' (batch)."}
        return json.dumps(payload, ensure_ascii=False) if return_json else []

    overall_start = time.monotonic()

    # Build all children on the calling thread (thread-safe construction) —
    # delegate_tool.py:2051-2086.
    children = [
        build_child_agent(i, spec, parent_agent) for i, spec in enumerate(specs)
    ]

    results: List[SubagentResult] = []
    if len(children) == 1:
        # Single task — run directly, no pool overhead (delegate_tool.py:2091-2095).
        results.append(run_single_child(children[0], timeout_seconds=timeout_seconds))
    else:
        # Batch — parallel fan-out (delegate_tool.py:2096-2213).
        with ThreadPoolExecutor(max_workers=max_concurrent_children) as ex:
            futures = {
                ex.submit(run_single_child, c, timeout_seconds=timeout_seconds): c
                for c in children
            }
            pending = set(futures.keys())
            while pending:
                done, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in done:
                    child = futures[fut]
                    try:
                        results.append(fut.result())
                    except Exception as exc:
                        results.append(
                            SubagentResult(
                                task_index=child.task_index,
                                status="error",
                                summary=None,
                                exit_reason="error",
                                role=child.role,
                                error=str(exc),
                            )
                        )
        # Sort by task_index so output matches input order (delegate_tool.py:2213).
        results.sort(key=lambda r: r.task_index)

    total_duration = round(time.monotonic() - overall_start, 2)

    if not return_json:
        return results

    # Serialize to the JSON tool result the parent receives (delegate_tool.py:2303-2309).
    return json.dumps(
        {
            "results": [
                {
                    "task_index": r.task_index,
                    "status": r.status,
                    "summary": r.summary,
                    "exit_reason": r.exit_reason,
                    "api_calls": r.api_calls,
                    "duration_seconds": r.duration_seconds,
                    "model": r.model,
                    "role": r.role.value,
                    "tool_trace": r.tool_trace,
                    "error": r.error,
                }
                for r in results
            ],
            "total_duration_seconds": total_duration,
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Mixture-of-Agents — mirror of mixture_of_agents_tool.py (236-409, 90-102).
# Layer 1: fan N reference agents out in parallel. Layer 2: aggregate.
# ---------------------------------------------------------------------------
def construct_aggregator_prompt(system_prompt: str, responses: List[str]) -> str:
    """Append enumerated reference responses to the aggregator's system prompt.

    Verbatim structure from mixture_of_agents_tool.py:_construct_aggregator_prompt
    (90-102).
    """
    response_text = "\n".join(f"{i + 1}. {r}" for i, r in enumerate(responses))
    return f"{system_prompt}\n\n{response_text}"


@dataclass
class MoAResult:
    """Result of a Mixture-of-Agents run.

    Mirror of the JSON dict returned by mixture_of_agents_tool (365-372): the final
    synthesized answer plus which models contributed.
    """

    success: bool
    response: str
    reference_responses: List[str]
    reference_models: List[str]
    aggregator_model: str
    error: Optional[str] = None


def mixture_of_agents(
    user_prompt: str,
    *,
    reference_run_agents: List[RunAgent],
    aggregator_run_agent: RunAgent,
    reference_models: Optional[List[str]] = None,
    aggregator_model: str = "aggregator",
    min_successful_references: int = 1,
    parent_toolsets: Optional[List[str]] = None,
    max_workers: int = MAX_CONCURRENT_CHILDREN,
) -> MoAResult:
    """Run the 2-layer Mixture-of-Agents pattern.

    Mirror of mixture_of_agents_tool.py:mixture_of_agents_tool (236-409):
      * Layer 1 fans out N reference agents IN PARALLEL on the same prompt, each in
        its own isolated context (312-317). Failures are tolerated as long as
        `min_successful_references` succeed (338-339).
      * Layer 2 feeds all successful responses into the aggregator via
        AGGREGATOR_SYSTEM_PROMPT + enumerated responses (345-356).

    Each reference agent here is invoked through the same isolated RunAgent contract
    used by delegation — so MoA is "delegation, fanned out, then synthesized".
    """
    toolsets = list(parent_toolsets or DEFAULT_TOOLSETS)
    ref_models = reference_models or [f"ref-{i}" for i in range(len(reference_run_agents))]

    # Layer 1 — parallel reference responses (asyncio.gather -> ThreadPoolExecutor).
    def _run_reference(idx: int) -> tuple[str, Optional[str]]:
        agent = reference_run_agents[idx]
        prompt = build_child_system_prompt(user_prompt, role=Role.LEAF)
        try:
            res = agent(
                user_message=user_prompt,
                system_prompt=prompt,
                toolsets=toolsets,
                task_id=f"moa-ref-{idx}",
            )
            return (ref_models[idx], res.get("final_response") or "")
        except Exception:  # tolerate per-model failures (mixture_of_agents_tool.py:105-179)
            return (ref_models[idx], None)

    successful_responses: List[str] = []
    failed_models: List[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for model_name, content in ex.map(_run_reference, range(len(reference_run_agents))):
            if content:
                successful_responses.append(content)
            else:
                failed_models.append(model_name)

    if len(successful_responses) < min_successful_references:
        return MoAResult(
            success=False,
            response="MoA processing failed: insufficient successful reference models.",
            reference_responses=successful_responses,
            reference_models=ref_models,
            aggregator_model=aggregator_model,
            error=(
                f"Insufficient successful reference models "
                f"({len(successful_responses)}/{len(ref_models)})."
            ),
        )

    # Layer 2 — synthesize (mixture_of_agents_tool.py:345-356).
    agg_system = construct_aggregator_prompt(AGGREGATOR_SYSTEM_PROMPT, successful_responses)
    agg_res = aggregator_run_agent(
        user_message=user_prompt,
        system_prompt=agg_system,
        toolsets=toolsets,
        task_id="moa-aggregator",
    )
    final = agg_res.get("final_response") or ""

    return MoAResult(
        success=True,
        response=final,
        reference_responses=successful_responses,
        reference_models=ref_models,
        aggregator_model=aggregator_model,
    )


# ---------------------------------------------------------------------------
# A default fake "brain" so the module is runnable with zero wiring. Stands in for
# the real `AIAgent.run_conversation` result shape (run_agent.py:4575+).
# ---------------------------------------------------------------------------
def fake_run_agent(
    *,
    user_message: str,
    system_prompt: str,
    toolsets: List[str],
    task_id: str,
) -> Dict[str, Any]:
    """A trivial offline child 'brain' returning a run_conversation-shaped dict.

    Replace with a real model-backed implementation to make children actually
    reason. The shape (final_response/completed/api_calls/messages) matches what
    delegate_tool.py:_run_single_child reads.
    """
    return {
        "final_response": (
            f"[subagent {task_id}] Completed: {user_message} "
            f"(tools available: {', '.join(toolsets) or 'none'})."
        ),
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "tool_calls": [{"function": {"name": (toolsets[0] if toolsets else "noop")}}],
            },
        ],
    }


__all__ = [
    "Role",
    "normalize_role",
    "DelegateSpec",
    "SubagentResult",
    "ChildAgent",
    "ParentAgent",
    "RunAgent",
    "strip_blocked_tools",
    "resolve_child_toolsets",
    "build_child_system_prompt",
    "build_child_agent",
    "run_single_child",
    "delegate_task",
    "construct_aggregator_prompt",
    "MoAResult",
    "mixture_of_agents",
    "fake_run_agent",
    "AGGREGATOR_SYSTEM_PROMPT",
    "DEFAULT_TOOLSETS",
    "BLOCKED_TOOLSET_NAMES",
    "MAX_SPAWN_DEPTH",
    "MAX_CONCURRENT_CHILDREN",
]
