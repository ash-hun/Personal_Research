"""demo.py — runnable demonstration of the context_compression mirror.

    python3 demo.py

No external dependencies. Builds a long fake conversation that exceeds the
budget, runs the engine, shows it trigger compression with a *fake* summarizer,
and prints before/after token counts and message counts.

Source feature: Hermes Agent context compression
  (agent/context_engine.py, agent/context_compressor.py).
"""

from __future__ import annotations

from typing import List

from context_compression import (
    ContextEngine,
    Message,
    estimate_messages_tokens_rough,
)


# --------------------------------------------------------------------------- #
# A fake summarizer. In Hermes this is an auxiliary-LLM call
# (agent/context_compressor.py :: _generate_summary -> call_llm). Here it is a
# deterministic stub so the demo runs offline. Swap it for a real LLM call and
# the engine is unchanged.
# --------------------------------------------------------------------------- #

def fake_summarizer(turns: List[Message]) -> str:
    n = len(turns)
    user_turns = sum(1 for m in turns if m.get("role") == "user")
    asst_turns = sum(1 for m in turns if m.get("role") == "assistant")
    return (
        "## Active Task\nUser is iterating on a data-pipeline refactor.\n\n"
        "## Completed Actions\n"
        f"Compacted {n} earlier turns ({user_turns} user / {asst_turns} assistant) "
        "covering project setup, dependency choices, and several debug cycles.\n\n"
        "## Remaining Work\nContinue from the most recent user request below."
    )


def build_long_conversation(rounds: int) -> List[Message]:
    """A system prompt + first exchange + many verbose middle turns + a recent
    tail ending in a fresh user request."""
    msgs: List[Message] = [
        {"role": "system", "content": "You are a helpful senior engineering assistant. " * 8},
        {"role": "user", "content": "Help me refactor my data pipeline into modular stages."},
        {"role": "assistant", "content": "Sure. Let's start by mapping the current stages. " * 6},
        {"role": "user", "content": "Here's the current code, it's a 400-line script."},
    ]
    for i in range(rounds):
        msgs.append({
            "role": "assistant",
            "content": (
                f"[round {i}] I analyzed the stage and here is a detailed walkthrough "
                "of the parsing, chunking, embedding, and indexing logic with examples. " * 10
            ),
        })
        msgs.append({
            "role": "user",
            "content": f"[round {i}] Looks good, but I hit an error on stage {i}, here is the traceback. " * 6,
        })
    # Recent tail + the latest, still-unanswered user ask.
    msgs.append({"role": "assistant", "content": "Got it, that traceback points to a None guard."})
    msgs.append({"role": "user", "content": "Now add retry logic to the embedding stage."})
    return msgs


def _print_estimate(label: str, est) -> None:
    print(
        f"  {label}: {est.total_tokens:>6} tokens  "
        f"({est.usage_percent:5.1f}% of {est.context_length})  "
        f"threshold={est.threshold_tokens}  over={est.over_threshold}"
    )


def main() -> int:
    # Small context window so the demo numbers stay readable.
    engine = ContextEngine(
        summarizer=fake_summarizer,
        context_length=8_000,
        threshold_percent=0.50,   # compact at 50% -> 4,000 tokens
        protect_first_n=3,
        summary_target_ratio=0.20,
    )

    print("=" * 70)
    print("Hermes context compression — demo")
    print("=" * 70)
    print(
        f"context_length={engine.context_length}  "
        f"threshold_tokens={engine.threshold_tokens}  "
        f"tail_token_budget={engine.tail_token_budget}\n"
    )

    convo = build_long_conversation(rounds=12)

    # ---- BEFORE ---------------------------------------------------------- #
    est_before = engine.measure(convo)
    print("BEFORE compression")
    _print_estimate("usage", est_before)
    print(f"  message count: {len(convo)}")
    print(f"  should_compress -> {engine.should_compress(est_before.total_tokens)}\n")

    # ---- RUN ENGINE ------------------------------------------------------ #
    # Pin a reference (e.g. an expanded @file the user attached) so we can show
    # it survives verbatim through compression.
    pinned = [{"role": "user", "content": "[pinned @file config.yaml] retries: 3, backoff: 2.0"}]
    result = engine.run(convo, pinned_refs=pinned)

    # ---- AFTER ----------------------------------------------------------- #
    print("AFTER compression")
    print(f"  triggered: {result.triggered}")
    print(f"  reason:    {result.reason}")
    print(f"  summarized turns: {result.summarized_turns}")
    print(
        f"  tokens:   {result.before_tokens:>6} -> {result.after_tokens:<6} "
        f"(saved {result.tokens_saved}, {result.savings_percent:.1f}%)"
    )
    print(f"  messages: {result.before_count:>6} -> {result.after_count}\n")

    # ---- INSPECT THE NEW MESSAGE LIST ----------------------------------- #
    print("Resulting message roles (head -> summary -> pinned -> tail):")
    for i, m in enumerate(result.messages):
        preview = m["content"] if isinstance(m["content"], str) else "<multimodal>"
        preview = preview.replace("\n", " ")[:60]
        tag = ""
        if "[CONTEXT COMPACTION" in (m["content"] if isinstance(m["content"], str) else ""):
            tag = "  <-- injected summary"
        if "[pinned @file" in (m["content"] if isinstance(m["content"], str) else ""):
            tag = "  <-- pinned reference (preserved)"
        print(f"  {i:>2} {m['role']:<9} {preview!r}{tag}")

    # ---- ASSERTIONS so the demo self-verifies --------------------------- #
    assert result.triggered, "expected compression to trigger"
    assert result.after_tokens < result.before_tokens, "expected token savings"
    assert result.after_count < result.before_count, "expected fewer messages"
    # System prompt preserved at head.
    assert result.messages[0]["role"] == "system", "system prompt must survive"
    # Pinned reference preserved verbatim somewhere in the result.
    assert any(
        isinstance(m.get("content"), str) and "[pinned @file" in m["content"]
        for m in result.messages
    ), "pinned reference must survive compression"
    # Latest user ask preserved in the tail.
    assert result.messages[-1]["content"] == "Now add retry logic to the embedding stage.", \
        "latest user request must survive in the tail"
    # Post-compression usage is back under threshold.
    est_after = engine.measure(result.messages)
    assert est_after.total_tokens == result.after_tokens

    print("\nPost-compression re-measure:")
    _print_estimate("usage", est_after)
    print(f"  should_compress now -> {engine.should_compress(est_after.total_tokens)}")

    print("\nAll assertions passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
