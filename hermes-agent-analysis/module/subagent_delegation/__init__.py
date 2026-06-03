"""
subagent_delegation — a stdlib-only mirror of the Hermes Agent (Nous Research)
subagent-delegation feature: the `delegate_task` tool (isolated child spawn, parallel
fan-out, result collection) and the Mixture-of-Agents pattern.

See subagent_delegation.py for the distilled implementation and README.md (Korean)
for the source mapping and data flow. Run `python3 demo.py` for a live walkthrough.
"""

from .subagent_delegation import (
    AGGREGATOR_SYSTEM_PROMPT,
    BLOCKED_TOOLSET_NAMES,
    DEFAULT_TOOLSETS,
    MAX_CONCURRENT_CHILDREN,
    MAX_SPAWN_DEPTH,
    ChildAgent,
    DelegateSpec,
    MoAResult,
    ParentAgent,
    Role,
    RunAgent,
    SubagentResult,
    build_child_agent,
    build_child_system_prompt,
    construct_aggregator_prompt,
    delegate_task,
    fake_run_agent,
    mixture_of_agents,
    normalize_role,
    resolve_child_toolsets,
    run_single_child,
    strip_blocked_tools,
)

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
