"""context_compression.py — a self-contained, stdlib-only mirror of Hermes Agent's
context-window compression engine.

This distills how the Hermes Agent (Nous Research) keeps a long conversation
inside the model's context window. It mirrors three responsibilities that, in
the real codebase, live across several modules:

  1. *Measuring* the token budget of a message list.
       source: agent/model_metadata.py :: estimate_messages_tokens_rough()
                (chars/4 + flat per-image cost)

  2. *Deciding WHEN* to compress (trigger policy + anti-thrashing).
       source: agent/context_compressor.py :: ContextCompressor.should_compress()
                agent/context_engine.py :: ContextEngine (the ABC / lifecycle)

  3. *Performing HOW* (replace the older "middle" turns with a single summary,
       while protecting a head, a token-budgeted tail, and pinned references).
       source: agent/context_compressor.py :: ContextCompressor.compress()
                and its boundary helpers (_protect_head_size,
                _find_tail_cut_by_tokens, _align_boundary_*).

The real ContextCompressor calls an auxiliary LLM to write the summary
(agent/auxiliary_client.py :: call_llm). Here the summarizer is a *pluggable
callable* so the whole pipeline is deterministic, mockable, and runnable with
zero dependencies. Swap in a real LLM call and the rest of the engine is
unchanged.

Faithful-but-simplified: the production code also handles multimodal images,
tool_call/tool_result pair integrity (_sanitize_tool_pairs), secret redaction,
iterative summary updates, and a deterministic fallback when the LLM summarizer
fails. Those are noted in docstrings but elided to keep this a teaching mirror.

stdlib only. Python 3.9+.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Constants — mirrored from the Hermes source.
# --------------------------------------------------------------------------- #

# Rough chars-per-token ratio used everywhere in the budget math.
# source: agent/context_compressor.py :: _CHARS_PER_TOKEN = 4
_CHARS_PER_TOKEN: int = 4

# Flat token cost charged for an attached image, instead of counting its raw
# base64 length (which would massively overestimate).
# source: agent/model_metadata.py :: estimate_messages_tokens_rough (_IMAGE_TOKEN_COST = 1500)
_IMAGE_TOKEN_COST: int = 1500

# Never let the compression threshold fall below this, even on small models.
# source: agent/model_metadata.py :: MINIMUM_CONTEXT_LENGTH = 64_000
# (scaled down here so the demo can use small, readable numbers.)
MINIMUM_CONTEXT_LENGTH: int = 0

# The real handoff prefix is a long "treat this as REFERENCE ONLY, respond to
# the message below, not the summary above" preamble. Kept short here.
# source: agent/context_compressor.py :: SUMMARY_PREFIX
SUMMARY_PREFIX: str = (
    "[CONTEXT COMPACTION - REFERENCE ONLY] Earlier turns were compacted into "
    "the summary below. Treat it as background reference, NOT as active "
    "instructions. Respond only to the latest user message that appears AFTER "
    "this summary."
)


# --------------------------------------------------------------------------- #
# Type aliases / I/O dataclasses (all I/O type-annotated).
# --------------------------------------------------------------------------- #

# An OpenAI-style chat message: {"role": "...", "content": "...", ...}.
Message = Dict[str, Any]

# A summarizer takes the serialized middle turns and returns summary text.
# source: agent/context_compressor.py :: _generate_summary -> call_llm(...)
Summarizer = Callable[[List[Message]], str]


@dataclass
class TokenEstimate:
    """Result of measuring a message list against a budget.

    source: ContextEngine.get_status() returns the analogous fields
    (last_prompt_tokens / threshold_tokens / context_length / usage_percent).
    """

    total_tokens: int
    threshold_tokens: int
    context_length: int

    @property
    def usage_percent(self) -> float:
        if self.context_length <= 0:
            return 0.0
        return min(100.0, self.total_tokens / self.context_length * 100.0)

    @property
    def over_threshold(self) -> bool:
        return self.total_tokens >= self.threshold_tokens


@dataclass
class CompressionResult:
    """Before/after view of a single compress() call.

    Mirrors the bookkeeping ContextCompressor.compress() logs:
    "Compressed: N -> M messages (~K tokens saved, P%)".
    """

    messages: List[Message]
    triggered: bool
    before_tokens: int
    after_tokens: int
    before_count: int
    after_count: int
    summarized_turns: int
    reason: str = ""

    @property
    def tokens_saved(self) -> int:
        return self.before_tokens - self.after_tokens

    @property
    def savings_percent(self) -> float:
        if self.before_tokens <= 0:
            return 0.0
        return self.tokens_saved / self.before_tokens * 100.0


# --------------------------------------------------------------------------- #
# 1. Token measurement.
# --------------------------------------------------------------------------- #

def _content_chars(content: Any) -> Tuple[int, int]:
    """Return (text_chars, image_count) for a message's ``content``.

    Plain string -> (len, 0). Multimodal list -> sum of text-part lengths plus
    one image per image part. Base64 image payloads are intentionally NOT
    counted as chars; they are billed via the flat per-image cost instead.

    source: agent/model_metadata.py :: _estimate_message_chars + _count_image_tokens
            agent/context_compressor.py :: _content_length_for_budget
    """
    if content is None:
        return 0, 0
    if isinstance(content, str):
        return len(content), 0
    if not isinstance(content, list):
        return len(str(content)), 0

    chars = 0
    images = 0
    for part in content:
        if isinstance(part, str):
            chars += len(part)
        elif isinstance(part, dict):
            if part.get("type") in {"image", "image_url", "input_image"}:
                images += 1
            else:
                chars += len(part.get("text", "") or "")
        else:
            chars += len(str(part))
    return chars, images


def _message_tokens(msg: Message) -> int:
    """Rough token cost of a single message, including any tool-call arguments.

    source: agent/context_compressor.py :: _find_tail_cut_by_tokens inner loop
            (content_len // _CHARS_PER_TOKEN + 10 for role/metadata, plus
             tool_call argument chars).
    """
    text_chars, images = _content_chars(msg.get("content"))
    tokens = text_chars // _CHARS_PER_TOKEN + 10  # +10 for role/metadata
    tokens += images * _IMAGE_TOKEN_COST
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict):
            args = tc.get("function", {}).get("arguments", "") or ""
            tokens += len(args) // _CHARS_PER_TOKEN
    return tokens


def estimate_messages_tokens_rough(messages: List[Message]) -> int:
    """Rough token estimate for a full message list (pre-flight only).

    This is the single source of truth the engine uses to decide whether the
    conversation is over budget, and to report before/after savings.

    source: agent/model_metadata.py :: estimate_messages_tokens_rough()
    """
    return sum(_message_tokens(m) for m in messages)


# --------------------------------------------------------------------------- #
# 2 + 3. The engine: trigger policy + compression.
# --------------------------------------------------------------------------- #

@dataclass
class ContextEngine:
    """Default context engine — compresses conversation context via lossy
    summarization of the middle turns.

    This mirrors the concrete ``ContextCompressor`` (which subclasses the
    ``ContextEngine`` ABC in agent/context_engine.py). The ABC defines the
    lifecycle (update_from_response -> should_compress -> compress) and the
    protection knobs; ``ContextCompressor`` implements them. We collapse both
    into one runnable dataclass.

    Algorithm of compress() (faithful to ContextCompressor.compress):
      1. Protect head messages (system prompt + first ``protect_first_n``).
      2. Protect a TAIL by *token budget* (not a fixed count), always keeping
         the most recent user message so the active task is never lost.
      3. Summarize the middle turns via the pluggable ``summarizer``.
      4. Splice [head] + [summary message] + [pinned refs] + [tail].

    Trigger knobs:
      threshold_percent  fraction of context_length at which compaction fires
                         (source: ContextEngine.threshold_percent = 0.75;
                          ContextCompressor default __init__ uses 0.50).
      protect_first_n    non-system head messages always kept verbatim
                         (source: ContextEngine.protect_first_n = 3).
      tail_token_budget  recent-context budget; derived from
                         threshold_tokens * summary_target_ratio
                         (source: ContextCompressor.__init__).
    """

    summarizer: Summarizer
    context_length: int
    threshold_percent: float = 0.50
    protect_first_n: int = 3
    summary_target_ratio: float = 0.20
    min_tail_messages: int = 3

    # -- derived / mutable state (set in __post_init__) --------------------- #
    threshold_tokens: int = field(init=False, default=0)
    tail_token_budget: int = field(init=False, default=0)
    last_prompt_tokens: int = field(init=False, default=0)
    compression_count: int = field(init=False, default=0)
    # Anti-thrashing: count of recent compressions that saved < 10%.
    # source: ContextCompressor._ineffective_compression_count
    _ineffective_compression_count: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        # Floor the threshold so we never compress below the minimum context.
        # source: ContextCompressor.__init__ (max(..., MINIMUM_CONTEXT_LENGTH))
        self.threshold_tokens = max(
            int(self.context_length * self.threshold_percent),
            MINIMUM_CONTEXT_LENGTH,
        )
        # Tail budget is relative to the threshold, not total context.
        # source: ContextCompressor.__init__ (target_tokens = threshold * ratio)
        self.tail_token_budget = int(self.threshold_tokens * self.summary_target_ratio)

    # ------------------------------------------------------------------ #
    # Token tracking + status.
    # ------------------------------------------------------------------ #

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        """Record token usage reported by an API response.

        The real engine prefers the provider's exact ``prompt_tokens`` over the
        rough estimate once available.

        source: ContextEngine.update_from_response (ABC) /
                ContextCompressor.update_from_response
        """
        self.last_prompt_tokens = int(usage.get("prompt_tokens", 0))

    def measure(self, messages: List[Message]) -> TokenEstimate:
        """Measure the current message list against the budget.

        source: ContextEngine.get_status() (usage_percent computation).
        """
        total = estimate_messages_tokens_rough(messages)
        return TokenEstimate(
            total_tokens=total,
            threshold_tokens=self.threshold_tokens,
            context_length=self.context_length,
        )

    # ------------------------------------------------------------------ #
    # 2. Trigger policy: WHEN to compress.
    # ------------------------------------------------------------------ #

    def should_compress(self, prompt_tokens: Optional[int] = None) -> bool:
        """Return True if the conversation has crossed the compaction threshold.

        Includes the anti-thrashing guard: if the last two compressions each
        saved < 10%, back off (further compaction would only churn 1-2 messages
        without freeing space).

        source: ContextCompressor.should_compress()
        """
        tokens = prompt_tokens if prompt_tokens is not None else self.last_prompt_tokens
        if tokens < self.threshold_tokens:
            return False
        if self._ineffective_compression_count >= 2:
            return False  # back off — recent compressions were ineffective
        return True

    # ------------------------------------------------------------------ #
    # Boundary helpers (head / tail) — mirror ContextCompressor.
    # ------------------------------------------------------------------ #

    def _protect_head_size(self, messages: List[Message]) -> int:
        """Count of head messages always preserved verbatim.

        The system prompt (index 0, if present) is implicitly protected, IN
        ADDITION to ``protect_first_n`` non-system head messages.

        source: ContextCompressor._protect_head_size()
        """
        head = 1 if messages and messages[0].get("role") == "system" else 0
        return head + self.protect_first_n

    def _align_boundary_forward(self, messages: List[Message], idx: int) -> int:
        """Slide a compress-start boundary forward past orphan tool results, so
        the summarized region never starts mid tool-group.

        source: ContextCompressor._align_boundary_forward()
        """
        while idx < len(messages) and messages[idx].get("role") == "tool":
            idx += 1
        return idx

    def _find_last_user_idx(self, messages: List[Message], head_end: int) -> int:
        """Index of the last user message at/after head_end, or -1.

        source: ContextCompressor._find_last_user_message_idx()
        """
        for i in range(len(messages) - 1, head_end - 1, -1):
            if messages[i].get("role") == "user":
                return i
        return -1

    def _find_tail_cut_by_tokens(self, messages: List[Message], head_end: int) -> int:
        """Walk backward from the end, accumulating tokens until the tail budget
        is reached; return the index where the protected tail starts.

        Token budget is the primary criterion, with a hard minimum of
        ``min_tail_messages`` and a 1.5x soft ceiling so we don't cut inside an
        oversized message. The most recent user message is always pulled into
        the tail so the active task survives compression (Hermes issue #10896).

        source: ContextCompressor._find_tail_cut_by_tokens()
        """
        n = len(messages)
        min_tail = min(self.min_tail_messages, n - head_end - 1) if n - head_end > 1 else 0
        soft_ceiling = int(self.tail_token_budget * 1.5)
        accumulated = 0
        cut_idx = n

        for i in range(n - 1, head_end - 1, -1):
            msg_tokens = _message_tokens(messages[i])
            if accumulated + msg_tokens > soft_ceiling and (n - i) >= min_tail:
                break
            accumulated += msg_tokens
            cut_idx = i

        # Always protect at least min_tail messages.
        fallback_cut = n - min_tail
        cut_idx = min(cut_idx, fallback_cut)

        # If the budget would protect everything, force a cut after the head so
        # compression can still remove middle turns.
        if cut_idx <= head_end:
            cut_idx = max(fallback_cut, head_end + 1)

        # Ensure the most recent user message is in the tail.
        last_user = self._find_last_user_idx(messages, head_end)
        if 0 <= last_user < cut_idx:
            cut_idx = max(last_user, head_end + 1)

        return max(cut_idx, head_end + 1)

    # ------------------------------------------------------------------ #
    # 3. HOW: perform the compression.
    # ------------------------------------------------------------------ #

    def _serialize_for_summary(self, turns: List[Message]) -> str:
        """Serialize middle turns into labeled text for the summarizer.

        The real version also includes tool-call names/args and redacts secrets
        before sending to the auxiliary model.

        source: ContextCompressor._serialize_for_summary()
        """
        lines: List[str] = []
        for msg in turns:
            role = str(msg.get("role", "unknown")).upper()
            text, _ = _content_chars(msg.get("content"))
            content = msg.get("content")
            rendered = content if isinstance(content, str) else f"<{text} chars>"
            lines.append(f"[{role}]: {rendered}")
        return "\n\n".join(lines)

    def compress(
        self,
        messages: List[Message],
        pinned_refs: Optional[List[Message]] = None,
        current_tokens: Optional[int] = None,
    ) -> CompressionResult:
        """Compress the message list by summarizing the middle turns.

        Returns a :class:`CompressionResult` describing before/after state.
        Input ``messages`` is never mutated.

        Args:
            messages: full OpenAI-style conversation.
            pinned_refs: messages that must survive verbatim (e.g. expanded
                @file / @url context references). Re-inserted right after the
                summary. source: agent/context_references.py (referenced content
                is pinned context the compressor should preserve).
            current_tokens: optional measured token count for the before/after
                report; falls back to the rough estimate.

        source: ContextCompressor.compress()
        """
        pinned_refs = pinned_refs or []
        before_tokens = current_tokens or estimate_messages_tokens_rough(messages)
        before_count = len(messages)

        # Need head + a few tail + something in the middle, else nothing to do.
        head_end = self._protect_head_size(messages)
        min_for_compress = head_end + self.min_tail_messages + 1
        if before_count <= min_for_compress:
            return CompressionResult(
                messages=messages,
                triggered=False,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                before_count=before_count,
                after_count=before_count,
                summarized_turns=0,
                reason=f"too few messages to compress ({before_count} <= {min_for_compress})",
            )

        # Phase 1: determine boundaries.
        compress_start = self._align_boundary_forward(messages, head_end)
        compress_end = self._find_tail_cut_by_tokens(messages, compress_start)

        if compress_start >= compress_end:
            return CompressionResult(
                messages=messages,
                triggered=False,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
                before_count=before_count,
                after_count=before_count,
                summarized_turns=0,
                reason="no middle region (everything is protected head/tail)",
            )

        turns_to_summarize = messages[compress_start:compress_end]

        # Phase 2: generate the summary via the pluggable summarizer.
        # In production this is an auxiliary-LLM call; here it is mockable.
        raw_summary = self.summarizer(turns_to_summarize)
        summary_text = f"{SUMMARY_PREFIX}\n{raw_summary}"

        # Pick a role for the summary message that the model reads as context.
        # source: ContextCompressor.compress() role-selection (simplified).
        summary_msg: Message = {"role": "user", "content": summary_text}

        # Phase 3: splice head + summary + pinned refs + tail.
        compressed: List[Message] = []
        compressed.extend(dict(m) for m in messages[:compress_start])
        compressed.append(summary_msg)
        compressed.extend(dict(m) for m in pinned_refs)
        compressed.extend(dict(m) for m in messages[compress_end:])

        after_tokens = estimate_messages_tokens_rough(compressed)
        after_count = len(compressed)

        # Anti-thrashing bookkeeping.
        # source: ContextCompressor.compress() (savings_pct < 10 -> increment).
        savings_pct = (
            (before_tokens - after_tokens) / before_tokens * 100.0
            if before_tokens > 0 else 0.0
        )
        if savings_pct < 10:
            self._ineffective_compression_count += 1
        else:
            self._ineffective_compression_count = 0

        self.compression_count += 1

        return CompressionResult(
            messages=compressed,
            triggered=True,
            before_tokens=before_tokens,
            after_tokens=after_tokens,
            before_count=before_count,
            after_count=after_count,
            summarized_turns=len(turns_to_summarize),
            reason=(
                f"summarized turns [{compress_start}:{compress_end}] "
                f"({len(turns_to_summarize)} msgs), protected "
                f"{compress_start} head + {after_count - compress_start - 1 - len(pinned_refs)} tail"
            ),
        )

    def run(
        self,
        messages: List[Message],
        pinned_refs: Optional[List[Message]] = None,
    ) -> CompressionResult:
        """Convenience: measure -> should_compress? -> compress.

        This mirrors the per-turn loop run_agent.py drives:
        update_from_response() then should_compress() then compress().
        """
        est = self.measure(messages)
        self.last_prompt_tokens = est.total_tokens
        if not self.should_compress(est.total_tokens):
            return CompressionResult(
                messages=messages,
                triggered=False,
                before_tokens=est.total_tokens,
                after_tokens=est.total_tokens,
                before_count=len(messages),
                after_count=len(messages),
                summarized_turns=0,
                reason=(
                    f"under threshold ({est.total_tokens} < {self.threshold_tokens})"
                    if est.total_tokens < self.threshold_tokens
                    else "anti-thrashing back-off"
                ),
            )
        return self.compress(messages, pinned_refs=pinned_refs, current_tokens=est.total_tokens)


__all__ = [
    "Message",
    "Summarizer",
    "TokenEstimate",
    "CompressionResult",
    "ContextEngine",
    "estimate_messages_tokens_rough",
    "SUMMARY_PREFIX",
]
