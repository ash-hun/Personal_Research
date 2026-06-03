"""Self-contained mirror of Hermes Agent's closed-learning-loop memory system.

Public API mirrors the names in the Hermes source so the mapping stays obvious.
"""

from __future__ import annotations

from .memory_system import (
    # constants
    ENTRY_DELIMITER,
    DEFAULT_MEMORY_CHAR_LIMIT,
    DEFAULT_USER_CHAR_LIMIT,
    DEFAULT_MEMORY_NUDGE_INTERVAL,
    MEMORY_REVIEW_PROMPT,
    MEMORY_SCHEMA,
    CURATOR_REVIEW_PROMPT,
    # store + tool
    MemoryStore,
    ToolResult,
    MemoryToolCall,
    memory_tool,
    # providers + manager
    MemoryProvider,
    BuiltinMemoryProvider,
    MemoryManager,
    build_memory_context_block,
    # nudge
    LLMCallable,
    BackgroundReviewNudge,
    NudgeOutcome,
    # curator
    Curator,
    CuratorDecision,
    CuratorReport,
)

__all__ = [
    "ENTRY_DELIMITER",
    "DEFAULT_MEMORY_CHAR_LIMIT",
    "DEFAULT_USER_CHAR_LIMIT",
    "DEFAULT_MEMORY_NUDGE_INTERVAL",
    "MEMORY_REVIEW_PROMPT",
    "MEMORY_SCHEMA",
    "CURATOR_REVIEW_PROMPT",
    "MemoryStore",
    "ToolResult",
    "MemoryToolCall",
    "memory_tool",
    "MemoryProvider",
    "BuiltinMemoryProvider",
    "MemoryManager",
    "build_memory_context_block",
    "LLMCallable",
    "BackgroundReviewNudge",
    "NudgeOutcome",
    "Curator",
    "CuratorDecision",
    "CuratorReport",
]
