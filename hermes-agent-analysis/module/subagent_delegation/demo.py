#!/usr/bin/env python3
"""
demo.py — Runnable walkthrough of the Hermes subagent-delegation mirror.

    python3 demo.py

No external dependencies. Uses a fake `run_agent` that SIMULATES a child agent
solving each subtask, so the full spawn -> isolated-run -> collect flow executes
offline. Demonstrates:

  1. A single delegated subtask (runs directly).
  2. A batch fan-out (3 subtasks in parallel via a thread pool).
  3. Mixture-of-Agents (N reference agents -> aggregator synthesis).
  4. Context isolation: the child only sees its brief, never the parent's history.
  5. allowed-tools restriction: a requested toolset is intersected with the parent.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List

from subagent_delegation import (
    ParentAgent,
    Role,
    delegate_task,
    mixture_of_agents,
)

SEP = "=" * 74


def banner(title: str) -> None:
    print("\n" + SEP)
    print(title)
    print(SEP)


# ---------------------------------------------------------------------------
# A fake child "brain". It simulates a subagent solving a subtask by inspecting
# ONLY the brief it was handed (system_prompt + user_message + toolsets). It has no
# access to the parent — that is the whole point of isolation.
# ---------------------------------------------------------------------------
def fake_child_brain(
    *,
    user_message: str,
    system_prompt: str,
    toolsets: List[str],
    task_id: str,
) -> Dict[str, Any]:
    # Simulate a tiny bit of "work" so durations are non-zero and parallelism shows.
    time.sleep(0.05)

    # Prove isolation: assert the parent's secret never leaked into our context.
    leaked = "PARENT_SECRET" in system_prompt or "PARENT_SECRET" in user_message
    isolation_note = "LEAKED!" if leaked else "isolated (no parent history visible)"

    answer = (
        f"Solved '{user_message[:48]}' using [{', '.join(toolsets) or 'no tools'}]. "
        f"Context: {isolation_note}."
    )
    return {
        "final_response": answer,
        "completed": True,
        "interrupted": False,
        "api_calls": 2,
        "messages": [
            {"role": "user", "content": user_message},
            {
                "role": "assistant",
                "tool_calls": [
                    {"function": {"name": (toolsets[0] if toolsets else "noop")}}
                ],
            },
        ],
    }


def show_child_brief(parent: ParentAgent, spec_kwargs: Dict[str, Any]) -> None:
    """Print the isolated brief a child WOULD receive, to make isolation visible."""
    from subagent_delegation import DelegateSpec, build_child_agent

    spec = DelegateSpec(**spec_kwargs)
    child = build_child_agent(0, spec, parent)
    print("  --- child's isolated system prompt (brief) ---")
    for line in child.system_prompt.splitlines():
        print(f"  | {line}")
    print(f"  --- restricted toolset: {child.toolsets} ---")
    print(f"  --- effective role: {child.role.value}, depth: {child.depth} ---")


def main() -> None:
    # The parent agent. Its `history` holds a SECRET that must never reach a child.
    parent = ParentAgent(
        model="hermes-parent",
        enabled_toolsets=["terminal", "file", "web", "delegation"],
        delegate_depth=0,
        run_agent=fake_child_brain,
        history=[{"role": "user", "content": "PARENT_SECRET: do not leak me"}],
    )

    # -------------------------------------------------------------------
    # 1) SINGLE delegation — show the isolated brief, then run it.
    # -------------------------------------------------------------------
    banner("1) SINGLE DELEGATION (runs directly)")
    single_spec = {
        "goal": "Summarize the README of the repo in 3 bullet points.",
        "context": "The repo is a CLI tool. Keep it non-technical.",
        # Request 'web' + a forbidden 'memory' toolset to show restriction.
        "toolsets": ["file", "web", "memory"],
    }
    show_child_brief(parent, single_spec)

    out = delegate_task(parent, **single_spec)
    data = json.loads(out)
    r = data["results"][0]
    print(f"\n  RESULT status={r['status']} exit={r['exit_reason']} "
          f"dur={r['duration_seconds']}s tools_used={[t['tool'] for t in r['tool_trace']]}")
    print(f"  SUMMARY -> {r['summary']}")
    print("  NOTE: requested 'memory' was stripped (blocked); kept file+web "
          "(intersected with parent).")

    # -------------------------------------------------------------------
    # 2) BATCH fan-out — 3 independent subtasks in parallel.
    # -------------------------------------------------------------------
    banner("2) BATCH FAN-OUT (3 subtasks, parallel thread pool)")
    tasks = [
        {"goal": "Research approach A for caching.", "toolsets": ["web"]},
        {"goal": "Research approach B for caching.", "toolsets": ["web"]},
        {"goal": "Draft a comparison table of A vs B.", "toolsets": ["file"]},
    ]
    t0 = time.monotonic()
    out = delegate_task(parent, tasks=tasks)
    wall = round(time.monotonic() - t0, 3)
    data = json.loads(out)
    print(f"  fanned out {len(tasks)} children; wall={wall}s "
          f"(serial would be ~{0.05 * len(tasks):.2f}s+)")
    for r in data["results"]:
        print(f"  [{r['task_index']}] {r['status']:<9} ({r['duration_seconds']}s)  "
              f"{r['summary']}")
    print(f"  total_duration_seconds reported by tool: {data['total_duration_seconds']}")

    # -------------------------------------------------------------------
    # 3) MIXTURE OF AGENTS — N reference agents, then synthesis.
    # -------------------------------------------------------------------
    banner("3) MIXTURE OF AGENTS (fan-out N references -> aggregate)")

    def make_ref(style: str):
        def _ref(*, user_message, system_prompt, toolsets, task_id):
            time.sleep(0.05)
            return {
                "final_response": f"({style} view) {user_message} -> answer flavored by {style}.",
                "completed": True,
                "api_calls": 1,
                "messages": [],
            }
        return _ref

    def aggregator(*, user_message, system_prompt, toolsets, task_id):
        # The aggregator's system prompt carries the enumerated reference answers.
        n_refs = system_prompt.count("\n1.") + system_prompt.count("\n2.") \
            + system_prompt.count("\n3.")
        return {
            "final_response": (
                f"SYNTHESIS of {n_refs} reference responses for: '{user_message}'. "
                f"Merged the strongest points into one coherent answer."
            ),
            "completed": True,
            "api_calls": 1,
            "messages": [],
        }

    moa = mixture_of_agents(
        "Prove that the sum of the first n odd numbers is n^2.",
        reference_run_agents=[make_ref("algebraic"), make_ref("inductive"), make_ref("geometric")],
        aggregator_run_agent=aggregator,
        reference_models=["claude-opus", "gemini-pro", "gpt-pro"],
        aggregator_model="claude-opus",
    )
    print(f"  success={moa.success}  reference_models={moa.reference_models}")
    print("  --- per-reference responses (isolated, parallel) ---")
    for i, resp in enumerate(moa.reference_responses, 1):
        print(f"  {i}. {resp}")
    print("  --- aggregated answer ---")
    print(f"  {moa.response}")

    # -------------------------------------------------------------------
    # 4) DEPTH GUARD — a child already at max depth cannot delegate.
    # -------------------------------------------------------------------
    banner("4) DEPTH GUARD (a max-depth parent cannot re-delegate)")
    deep_parent = ParentAgent(delegate_depth=2, run_agent=fake_child_brain)  # MAX_SPAWN_DEPTH=2
    out = delegate_task(deep_parent, goal="try to delegate from too deep")
    print(f"  {json.loads(out)}")

    print("\n" + SEP)
    print("DONE. All flows executed offline with a fake child brain.")
    print(SEP)


if __name__ == "__main__":
    main()
