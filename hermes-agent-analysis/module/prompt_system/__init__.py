"""Hermes Agent prompt_system mirror — stdlib-only, runnable.

Re-exports the public API of the prompt_system mirror so it can be imported
as a package:

    from prompt_system import PromptBuilder, build_prompt, apply_anthropic_cache_control
"""

from .prompt_system import (
    ToolSpec,
    SkillSpec,
    MemorySnapshot,
    EnvHints,
    PromptSection,
    CacheBreakpoint,
    BuiltPrompt,
    PromptBuilder,
    render_system_text,
    apply_anthropic_cache_control,
    build_prompt,
    TIER_ORDER,
    TOOL_USE_ENFORCEMENT_MODELS,
    PLATFORM_HINTS,
    DEFAULT_AGENT_IDENTITY,
)

__all__ = [
    "ToolSpec", "SkillSpec", "MemorySnapshot", "EnvHints",
    "PromptSection", "CacheBreakpoint", "BuiltPrompt",
    "PromptBuilder", "render_system_text",
    "apply_anthropic_cache_control", "build_prompt",
    "TIER_ORDER", "TOOL_USE_ENFORCEMENT_MODELS", "PLATFORM_HINTS",
    "DEFAULT_AGENT_IDENTITY",
]
