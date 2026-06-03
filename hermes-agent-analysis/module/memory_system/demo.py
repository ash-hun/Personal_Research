"""Runnable demo of the Hermes "closed learning loop" memory mirror.

    $ python3 demo.py

No external deps. Simulates two sessions plus a curator pass so you can watch
the memory store evolve:

    Session 1  — the agent writes memories via the tool (direct + nudged review).
    Curator    — a fake LLM merges/refines redundant entries.
    Session 2  — the manager retrieves relevant memories and injects them.

The LLM is faked: the nudge "LLM" returns memory tool-call JSON; the curator
"LLM" returns consolidation-decision JSON. Both are pure functions of the prompt,
so the demo is deterministic.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import List

from memory_system import (
    BackgroundReviewNudge,
    BuiltinMemoryProvider,
    Curator,
    MemoryManager,
    MemoryStore,
    MemoryToolCall,
    memory_tool,
)


# ---------------------------------------------------------------------------
# Pretty printing helpers
# ---------------------------------------------------------------------------

def banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def dump_store(store: MemoryStore) -> None:
    """Print the live state of both memory targets."""
    for target in ("user", "memory"):
        entries = store._entries_for(target)
        print(f"  [{target}] {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}:")
        for i, e in enumerate(entries):
            print(f"     {i}. {e}")
        if not entries:
            print("     (empty)")


# ---------------------------------------------------------------------------
# Fake LLMs (pluggable, deterministic)
# ---------------------------------------------------------------------------

def fake_nudge_llm(prompt: str) -> str:
    """Stand in for the forked review agent.

    Reads the transcript embedded in the prompt and emits memory tool-call JSON
    for the preferences/persona it "noticed". Returns 'Nothing to save.' if the
    transcript holds nothing memorable.
    """
    p = prompt.lower()
    calls: List[dict] = []
    if "call me jay" in p or "my name is jay" in p:
        calls.append({"action": "add", "target": "user", "content": "User's name is Jay."})
    if "prefer concise" in p or "too verbose" in p:
        calls.append(
            {"action": "add", "target": "user", "content": "User prefers concise, to-the-point answers."}
        )
    if "use tabs" in p:
        calls.append(
            {"action": "add", "target": "memory", "content": "Project convention: indent with tabs, not spaces."}
        )
    if not calls:
        return "Nothing to save."
    return json.dumps(calls)


def fake_curator_llm(prompt: str) -> str:
    """Stand in for the curator review agent.

    Notices the two overlapping user-preference entries about conciseness/style
    and proposes merging them into one richer class-level entry.
    """
    if "concise" in prompt.lower() and "terse" in prompt.lower():
        return json.dumps(
            [
                {
                    "action": "merge",
                    "target": "user",
                    "old_texts": [
                        "User prefers concise, to-the-point answers.",
                        "User dislikes long preambles; wants terse replies.",
                    ],
                    "new_content": (
                        "Communication style: user wants concise, terse, to-the-point answers "
                        "with no long preambles."
                    ),
                    "rationale": "Two entries describe the same preference; fold into one umbrella.",
                }
            ]
        )
    return "[]"


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="hermes_mem_demo_"))
    mem_dir = workdir / "memories"
    print(f"Memory files will live under: {mem_dir}")

    # === SESSION 1 ========================================================
    banner("SESSION 1 — agent writes memories via the tool")

    store = MemoryStore(memory_dir=mem_dir)
    store.load_from_disk()  # cold start: empty
    manager = MemoryManager()
    manager.add_provider(BuiltinMemoryProvider(store))

    # (a) Direct tool calls — the model explicitly persists facts mid-session.
    print("\n-- direct memory tool calls --")
    for call in [
        MemoryToolCall(action="add", target="user", content="User dislikes long preambles; wants terse replies."),
        MemoryToolCall(action="add", target="memory", content="Project convention: indent with tabs, not spaces."),
    ]:
        result = memory_tool(call, store)
        print(f"  memory({call.action}, {call.target}) -> {json.loads(result)['message']}")

    # (b) A simulated conversation, then the periodic NUDGE fires a review.
    transcript = [
        {"role": "user", "content": "Hey, my name is Jay. Also you're too verbose — prefer concise answers."},
        {"role": "assistant", "content": "Got it, Jay."},
    ]
    nudge = BackgroundReviewNudge(manager, fake_nudge_llm, nudge_interval=1)  # interval=1 so it fires now
    print("\n-- background review nudge fires --")
    outcome = nudge.on_user_turn(transcript)
    print(f"  nudge fired={outcome.fired}: {outcome.summary}")
    for r in outcome.results:
        print(f"     write -> {json.loads(r).get('message', json.loads(r).get('error'))}")

    print("\nLive store after session 1:")
    dump_store(store)

    # === CURATOR PASS =====================================================
    banner("CURATOR — periodic review merges/refines redundant memories")
    curator = Curator(store, fake_curator_llm)

    print("\n-- dry run (preview, no mutation) --")
    preview = curator.run_review(dry_run=True)
    print(f"  {preview.summary}")
    for d in preview.decisions:
        print(f"     would {d.action} {d.target}: {d.new_content!r}  ({d.rationale})")

    print("\n-- live run (applies decisions) --")
    report = curator.run_review(dry_run=False)
    print(f"  {report.summary}")

    print("\nLive store after curator:")
    dump_store(store)

    # === SESSION 2 ========================================================
    banner("SESSION 2 — retrieve + inject relevant memories into the prompt")

    # New session = fresh store loaded from the SAME disk files (persistence!).
    store2 = MemoryStore(memory_dir=mem_dir)
    store2.load_from_disk()
    manager2 = MemoryManager()
    manager2.add_provider(BuiltinMemoryProvider(store2))

    print("\nFrozen system-prompt block (injected once, prefix-cache stable):")
    sys_block = manager2.build_system_prompt()
    for line in sys_block.splitlines():
        print("   " + line)

    user_query = "How should you format your replies to me, and what indentation does this project use?"
    print(f"\nUser query: {user_query}")
    injected = manager2.inject_for_turn(user_query)
    print("\nRetrieved + injected <memory-context> for this turn:")
    for line in injected.splitlines():
        print("   " + line)

    print("\nThe agent now starts session 2 already knowing the user is Jay, wants")
    print("terse answers, and that the project uses tabs — without being told again.")

    banner("DONE — closed learning loop completed end to end")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
