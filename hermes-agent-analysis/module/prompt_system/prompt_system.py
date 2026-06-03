"""Self-contained, stdlib-only mirror of Hermes Agent's prompt_system feature.

This module distills the REAL mechanism by which Hermes assembles the system
prompt and applies Anthropic prompt caching. It mirrors three reference files
from the Hermes Agent codebase (Nous Research):

  * agent/system_prompt.py   -- build_system_prompt_parts(): the three-tier
                                 (stable / context / volatile) ordering of
                                 prompt sections, joined with "\\n\\n".
  * agent/prompt_builder.py  -- the stateless section builders (identity,
                                 tool guidance, skills index, environment
                                 hints, context files) + the guidance constants.
  * agent/prompt_caching.py  -- apply_anthropic_cache_control(): the
                                 "system_and_3" cache breakpoint strategy
                                 (4 cache_control markers: system + last 3 msgs).

WHY THIS SHAPE (faithful to Hermes):

Hermes builds the system prompt ONCE per session and reuses it verbatim across
all turns. Only a context-compression event triggers a rebuild. This is the
single most important invariant: a byte-stable system prompt keeps the upstream
provider's prefix KV-cache warm, so multi-turn input token cost drops ~75%.

To make that work, sections are grouped into three tiers by how often they
change, and they are emitted in cache-friendliest-first order:

  STABLE   -- identity, tool/skill guidance, skills index, env/platform hints.
              Fixed for the agent's lifetime. This is the big, cacheable prefix.
  CONTEXT  -- caller system_message + cwd-discovered project files (AGENTS.md,
              CLAUDE.md, .cursorrules). Session-stable, may differ per session.
  VOLATILE -- memory snapshot, USER profile, and a DATE-ONLY timestamp line.
              Changes per session/turn; intentionally placed LAST so it never
              invalidates the cacheable prefix in front of it.

Note (faithful detail): the timestamp is date-precision, not minute-precision,
specifically so the whole string stays byte-stable for a full day and does not
bust the prefix cache on every rebuild (Hermes credit: PR #20451).

Everything here is stdlib-only and runnable. The text content of guidance
constants is quoted verbatim (trimmed) from agent/prompt_builder.py so the
researcher can see exactly what Hermes injects and where.
"""

from __future__ import annotations

import copy
import datetime as _dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Guidance constants — verbatim (trimmed) from agent/prompt_builder.py.
# These are the actual strings Hermes injects. Kept here so the mirror is
# self-contained and the researcher can read what each section contributes.
# ---------------------------------------------------------------------------

# agent/prompt_builder.py:121  DEFAULT_AGENT_IDENTITY
DEFAULT_AGENT_IDENTITY = (
    "You are Hermes Agent, an intelligent AI assistant created by Nous Research. "
    "You are helpful, knowledgeable, and direct. You assist users with a wide "
    "range of tasks including answering questions, writing and editing code, "
    "analyzing information, creative work, and executing actions via your tools. "
    "You communicate clearly, admit uncertainty when appropriate, and prioritize "
    "being genuinely useful over being verbose unless otherwise directed below. "
    "Be targeted and efficient in your exploration and investigations."
)

# agent/prompt_builder.py:131  HERMES_AGENT_HELP_GUIDANCE
HERMES_AGENT_HELP_GUIDANCE = (
    "If the user asks about configuring, setting up, or using Hermes Agent "
    "itself, load the `hermes-agent` skill with skill_view(name='hermes-agent') "
    "before answering. Docs: https://hermes-agent.nousresearch.com/docs"
)

# agent/prompt_builder.py:286  TASK_COMPLETION_GUIDANCE (applied to ALL models)
TASK_COMPLETION_GUIDANCE = (
    "# Finishing the job\n"
    "When the user asks you to build, run, or verify something, the deliverable is "
    "a working artifact backed by real tool output — not a description of one. "
    "Do not stop after writing a stub, a plan, or a single command. Keep working "
    "until you have actually exercised the code or produced the requested result, "
    "then report what real execution returned.\n"
    "If a tool, install, or network call fails and blocks the real path, say so "
    "directly and try an alternative. NEVER substitute plausible-looking fabricated "
    "output for results you couldn't actually produce."
)

# agent/prompt_builder.py:137  MEMORY_GUIDANCE (only when 'memory' tool loaded)
MEMORY_GUIDANCE = (
    "You have persistent memory across sessions. Save durable facts using the memory "
    "tool: user preferences, environment details, tool quirks, and stable conventions. "
    "Memory is injected into every turn, so keep it compact and focused on facts that "
    "will still matter later. Write memories as declarative facts, not instructions."
)

# agent/prompt_builder.py:160  SESSION_SEARCH_GUIDANCE
SESSION_SEARCH_GUIDANCE = (
    "When the user references something from a past conversation or you suspect "
    "relevant cross-session context exists, use session_search to recall it before "
    "asking them to repeat themselves."
)

# agent/prompt_builder.py:166  SKILLS_GUIDANCE (only when 'skill_manage' tool loaded)
SKILLS_GUIDANCE = (
    "After completing a complex task (5+ tool calls), fixing a tricky error, "
    "or discovering a non-trivial workflow, save the approach as a skill with "
    "skill_manage so you can reuse it next time. When using a skill and finding "
    "it outdated, patch it immediately with skill_manage(action='patch')."
)

# agent/prompt_builder.py:251  TOOL_USE_ENFORCEMENT_GUIDANCE
TOOL_USE_ENFORCEMENT_GUIDANCE = (
    "# Tool-use enforcement\n"
    "You MUST use your tools to take action — do not describe what you would do "
    "or plan to do without actually doing it. When you say you will perform an "
    "action, you MUST immediately make the corresponding tool call in the same "
    "response. Never end your turn with a promise of future action — execute it now."
)

# agent/prompt_builder.py:268  model-name substrings that trigger enforcement
TOOL_USE_ENFORCEMENT_MODELS = (
    "gpt", "codex", "gemini", "gemma", "grok", "glm", "qwen", "deepseek",
)

# agent/prompt_builder.py:443  PLATFORM_HINTS (subset, verbatim)
PLATFORM_HINTS: Dict[str, str] = {
    "whatsapp": (
        "You are on a text messaging communication platform, WhatsApp. "
        "Please do not use markdown as it does not render. "
        "You can send media files natively: include MEDIA:/absolute/path in your reply."
    ),
    "telegram": (
        "You are on a text messaging communication platform, Telegram. "
        "Standard markdown is automatically converted to Telegram format. "
        "Telegram has NO table syntax — prefer bullet lists or key: value pairs."
    ),
}

# agent/prompt_builder.py:876  context-file size cap
CONTEXT_FILE_MAX_CHARS = 20_000

# The three tiers, in cache-friendliest-first emission order.
# Mirrors agent/system_prompt.py:340-344 + build_system_prompt():347.
TIER_ORDER = ("stable", "context", "volatile")


# ---------------------------------------------------------------------------
# Dataclass I/O — typed inputs and outputs.
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """A loaded tool. Only the name gates guidance injection in Hermes;
    full schema is sent separately to the API. Mirrors how
    agent/system_prompt.py checks ``name in agent.valid_tool_names``.
    """
    name: str
    description: str = ""


@dataclass
class SkillSpec:
    """One entry in the compact skills index.

    Mirrors a SKILL.md frontmatter row used by
    build_skills_system_prompt() (agent/prompt_builder.py:1040).
    """
    name: str
    description: str = ""
    category: str = "general"


@dataclass
class MemorySnapshot:
    """Volatile memory state injected last, per turn.

    Mirrors agent._memory_store.format_for_system_prompt(...) and the
    USER.md block in agent/system_prompt.py:303-312.
    """
    memory_facts: List[str] = field(default_factory=list)
    user_profile: str = ""


@dataclass
class EnvHints:
    """Environment / platform context.

    Mirrors build_environment_hints() (agent/prompt_builder.py:767) plus
    the PLATFORM_HINTS lookup in agent/system_prompt.py:269.
    """
    host: str = ""                 # e.g. "macOS (15.5)"
    home: str = ""                 # user home directory
    cwd: str = ""                  # current working directory
    platform: str = ""             # "whatsapp" / "telegram" / "" (chat hint)


@dataclass
class PromptSection:
    """One ordered chunk of the system prompt.

    ``tier`` decides emission order and cacheability:
      stable < context < volatile (see TIER_ORDER). ``cacheable`` is a
      derived hint (True for stable+context, False for volatile) used by
      the caching step to decide where the stable prefix ends.
    """
    name: str
    tier: str            # "stable" | "context" | "volatile"
    text: str
    cacheable: bool = True


@dataclass
class CacheBreakpoint:
    """A single Anthropic cache_control marker placement.

    Mirrors the marker injected by agent/prompt_caching.py: ``ttl`` is
    "5m" or "1h"; ``message_index`` is the position in the final API
    message list (-? not used; absolute index here).
    """
    message_index: int
    role: str
    ttl: str
    reason: str          # human-readable: why this breakpoint landed here


@dataclass
class BuiltPrompt:
    """Output of the builder + caching pass.

    Attributes
    ----------
    sections        : ordered PromptSection list (post-ordering).
    system_text     : the full system prompt string (tiers joined by "\\n\\n").
    messages        : the API message list (system + conversation) with
                      cache_control markers injected.
    breakpoints     : where the up-to-4 cache markers landed and why.
    stable_prefix_chars : size of the cacheable prefix (stable+context) — the
                      portion that stays byte-stable across turns.
    """
    sections: List[PromptSection]
    system_text: str
    messages: List[Dict[str, Any]]
    breakpoints: List[CacheBreakpoint]
    stable_prefix_chars: int


# ---------------------------------------------------------------------------
# PromptBuilder — composes ordered sections into the system prompt.
# Mirrors build_system_prompt_parts() / build_system_prompt() in
# agent/system_prompt.py.
# ---------------------------------------------------------------------------

class PromptBuilder:
    """Assemble Hermes-style system prompt sections, in cache-friendly order.

    The builder is intentionally faithful to agent/system_prompt.py:
      * Identity goes first (SOUL.md would override DEFAULT_AGENT_IDENTITY;
        here we mirror the fallback path).
      * Tool-aware guidance is ONLY injected when the matching tool name is
        present in ``valid_tool_names`` (gating mirrors lines 110-195).
      * Tool-use enforcement is injected for model families in
        TOOL_USE_ENFORCEMENT_MODELS (mirrors lines 150-177).
      * Skills index, env hints, platform hint complete the STABLE tier.
      * system_message + context files form the CONTEXT tier.
      * memory + user profile + date-only timestamp form the VOLATILE tier.
    """

    def __init__(
        self,
        *,
        model: str = "hermes-4",
        provider: str = "nous",
        valid_tool_names: Optional[set[str]] = None,
        soul_identity: Optional[str] = None,
        tool_use_enforcement: str = "auto",  # "auto" | "true" | "false"
    ) -> None:
        self.model = model
        self.provider = provider
        self.valid_tool_names = valid_tool_names or set()
        self.soul_identity = soul_identity
        self.tool_use_enforcement = tool_use_enforcement

    # -- STABLE tier ------------------------------------------------------

    def _build_stable_sections(
        self,
        skills: List[SkillSpec],
        env: Optional[EnvHints],
    ) -> List[PromptSection]:
        out: List[PromptSection] = []

        # Identity: SOUL.md primary, DEFAULT_AGENT_IDENTITY fallback.
        # (agent/system_prompt.py:90-99)
        identity = self.soul_identity or DEFAULT_AGENT_IDENTITY
        out.append(PromptSection("identity", "stable", identity))

        # Hermes self-help pointer. (line 102)
        out.append(PromptSection("hermes_help", "stable", HERMES_AGENT_HELP_GUIDANCE))

        # Universal task-completion guidance (only when tools exist). (line 110)
        if self.valid_tool_names:
            out.append(PromptSection("task_completion", "stable", TASK_COMPLETION_GUIDANCE))

        # Tool-aware guidance, gated by tool name. (lines 114-132)
        tool_guidance: List[str] = []
        if "memory" in self.valid_tool_names:
            tool_guidance.append(MEMORY_GUIDANCE)
        if "session_search" in self.valid_tool_names:
            tool_guidance.append(SESSION_SEARCH_GUIDANCE)
        if "skill_manage" in self.valid_tool_names:
            tool_guidance.append(SKILLS_GUIDANCE)
        if tool_guidance:
            out.append(PromptSection("tool_guidance", "stable", " ".join(tool_guidance)))

        # Tool-use enforcement, gated by model family. (lines 150-177)
        if self.valid_tool_names and self._should_enforce_tool_use():
            out.append(PromptSection("tool_use_enforcement", "stable", TOOL_USE_ENFORCEMENT_GUIDANCE))

        # Skills index (compact). (build_skills_system_prompt, line 1040)
        skills_block = self._render_skills_index(skills)
        if skills_block:
            out.append(PromptSection("skills_index", "stable", skills_block))

        # Environment hints (host/home/cwd). (build_environment_hints, line 767)
        if env:
            env_block = self._render_env_hints(env)
            if env_block:
                out.append(PromptSection("environment_hints", "stable", env_block))

            # Platform / chat-channel hint. (agent/system_prompt.py:269-271)
            pkey = (env.platform or "").lower().strip()
            if pkey in PLATFORM_HINTS:
                out.append(PromptSection("platform_hint", "stable", PLATFORM_HINTS[pkey]))

        return out

    def _should_enforce_tool_use(self) -> bool:
        enforce = self.tool_use_enforcement
        if enforce in ("true", "always", "yes", "on"):
            return True
        if enforce in ("false", "never", "no", "off"):
            return False
        # "auto": match hardcoded model-family substrings.
        model_lower = (self.model or "").lower()
        return any(p in model_lower for p in TOOL_USE_ENFORCEMENT_MODELS)

    @staticmethod
    def _render_skills_index(skills: List[SkillSpec]) -> str:
        """Compact category-grouped skill index.

        Mirrors the <available_skills> block in
        build_skills_system_prompt() (agent/prompt_builder.py:1214-1262).
        """
        if not skills:
            return ""
        by_cat: Dict[str, List[SkillSpec]] = {}
        for s in skills:
            by_cat.setdefault(s.category, []).append(s)

        lines: List[str] = []
        for cat in sorted(by_cat):
            lines.append(f"  {cat}:")
            for s in sorted(by_cat[cat], key=lambda x: x.name):
                if s.description:
                    lines.append(f"    - {s.name}: {s.description}")
                else:
                    lines.append(f"    - {s.name}")

        return (
            "## Skills (mandatory)\n"
            "Before replying, scan the skills below. If a skill matches or is even "
            "partially relevant to your task, you MUST load it with skill_view(name) "
            "and follow its instructions.\n"
            "\n<available_skills>\n" + "\n".join(lines) + "\n</available_skills>\n"
            "\nOnly proceed without loading a skill if genuinely none are relevant."
        )

    @staticmethod
    def _render_env_hints(env: EnvHints) -> str:
        """Factual host/home/cwd block. Mirrors build_environment_hints()
        local-backend path (agent/prompt_builder.py:791-817).
        """
        lines: List[str] = []
        if env.host:
            lines.append(f"Host: {env.host}")
        if env.home:
            lines.append(f"User home directory: {env.home}")
        if env.cwd:
            lines.append(f"Current working directory: {env.cwd}")
        return "\n".join(lines)

    # -- CONTEXT tier -----------------------------------------------------

    def _build_context_sections(
        self,
        system_message: Optional[str],
        context_files: Optional[Dict[str, str]],
    ) -> List[PromptSection]:
        out: List[PromptSection] = []

        # Caller-supplied system_message. (agent/system_prompt.py:287-288)
        if system_message:
            out.append(PromptSection("system_message", "context", system_message))

        # cwd-discovered project context files (first-found-wins in Hermes;
        # here we render whatever the caller passes, capped). Mirrors
        # build_context_files_prompt() (agent/prompt_builder.py:1469).
        if context_files:
            chunks: List[str] = []
            for fname, content in context_files.items():
                capped = content[:CONTEXT_FILE_MAX_CHARS]
                chunks.append(f"## {fname}\n{capped}")
            block = (
                "# Project Context\n\n"
                "The following project context files have been loaded and should "
                "be followed:\n\n" + "\n\n".join(chunks)
            )
            out.append(PromptSection("context_files", "context", block))

        return out

    # -- VOLATILE tier ----------------------------------------------------

    def _build_volatile_sections(
        self,
        memory: Optional[MemorySnapshot],
        now: Optional[_dt.datetime],
    ) -> List[PromptSection]:
        out: List[PromptSection] = []

        if memory:
            if memory.memory_facts:
                mem_block = "# Memory\n" + "\n".join(f"- {m}" for m in memory.memory_facts)
                out.append(PromptSection("memory", "volatile", mem_block, cacheable=False))
            if memory.user_profile:
                out.append(
                    PromptSection("user_profile", "volatile",
                                  "# USER\n" + memory.user_profile, cacheable=False)
                )

        # Date-ONLY timestamp — byte-stable for the full day so it does not
        # bust the prefix cache on every rebuild. (agent/system_prompt.py:323-338)
        now = now or _dt.datetime(2026, 6, 3)
        ts = f"Conversation started: {now.strftime('%A, %B %d, %Y')}"
        if self.model:
            ts += f"\nModel: {self.model}"
        if self.provider:
            ts += f"\nProvider: {self.provider}"
        out.append(PromptSection("timestamp", "volatile", ts, cacheable=False))

        return out

    # -- public assembly --------------------------------------------------

    def build(
        self,
        *,
        tools: Optional[List[ToolSpec]] = None,
        skills: Optional[List[SkillSpec]] = None,
        memory: Optional[MemorySnapshot] = None,
        env: Optional[EnvHints] = None,
        system_message: Optional[str] = None,
        context_files: Optional[Dict[str, str]] = None,
        now: Optional[_dt.datetime] = None,
    ) -> List[PromptSection]:
        """Compose all sections in cache-friendly tier order.

        Note: ``tools`` only gates guidance via their .name (Hermes sends
        the full tool schema to the API separately, not in the system text).
        We sync valid_tool_names from the passed tools if provided.
        """
        if tools is not None:
            self.valid_tool_names = {t.name for t in tools}

        sections: List[PromptSection] = []
        sections += self._build_stable_sections(skills or [], env)
        sections += self._build_context_sections(system_message, context_files)
        sections += self._build_volatile_sections(memory, now)

        # Defensive: enforce TIER_ORDER (stable build already emits in order,
        # but a stable-sort guarantees correctness if a caller reorders).
        rank = {tier: i for i, tier in enumerate(TIER_ORDER)}
        sections.sort(key=lambda s: rank.get(s.tier, 99))
        return sections


def render_system_text(sections: List[PromptSection]) -> str:
    """Join sections into the final system prompt string.

    Tiers (and sections within them) are joined by "\\n\\n", exactly as
    build_system_prompt() does (agent/system_prompt.py:363).
    """
    return "\n\n".join(s.text.strip() for s in sections if s.text and s.text.strip())


# ---------------------------------------------------------------------------
# prompt_caching — mark cache breakpoints on the largest/stable prefix.
# Mirrors apply_anthropic_cache_control() in agent/prompt_caching.py.
# ---------------------------------------------------------------------------

def _build_marker(ttl: str) -> Dict[str, str]:
    """cache_control marker dict. Mirrors prompt_caching._build_marker()."""
    marker: Dict[str, str] = {"type": "ephemeral"}
    if ttl == "1h":
        marker["ttl"] = "1h"
    return marker


def _apply_cache_marker(msg: Dict[str, Any], marker: Dict[str, str]) -> None:
    """Attach cache_control to one message, normalising content shape.

    Mirrors prompt_caching._apply_cache_marker(): a str content is promoted
    to a single text block carrying the marker; a list content gets the
    marker on its last block.
    """
    content = msg.get("content")
    if content is None or content == "":
        msg["cache_control"] = marker
        return
    if isinstance(content, str):
        msg["content"] = [{"type": "text", "text": content, "cache_control": marker}]
        return
    if isinstance(content, list) and content and isinstance(content[-1], dict):
        content[-1]["cache_control"] = marker


def apply_anthropic_cache_control(
    api_messages: List[Dict[str, Any]],
    cache_ttl: str = "5m",
) -> tuple[List[Dict[str, Any]], List[CacheBreakpoint]]:
    """Apply the 'system_and_3' caching strategy.

    Places up to 4 cache_control breakpoints: the system prompt + the last 3
    non-system messages, all at the same TTL. The system breakpoint covers
    the entire stable+context prefix in one shot — that is the big, reusable
    chunk. The 3 trailing breakpoints let the provider also cache the most
    recent conversation turns as they grow.

    Faithful to agent/prompt_caching.py:49 (apply_anthropic_cache_control),
    with the added side output of CacheBreakpoint records for inspection.

    Returns
    -------
    (messages, breakpoints) : a deep-copied message list with markers injected,
    plus a list describing where each of the (<=4) breakpoints landed.
    """
    messages = copy.deepcopy(api_messages)
    breakpoints: List[CacheBreakpoint] = []
    if not messages:
        return messages, breakpoints

    marker = _build_marker(cache_ttl)
    used = 0

    # 1) System prompt breakpoint — caches the whole stable+context prefix.
    if messages[0].get("role") == "system":
        _apply_cache_marker(messages[0], marker)
        breakpoints.append(CacheBreakpoint(
            message_index=0, role="system", ttl=cache_ttl,
            reason="system prompt: caches the entire stable+context prefix",
        ))
        used += 1

    # 2) Up to (4 - used) trailing non-system messages.
    remaining = 4 - used
    non_sys = [i for i in range(len(messages)) if messages[i].get("role") != "system"]
    for idx in non_sys[-remaining:]:
        _apply_cache_marker(messages[idx], marker)
        breakpoints.append(CacheBreakpoint(
            message_index=idx, role=messages[idx].get("role", "?"), ttl=cache_ttl,
            reason="trailing conversation turn (rolling cache as history grows)",
        ))

    return messages, breakpoints


# ---------------------------------------------------------------------------
# build_prompt — convenience orchestrator producing a BuiltPrompt.
# ---------------------------------------------------------------------------

def build_prompt(
    builder: PromptBuilder,
    *,
    tools: Optional[List[ToolSpec]] = None,
    skills: Optional[List[SkillSpec]] = None,
    memory: Optional[MemorySnapshot] = None,
    env: Optional[EnvHints] = None,
    system_message: Optional[str] = None,
    context_files: Optional[Dict[str, str]] = None,
    conversation: Optional[List[Dict[str, Any]]] = None,
    cache_ttl: str = "5m",
    now: Optional[_dt.datetime] = None,
) -> BuiltPrompt:
    """End-to-end: compose sections -> system text -> messages -> cache markers.

    This wires the two halves of the feature together the way the real agent
    does: build the (cached-once) system prompt, prepend it as the first
    message, append the live conversation, then apply cache_control.
    """
    sections = builder.build(
        tools=tools, skills=skills, memory=memory, env=env,
        system_message=system_message, context_files=context_files, now=now,
    )
    system_text = render_system_text(sections)

    # Size of the cacheable prefix (stable + context tiers).
    stable_prefix_chars = sum(
        len(s.text) for s in sections if s.tier in ("stable", "context")
    )

    messages: List[Dict[str, Any]] = [{"role": "system", "content": system_text}]
    if conversation:
        messages += copy.deepcopy(conversation)

    messages, breakpoints = apply_anthropic_cache_control(messages, cache_ttl=cache_ttl)

    return BuiltPrompt(
        sections=sections,
        system_text=system_text,
        messages=messages,
        breakpoints=breakpoints,
        stable_prefix_chars=stable_prefix_chars,
    )


__all__ = [
    "ToolSpec", "SkillSpec", "MemorySnapshot", "EnvHints",
    "PromptSection", "CacheBreakpoint", "BuiltPrompt",
    "PromptBuilder", "render_system_text",
    "apply_anthropic_cache_control", "build_prompt",
    "TIER_ORDER", "TOOL_USE_ENFORCEMENT_MODELS", "PLATFORM_HINTS",
    "DEFAULT_AGENT_IDENTITY",
]
