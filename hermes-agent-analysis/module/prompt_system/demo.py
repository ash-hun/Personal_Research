"""Runnable demo of the Hermes prompt_system mirror.

    python3 demo.py

Feeds fake tools / skills / memory / env into the PromptBuilder, then prints:
  1. the ordered sections (with tier + cacheable flag),
  2. the assembled system prompt with section boundaries,
  3. where the up-to-4 Anthropic cache breakpoints land,
  4. a turn-2 view showing the stable prefix is byte-identical (cache stays warm).

No external dependencies.
"""

from __future__ import annotations

import datetime as _dt

from prompt_system import (
    PromptBuilder,
    ToolSpec,
    SkillSpec,
    MemorySnapshot,
    EnvHints,
    build_prompt,
    render_system_text,
)

SEP = "=" * 72


def _banner(title: str) -> None:
    print("\n" + SEP)
    print(title)
    print(SEP)


def make_fixtures():
    tools = [
        ToolSpec("terminal", "Run shell commands"),
        ToolSpec("read_file", "Read a file"),
        ToolSpec("memory", "Persist durable facts"),
        ToolSpec("session_search", "Search past sessions"),
        ToolSpec("skill_manage", "Create/patch skills"),
    ]
    skills = [
        SkillSpec("hermes-agent", "Configure Hermes itself", category="meta"),
        SkillSpec("pdf-extract", "Extract text/tables from PDFs", category="docs"),
        SkillSpec("git-triage", "Triage failing CI on a PR", category="dev"),
    ]
    memory = MemorySnapshot(
        memory_facts=[
            "User prefers concise responses.",
            "Project uses pytest with xdist.",
        ],
        user_profile="Name: Researcher. Timezone: Asia/Seoul.",
    )
    env = EnvHints(
        host="macOS (15.5)",
        home="/Users/researcher",
        cwd="/Users/researcher/proj",
        platform="telegram",
    )
    context_files = {
        "AGENTS.md": "Use 4-space indent. Run `make test` before committing.",
    }
    return tools, skills, memory, env, context_files


def main() -> int:
    tools, skills, memory, env, context_files = make_fixtures()

    # gpt-family model name -> tool-use enforcement guidance gets injected.
    builder = PromptBuilder(model="gpt-5-codex", provider="openai")

    conversation = [
        {"role": "user", "content": "Extract the tables from report.pdf."},
        {"role": "assistant", "content": "On it — loading the pdf-extract skill."},
        {"role": "user", "content": "Now summarize page 3."},
    ]

    built = build_prompt(
        builder,
        tools=tools,
        skills=skills,
        memory=memory,
        env=env,
        system_message="Be extra careful with financial figures.",
        context_files=context_files,
        conversation=conversation,
        cache_ttl="5m",
        now=_dt.datetime(2026, 6, 3),
    )

    # 1) Ordered sections -----------------------------------------------------
    _banner("1) ORDERED SECTIONS  (tier ordering: stable -> context -> volatile)")
    for i, s in enumerate(built.sections):
        flag = "CACHEABLE" if s.cacheable else "DYNAMIC  "
        print(f"  [{i:>2}] {flag} | {s.tier:<8} | {s.name:<22} | {len(s.text):>4} chars")

    # 2) Assembled prompt with boundaries ------------------------------------
    _banner("2) ASSEMBLED SYSTEM PROMPT  (--- marks section boundaries ---)")
    for s in built.sections:
        print(f"\n----- [{s.tier}] {s.name} -----")
        preview = s.text if len(s.text) <= 320 else s.text[:317] + "..."
        print(preview)

    # 3) Cache breakpoints ----------------------------------------------------
    _banner("3) CACHE BREAKPOINTS  (system_and_3: system + last 3 messages)")
    print(f"Total messages: {len(built.messages)}  |  breakpoints used: {len(built.breakpoints)}/4")
    print(f"Cacheable stable+context prefix: {built.stable_prefix_chars} chars")
    print(f"Volatile tail (re-sent each turn): "
          f"{len(built.system_text) - built.stable_prefix_chars} chars\n")
    for bp in built.breakpoints:
        print(f"  msg[{bp.message_index}] role={bp.role:<9} ttl={bp.ttl}  -> {bp.reason}")

    # Show that markers are actually attached on the wire.
    _banner("3b) cache_control ON THE WIRE  (first/last messages)")
    sys_msg = built.messages[0]
    last_msg = built.messages[-1]
    print("system message content[0] keys:", list(sys_msg["content"][0].keys()))
    print("  cache_control:", sys_msg["content"][0].get("cache_control"))
    print("last message content:", last_msg.get("content"))

    # 4) Turn-2 stability check ----------------------------------------------
    _banner("4) TURN-2 STABILITY  (date-only timestamp -> prefix byte-identical)")
    built2 = build_prompt(
        builder,
        tools=tools, skills=skills, memory=memory, env=env,
        system_message="Be extra careful with financial figures.",
        context_files=context_files,
        conversation=conversation + [
            {"role": "assistant", "content": "Page 3 covers Q2 revenue..."},
        ],
        cache_ttl="5m",
        now=_dt.datetime(2026, 6, 3, 23, 59),  # later same day
    )
    stable1 = render_system_text([s for s in built.sections if s.tier == "stable"])
    stable2 = render_system_text([s for s in built2.sections if s.tier == "stable"])
    print("STABLE tier identical across turns:", stable1 == stable2)
    print("(So the upstream prefix KV-cache stays warm; only the volatile "
          "tail + new turn change.)")

    print("\n" + SEP)
    print("DONE")
    print(SEP)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
