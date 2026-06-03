"""
code_execution_rpc — A stdlib-only mirror of Hermes Agent's "code execution via
RPC" capability: the agent writes a Python script that calls its own tools as
RPC stubs, collapsing a multi-step tool pipeline into a single agent turn.

Public API:
    ToolGateway        - brokers tool calls coming from inside a script
    execute_code       - runs an agent-authored script in a controlled namespace
    ExecuteCodeResult  - structured result (status/output/tool_calls_made/...)
    ToolSpec           - a registered tool's metadata + handler
    RpcRequest         - one tool-call request envelope
    RpcCallLogEntry    - per-call observability record

Source mirrored: Nous Research Hermes Agent
    tools/code_execution_tool.py, tools/managed_tool_gateway.py, tools/tool_search.py
"""

from code_execution_rpc.code_execution_rpc import (
    DEFAULT_MAX_TOOL_CALLS,
    DEFAULT_TIMEOUT,
    MAX_STDOUT_BYTES,
    ExecuteCodeResult,
    RpcCallLogEntry,
    RpcRequest,
    ToolGateway,
    ToolHandler,
    ToolSpec,
    ToolsNamespace,
    execute_code,
)

__all__ = [
    "DEFAULT_MAX_TOOL_CALLS",
    "DEFAULT_TIMEOUT",
    "MAX_STDOUT_BYTES",
    "ExecuteCodeResult",
    "RpcCallLogEntry",
    "RpcRequest",
    "ToolGateway",
    "ToolHandler",
    "ToolSpec",
    "ToolsNamespace",
    "execute_code",
]
