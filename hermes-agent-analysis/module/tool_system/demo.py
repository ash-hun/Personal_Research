"""
demo.py — exercise the Hermes tool-system mirror end to end.

    python3 demo.py

No external dependencies. This walks through the real action-layer flow:

  1. Define + register 3 fake tools (echo, add, run_command) into toolsets.
  2. Resolve a composed toolset to a flat tool list.
  3. Dispatch a batch of model-emitted tool calls through the executor.
  4. Watch the approval flow intercept a dangerous command.
  5. Watch guardrail loop-detection warn on repeated failures.
  6. See each result classified as success / error / blocked.
"""

from __future__ import annotations

import json

from tool_system import (
    registry,
    resolve_toolset,
    ToolCall,
    ToolExecutor,
    ToolCallGuardrailController,
    ToolCallGuardrailConfig,
    ApprovalController,
)


# ---------------------------------------------------------------------------
# 1. Define three fake tools — name, description, JSON schema, handler.
#    This mirrors how tools/file_tools.py defines READ_FILE_SCHEMA then calls
#    registry.register(name=..., toolset=..., schema=..., handler=...).
# ---------------------------------------------------------------------------

ECHO_SCHEMA = {
    "name": "echo",
    "description": "Echo back the provided text.",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}

ADD_SCHEMA = {
    "name": "add",
    "description": "Add two integers and return the sum as JSON.",
    "parameters": {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
}

RUN_COMMAND_SCHEMA = {
    "name": "run_command",
    "description": "Run a shell command. Dangerous commands require human approval.",
    "parameters": {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    },
}


def echo_handler(text: str) -> str:
    return json.dumps({"echo": text})


def add_handler(a: int, b: int) -> str:
    return json.dumps({"sum": a + b})


def run_command_handler(command: str) -> str:
    # A toy "terminal": pretend any non-empty command succeeds with exit 0,
    # an empty command "fails" with a non-zero exit code so we can show the
    # guardrail loop detection kick in.
    if not command.strip():
        return json.dumps({"exit_code": 1, "error": "empty command"})
    return json.dumps({"exit_code": 0, "stdout": f"(ran) {command}"})


def register_tools() -> None:
    registry.register(name="echo", toolset="full", schema=ECHO_SCHEMA, handler=echo_handler, emoji="🔊")
    registry.register(name="add", toolset="math", schema=ADD_SCHEMA, handler=add_handler, emoji="➕")
    registry.register(name="run_command", toolset="ops", schema=RUN_COMMAND_SCHEMA, handler=run_command_handler, emoji="💻")


def line(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def show_results(messages) -> None:
    for m in messages:
        status = "BLOCKED" if m.blocked else ("ERROR" if m.is_error else "ok")
        print(f"  [{status:7}] {m.tool_name}({m.tool_call_id}) -> {m.content}")


def main() -> None:
    register_tools()

    # -----------------------------------------------------------------------
    # 2. Toolset assembly + composition.
    # -----------------------------------------------------------------------
    line("1) TOOLSET RESOLUTION (composition via 'includes')")
    print("  registered toolsets:", registry.get_registered_toolset_names())
    print("  resolve_toolset('full') ->", resolve_toolset("full"))
    print("  resolve_toolset('all')  ->", resolve_toolset("all"))
    print("  tool defs sent to model:")
    for d in registry.get_tool_definitions(names=["echo", "add", "run_command"]):
        print("    -", d["function"]["name"], ":", d["function"]["description"][:48])

    # -----------------------------------------------------------------------
    # 3. Happy path: dispatch echo + add. Both succeed and are classified ok.
    # -----------------------------------------------------------------------
    line("2) DISPATCH + EXECUTE (happy path)")
    executor = ToolExecutor(registry=registry)
    batch = [
        ToolCall(id="c1", name="echo", arguments=json.dumps({"text": "hello hermes"})),
        ToolCall(id="c2", name="add", arguments=json.dumps({"a": 2, "b": 40})),
        ToolCall(id="c3", name="ghost_tool", arguments="{}"),  # unknown -> blocked
    ]
    show_results(executor.execute_tool_calls(batch))

    # -----------------------------------------------------------------------
    # 4. Approval interception. A dangerous command needs human approval.
    #    First with an auto-deny approver, then with an auto-approve approver.
    # -----------------------------------------------------------------------
    line("3) APPROVAL INTERCEPTION (dangerous command)")

    def deny_approver(command, description):
        print(f"    [approver] '{command}' flagged: {description} -> DENY")
        return "deny"

    def approve_approver(command, description):
        print(f"    [approver] '{command}' flagged: {description} -> APPROVE")
        return "approve"

    danger = [ToolCall(id="d1", name="run_command", arguments=json.dumps({"command": "sudo rm -rf build/"}))]

    exec_deny = ToolExecutor(registry=registry, approvals=ApprovalController(), approval_callback=deny_approver)
    print("  -- approver denies:")
    show_results(exec_deny.execute_tool_calls(danger))

    exec_ok = ToolExecutor(registry=registry, approvals=ApprovalController(), approval_callback=approve_approver)
    print("  -- approver approves:")
    show_results(exec_ok.execute_tool_calls(danger))

    print("  -- hardline command is blocked unconditionally (before any prompt):")
    hardline = [ToolCall(id="d2", name="run_command", arguments=json.dumps({"command": "rm -rf /"}))]
    show_results(exec_ok.execute_tool_calls(hardline))

    # -----------------------------------------------------------------------
    # 5. Guardrail loop detection. Repeat a failing call; watch warnings appear
    #    after the threshold, then a hard BLOCK once hard_stop is enabled.
    # -----------------------------------------------------------------------
    line("4) GUARDRAIL LOOP DETECTION (repeated failure)")
    guard_cfg = ToolCallGuardrailConfig(
        hard_stop_enabled=True,
        exact_failure_warn_after=2,
        exact_failure_block_after=4,
    )
    guarded = ToolExecutor(
        registry=registry,
        guardrails=ToolCallGuardrailController(guard_cfg),
    )
    # Same failing call (empty command -> exit_code 1) repeated 5x in one turn.
    failing = ToolCall(id="f", name="run_command", arguments=json.dumps({"command": ""}))
    for attempt in range(1, 6):
        msg = guarded.execute_tool_calls([ToolCall(id=f"f{attempt}", name="run_command", arguments=failing.arguments)])[0]
        status = "BLOCKED" if msg.blocked else ("ERROR" if msg.is_error else "ok")
        # Surface only the guardrail tail of the content for readability.
        tail = msg.content.split("[Tool loop", 1)
        note = ("  <-- guardrail: " + ("[Tool loop" + tail[1]).strip()) if len(tail) > 1 else ""
        if msg.blocked:
            note = "  <-- guardrail BLOCKED before execution"
        print(f"  attempt {attempt}: [{status}]{note}")

    line("DONE")
    print("  Flow verified: define -> register -> toolset resolve -> dispatch ->")
    print("  guardrail.before_call -> approval -> handler -> classify -> guardrail.after_call.")


if __name__ == "__main__":
    main()
