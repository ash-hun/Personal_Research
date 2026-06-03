"""Hermes Agent — tool_system mirror.

A self-contained, stdlib-only distillation of the Hermes Agent action layer:
tool definition, registry, toolset assembly, dispatch/execute, guardrails,
human approval, and result classification.

See tool_system.py for the mechanism and demo.py for a runnable walkthrough.
"""

from .tool_system import (
    # tool definition + registry
    ToolEntry,
    ToolRegistry,
    registry,
    # toolsets
    TOOLSETS,
    get_toolset,
    resolve_toolset,
    # result classification
    FILE_MUTATING_TOOL_NAMES,
    file_mutation_result_landed,
    classify_tool_failure,
    # guardrails
    ToolCallGuardrailConfig,
    ToolCallSignature,
    ToolGuardrailDecision,
    ToolCallGuardrailController,
    toolguard_synthetic_result,
    append_toolguard_guidance,
    # approval
    ApprovalResult,
    ApprovalController,
    detect_hardline_command,
    detect_dangerous_command,
    # dispatch + execute
    ToolCall,
    ToolResultMessage,
    make_tool_result_message,
    ToolExecutor,
)

__all__ = [
    "ToolEntry",
    "ToolRegistry",
    "registry",
    "TOOLSETS",
    "get_toolset",
    "resolve_toolset",
    "FILE_MUTATING_TOOL_NAMES",
    "file_mutation_result_landed",
    "classify_tool_failure",
    "ToolCallGuardrailConfig",
    "ToolCallSignature",
    "ToolGuardrailDecision",
    "ToolCallGuardrailController",
    "toolguard_synthetic_result",
    "append_toolguard_guidance",
    "ApprovalResult",
    "ApprovalController",
    "detect_hardline_command",
    "detect_dangerous_command",
    "ToolCall",
    "ToolResultMessage",
    "make_tool_result_message",
    "ToolExecutor",
]
