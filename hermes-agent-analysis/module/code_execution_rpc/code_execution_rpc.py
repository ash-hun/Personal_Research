"""
code_execution_rpc.py — A self-contained, stdlib-only mirror of Hermes Agent's
"code execution via RPC" capability.

WHAT THIS MIRRORS
-----------------
Hermes (Nous Research) ships a tool called ``execute_code``: the agent writes a
*Python script* that can call the agent's *own tools* programmatically. Instead
of the model emitting one tool-call per step (each round-tripping through the
LLM context, costing tokens + a turn), it emits ONE script that chains many
tool calls with arbitrary Python logic in between (loops, filtering, branching).
The script runs in a child process and talks back to the parent agent over a
small JSON-line RPC protocol; only the script's final stdout/result re-enters
the model's context. This collapses an N-step tool pipeline into a single
zero-extra-context-cost agent turn.

HOW HERMES DOES IT (source: tools/code_execution_tool.py)
---------------------------------------------------------
1. ``generate_hermes_tools_module(enabled_tools)`` emits a tiny ``hermes_tools.py``
   source file containing one *stub function* per allowed tool (see ``_TOOL_STUBS``
   and ``SANDBOX_ALLOWED_TOOLS``). Each stub body is just::

       def web_search(query, limit=5):
           return _call('web_search', {"query": query, "limit": limit})

2. ``_call(tool, args)`` (in ``_UDS_TRANSPORT_HEADER``) serializes
   ``{"tool": ..., "args": ...}\n`` and writes it to a Unix-domain socket
   (or loopback TCP on Windows, or file-based RPC on remote backends).

3. The parent runs ``_rpc_server_loop()`` in a daemon thread: it accepts the
   child's connection, reads newline-delimited requests, enforces an allow-list
   + a max-tool-calls budget, dispatches each request through the *real* tool
   handler (``model_tools.handle_function_call``), and writes the JSON result
   back with a trailing ``\n``.

4. ``execute_code()`` stages the generated module + the agent's script into a
   temp dir, spawns ``python script.py`` in a *scrubbed* environment (secrets
   stripped, ``HERMES_RPC_SOCKET`` injected), captures stdout/stderr with
   head+tail truncation, and returns a JSON blob:
   ``{"status", "output", "tool_calls_made", "duration_seconds"}``.

WHAT THIS MIRROR CHANGES (and why it's still faithful)
------------------------------------------------------
A real subprocess + socket round-trip is overkill for a teaching mirror and
hurts portability. Here the "script runtime" is simulated with ``exec()`` over a
*controlled namespace*: the script gets a ``tools`` object (and importable
``hermes_tools``-style callables) whose every attribute access returns an RPC
stub that round-trips through an in-process ``ToolGateway``. The gateway plays
the role of ``_rpc_server_loop`` — it owns the allow-list, the call budget, the
call log, and the real handler dispatch. The data flow (stub -> serialize ->
gateway dispatch -> result back) and the I/O contract are preserved.

  *** SECURITY NOTE ***
  exec() in the same process is NOT a sandbox. Real Hermes isolates the script
  in a child process with a scrubbed env (no API keys/tokens), an allow-list,
  a tool-call budget, output redaction, and ANSI stripping. See README.md
  "보안 주의" for the mapping. Do not run untrusted scripts through this mirror.
"""

from __future__ import annotations

import io
import json
import time
import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Configuration constants
# Mirror of: tools/code_execution_tool.py SANDBOX_ALLOWED_TOOLS + resource limits
# ---------------------------------------------------------------------------

#: Hermes only exposes a curated subset of tools to sandbox scripts. Real value:
#: {web_search, web_extract, read_file, write_file, search_files, patch, terminal}.
#: Here the allow-list is supplied per-gateway at registration time instead.
DEFAULT_MAX_TOOL_CALLS: int = 50   # mirror of DEFAULT_MAX_TOOL_CALLS
DEFAULT_TIMEOUT: float = 300.0     # mirror of DEFAULT_TIMEOUT (seconds)
MAX_STDOUT_BYTES: int = 50_000     # mirror of MAX_STDOUT_BYTES


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

#: A tool handler takes a JSON-able args dict and returns a JSON-able result.
#: Mirrors the role of ``model_tools.handle_function_call(tool_name, args, ...)``.
ToolHandler = Callable[[Dict[str, Any]], Any]


@dataclass
class ToolSpec:
    """Registration record for one tool the gateway can broker.

    Mirror of an entry in Hermes's ``_TOOL_STUBS`` (the static stub template)
    fused with the runtime registry entry that ``handle_function_call`` resolves.

    Attributes:
        name:      Tool name the script calls, e.g. ``"fetch"``.
        handler:   Real implementation invoked when the script calls the stub.
        signature: Human-readable signature shown by tool discovery, e.g.
                   ``"url: str"`` — mirrors the ``sig`` field of ``_TOOL_STUBS``.
        doc:       One-line docstring surfaced to the agent — mirrors the
                   ``doc`` field of ``_TOOL_STUBS``.
    """

    name: str
    handler: ToolHandler
    signature: str = "**kwargs"
    doc: str = ""


# ---------------------------------------------------------------------------
# RPC request / response envelopes
# Mirror of the newline-delimited JSON protocol in _call() <-> _rpc_server_loop()
# ---------------------------------------------------------------------------

@dataclass
class RpcRequest:
    """One tool-call request sent by a script stub to the gateway.

    Wire form in Hermes: ``json.dumps({"tool": ..., "args": ...}) + "\\n"``.
    """

    tool: str
    args: Dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> str:
        """Serialize exactly as the generated ``_call()`` stub does."""
        return json.dumps({"tool": self.tool, "args": self.args}) + "\n"

    @classmethod
    def from_wire(cls, line: str) -> "RpcRequest":
        """Parse one newline-delimited request, as ``_rpc_server_loop`` does."""
        payload = json.loads(line)
        return cls(tool=payload.get("tool", ""), args=payload.get("args", {}) or {})


@dataclass
class RpcCallLogEntry:
    """Observability record appended per dispatched call.

    Mirror of the dict appended to ``tool_call_log`` inside ``_rpc_server_loop``:
    ``{"tool", "args_preview", "duration"}``.
    """

    tool: str
    args_preview: str
    duration: float


# ---------------------------------------------------------------------------
# The gateway — the parent-side RPC broker
# Mirror of: _rpc_server_loop() (tools/code_execution_tool.py) +
#            the brokering role of tools/managed_tool_gateway.py
# ---------------------------------------------------------------------------

class ToolGateway:
    """Brokers tool calls coming *from inside* an agent-authored script.

    In Hermes this is split across a socket server thread
    (``_rpc_server_loop``) that owns the allow-list / call budget / dispatch,
    and the ``managed_tool_gateway`` that resolves the upstream endpoint. Here
    it is one in-process object: register tools, then ``dispatch()`` each
    request the script's stubs emit.

    Enforcement faithfully mirrored from ``_rpc_server_loop``:
      * allow-list check     -> "Tool '<x>' is not available in execute_code."
      * call-budget check    -> "Tool call limit reached (<n>)."
      * per-call logging      -> ``call_log`` (mirror of ``tool_call_log``)
      * call counter          -> ``tool_calls_made`` (mirror of ``tool_call_counter``)
    """

    def __init__(self, max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS) -> None:
        self._tools: Dict[str, ToolSpec] = {}
        self._max_tool_calls: int = max_tool_calls
        self.tool_calls_made: int = 0
        self.call_log: List[RpcCallLogEntry] = []

    # -- registration -------------------------------------------------------

    def register(
        self,
        name: str,
        handler: ToolHandler,
        *,
        signature: str = "**kwargs",
        doc: str = "",
    ) -> None:
        """Register a tool so scripts may call ``tools.<name>(...)``.

        Mirrors how Hermes adds a tool to ``SANDBOX_ALLOWED_TOOLS`` and gives it
        a stub template in ``_TOOL_STUBS``; registering here is the union of
        both (allow-list membership + stub metadata + real handler).
        """
        self._tools[name] = ToolSpec(
            name=name, handler=handler, signature=signature, doc=doc
        )

    @property
    def allowed_tools(self) -> frozenset:
        """The sandbox allow-list — mirror of ``allowed_tools`` in the loop."""
        return frozenset(self._tools)

    # -- discovery (mirror of tools/tool_search.py dispatch_tool_search) -----

    def describe_tools(self) -> List[Dict[str, str]]:
        """Return stub signatures the agent would see before writing a script.

        Mirror of ``generate_hermes_tools_module`` output / ``tool_search``
        discovery: ``[{"name", "signature", "doc"}, ...]``.
        """
        return [
            {"name": s.name, "signature": f"{s.name}({s.signature})", "doc": s.doc}
            for s in sorted(self._tools.values(), key=lambda t: t.name)
        ]

    # -- dispatch (mirror of _rpc_server_loop body) -------------------------

    def dispatch(self, request: RpcRequest) -> Any:
        """Handle one RPC request from a script stub; return a JSON-able result.

        Faithful port of the per-message body of ``_rpc_server_loop``:
        allow-list gate -> budget gate -> dispatch through the real handler ->
        log + increment counter. Errors are returned as ``{"error": ...}``
        dicts (the script sees them as ordinary return values), exactly like
        the real loop sends an error envelope rather than killing the script.
        """
        # 1) Allow-list enforcement.
        if request.tool not in self._tools:
            available = ", ".join(sorted(self._tools))
            return {
                "error": (
                    f"Tool '{request.tool}' is not available in execute_code. "
                    f"Available: {available}"
                )
            }

        # 2) Tool-call budget enforcement.
        if self.tool_calls_made >= self._max_tool_calls:
            return {
                "error": (
                    f"Tool call limit reached ({self._max_tool_calls}). "
                    "No more tool calls allowed in this execution."
                )
            }

        # 3) Dispatch through the real handler (mirror of handle_function_call).
        call_start = time.monotonic()
        try:
            result = self._tools[request.tool].handler(request.args)
        except Exception as exc:  # noqa: BLE001 - mirror tool_error behavior
            result = {"error": str(exc)}

        # 4) Account + log (mirror of tool_call_counter[0] += 1 / tool_call_log).
        self.tool_calls_made += 1
        self.call_log.append(
            RpcCallLogEntry(
                tool=request.tool,
                args_preview=str(request.args)[:80],
                duration=round(time.monotonic() - call_start, 4),
            )
        )
        return result


# ---------------------------------------------------------------------------
# Script-side RPC stubs
# Mirror of the auto-generated hermes_tools.py: tools.<name>(...) -> _call(...)
# ---------------------------------------------------------------------------

class _ToolStub:
    """A single callable RPC stub bound to one tool name.

    Mirror of one generated stub function in ``hermes_tools.py``. Calling it
    builds an ``RpcRequest`` and round-trips through the gateway — the in-process
    analogue of writing to the Unix-domain socket in ``_call()``.
    """

    def __init__(self, gateway: ToolGateway, name: str) -> None:
        self._gateway = gateway
        self._name = name

    def __call__(self, **kwargs: Any) -> Any:
        # Mirror of: request = {"tool": name, "args": args}; conn.sendall(...).
        request = RpcRequest(tool=self._name, args=kwargs)
        return self._gateway.dispatch(request)

    def __repr__(self) -> str:
        return f"<rpc stub tools.{self._name}>"


class ToolsNamespace:
    """The ``tools`` object injected into a script's runtime.

    Attribute access ``tools.fetch`` returns a ``_ToolStub`` for ``fetch``.
    This is the in-process analogue of ``from hermes_tools import fetch`` —
    every name resolves to an RPC-backed callable, and only allow-listed names
    actually succeed (the gateway rejects unknown tools at dispatch time, so
    even an unknown attribute yields a stub that returns an ``{"error": ...}``).
    """

    def __init__(self, gateway: ToolGateway) -> None:
        object.__setattr__(self, "_gateway", gateway)

    def __getattr__(self, name: str) -> _ToolStub:
        # Called only for names not found normally -> always an RPC stub.
        return _ToolStub(object.__getattribute__(self, "_gateway"), name)

    def __dir__(self) -> List[str]:
        return sorted(object.__getattribute__(self, "_gateway").allowed_tools)


# ---------------------------------------------------------------------------
# execute_code — the tool entry point
# Mirror of: tools/code_execution_tool.py execute_code()
# ---------------------------------------------------------------------------

@dataclass
class ExecuteCodeResult:
    """Structured result of running an agent-authored script.

    Mirror of the JSON dict returned by ``execute_code()``:
    ``{"status", "output", "tool_calls_made", "duration_seconds"}`` plus an
    optional ``error`` and the RPC call log for inspection.

    Attributes:
        status:           "success" | "error".
        output:           Captured stdout (head+tail truncated to MAX_STDOUT_BYTES).
        tool_calls_made:  Count of RPC tool calls the script performed.
        duration_seconds: Wall-clock execution time.
        error:            Traceback / message when status == "error".
        call_log:         Per-call observability records (mirror tool_call_log).
    """

    status: str
    output: str
    tool_calls_made: int
    duration_seconds: float
    error: Optional[str] = None
    call_log: List[RpcCallLogEntry] = field(default_factory=list)

    def to_json(self) -> str:
        """Serialize like ``execute_code`` does (``json.dumps(result)``)."""
        return json.dumps(
            {
                "status": self.status,
                "output": self.output,
                "tool_calls_made": self.tool_calls_made,
                "duration_seconds": self.duration_seconds,
                **({"error": self.error} if self.error else {}),
            },
            ensure_ascii=False,
        )


def _truncate_head_tail(text: str, max_bytes: int = MAX_STDOUT_BYTES) -> str:
    """Keep the first 40% and last 60% of stdout, eliding the middle.

    Mirror of the head+tail truncation in ``execute_code``'s ``_drain_head_tail``
    (so the final ``print()`` is never lost even on huge output).
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    head_bytes = int(max_bytes * 0.4)
    tail_bytes = max_bytes - head_bytes
    head = raw[:head_bytes].decode("utf-8", errors="replace")
    tail = raw[-tail_bytes:].decode("utf-8", errors="replace")
    omitted = len(raw) - head_bytes - tail_bytes
    notice = (
        f"\n\n... [OUTPUT TRUNCATED - {omitted:,} chars omitted "
        f"out of {len(raw):,} total] ...\n\n"
    )
    return head + notice + tail


def execute_code(
    code: str,
    gateway: ToolGateway,
    timeout: float = DEFAULT_TIMEOUT,
) -> ExecuteCodeResult:
    """Run an agent-authored Python script that calls tools via the gateway.

    This is the mirror of Hermes's ``execute_code(code, task_id, enabled_tools)``.
    Faithful pieces preserved:
      * The script runs in a *controlled namespace* (here via ``exec`` instead
        of a child process — see module docstring's SECURITY NOTE).
      * A ``tools`` object exposes every registered tool as an RPC stub; calling
        ``tools.fetch(url=...)`` round-trips through ``gateway.dispatch``.
      * stdout is captured and head+tail truncated to ``MAX_STDOUT_BYTES``.
      * The return value reports ``status``, ``output``, ``tool_calls_made``,
        ``duration_seconds`` — the same contract the model receives.

    Args:
        code:    The Python source the agent wrote (its single turn's payload).
        gateway: The ``ToolGateway`` brokering the script's tool calls.
        timeout: Advisory wall-clock budget (mirror of DEFAULT_TIMEOUT; not
                 enforced as a hard kill in this in-process mirror).

    Returns:
        ExecuteCodeResult — JSON-serializable via ``.to_json()``.
    """
    if not code or not code.strip():
        return ExecuteCodeResult(
            status="error", output="", tool_calls_made=0,
            duration_seconds=0.0, error="No code provided.",
            call_log=gateway.call_log,
        )

    tools_ns = ToolsNamespace(gateway)

    # The controlled namespace given to the script. In Hermes the script imports
    # `from hermes_tools import web_search, ...`; here we pre-bind a `tools`
    # object plus the convenience helpers Hermes ships in `_COMMON_HELPERS`.
    script_globals: Dict[str, Any] = {
        "__name__": "__hermes_sandbox__",
        "__builtins__": __builtins__,  # NOTE: full builtins — see SECURITY NOTE
        "tools": tools_ns,
        "json": json,
        # Mirror of hermes_tools convenience helpers (json_parse, retry, ...):
        "json_parse": lambda text: json.loads(text, strict=False),
    }

    exec_start = time.monotonic()
    stdout_buffer = io.StringIO()
    status = "success"
    error: Optional[str] = None

    try:
        with contextlib.redirect_stdout(stdout_buffer):
            exec(compile(code, "<agent_script>", "exec"), script_globals)  # noqa: S102
    except Exception as exc:  # noqa: BLE001 - capture like execute_code does
        import traceback
        status = "error"
        error = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))

    duration = round(time.monotonic() - exec_start, 4)
    output = _truncate_head_tail(stdout_buffer.getvalue())
    if status == "error" and error:
        # Mirror: include stderr/traceback in output so the model sees it.
        output = output + "\n--- stderr ---\n" + error

    return ExecuteCodeResult(
        status=status,
        output=output,
        tool_calls_made=gateway.tool_calls_made,
        duration_seconds=duration,
        error=error,
        call_log=gateway.call_log,
    )
