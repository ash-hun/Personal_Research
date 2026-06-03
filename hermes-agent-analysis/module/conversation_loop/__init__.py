"""Hermes Agent conversation-loop mirror — public exports.

Self-contained, stdlib-only distillation of the Hermes agent turn loop.
See conversation_loop.py for the mechanism and README.md for the writeup.
"""

from .conversation_loop import (
    AIAgent,
    AssistantMessage,
    ConversationResult,
    GuardrailDecision,
    IterationBudget,
    ModelClient,
    PARTIAL_STREAM_STUB_ID,
    StopReason,
    Tool,
    ToolCall,
    ToolResult,
    run_conversation,
)

__all__ = [
    "AIAgent",
    "AssistantMessage",
    "ConversationResult",
    "GuardrailDecision",
    "IterationBudget",
    "ModelClient",
    "PARTIAL_STREAM_STUB_ID",
    "StopReason",
    "Tool",
    "ToolCall",
    "ToolResult",
    "run_conversation",
]
