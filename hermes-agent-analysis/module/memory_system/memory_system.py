"""Self-contained, stdlib-only mirror of Hermes Agent's "closed learning loop" memory system.

This module distills Nous Research's Hermes memory architecture into a single
runnable file so a researcher can read the whole loop end to end and customize it.

The closed learning loop has four moving parts:

    1. memory_tool   — the agent-facing write tool. The model calls
                       ``memory(action=add/replace/remove, target=memory/user, ...)``
                       to persist durable facts.  (mirror of tools/memory_tool.py)

    2. MemoryStore   — bounded, file-persisted entry store. Holds a FROZEN snapshot
                       for system-prompt injection (prefix-cache stable) plus the
                       LIVE entry list that tool calls mutate. (tools/memory_tool.py)

    3. MemoryProvider / MemoryManager
                     — a pluggable backend interface + an orchestrator that builds
                       the system-prompt block, prefetches/recalls relevant memories
                       for the next turn, and injects them wrapped in a
                       ``<memory-context>`` fence.
                       (agent/memory_provider.py, agent/memory_manager.py)

    4. background nudge + Curator
                     — Hermes periodically NUDGES the agent (every N user turns) to
                       review the transcript and proactively save memories
                       (agent/background_review.py::_MEMORY_REVIEW_PROMPT), and a
                       slower CURATOR pass periodically reviews/consolidates the
                       stored memories — merge, refine, prune. (agent/curator.py)

The full loop demonstrated by demo.py:

    session 1: agent writes memory via the tool (directly + nudged review)
        -> MemoryStore persists to "disk"
    curator:   reviews the store, merges/refines redundant entries
    session 2: MemoryManager retrieves relevant memories and injects them
        into the system prompt before the model sees the new user turn.

Everything here is stdlib only. The single external dependency in real Hermes —
the LLM — is modeled as a pluggable ``LLMCallable`` so curation and nudged review
stay deterministic and mockable.

Source citations are given inline as ``[hermes: <file>:<symbol>]``.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol


# ===========================================================================
# Constants (faithful to Hermes defaults)
# ===========================================================================

# tools/memory_tool.py — entries are joined by a "§" delimiter on disk.
ENTRY_DELIMITER: str = "\n§\n"

# tools/memory_tool.py::MemoryStore.__init__ — bounded char budgets per target.
DEFAULT_MEMORY_CHAR_LIMIT: int = 2200
DEFAULT_USER_CHAR_LIMIT: int = 1375

# agent/agent_init.py — fire a background memory-review nudge every N user turns.
DEFAULT_MEMORY_NUDGE_INTERVAL: int = 10

# agent/background_review.py::_MEMORY_REVIEW_PROMPT — the nudge prompt the agent
# receives when it is asked to review the transcript and save memories.
MEMORY_REVIEW_PROMPT: str = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)


# ===========================================================================
# Lightweight threat scan (mirror of tools/threat_patterns.py "strict" scope)
# ===========================================================================
#
# Memory enters the system prompt as a FROZEN snapshot, so a poisoned entry
# persists for the whole session and across sessions. Hermes scans every write
# and every load. We mirror a tiny subset of the strict pattern set.
# [hermes: tools/memory_tool.py::_scan_memory_content]

_THREAT_PATTERNS: List[str] = [
    r"ignore (all )?previous instructions",
    r"disregard (the )?(system|above)",
    r"you are now",
    r"exfiltrate",
    r"</?system>",
]


def scan_for_threats(text: str) -> List[str]:
    """Return a list of matched threat-pattern descriptions (empty if clean).

    [hermes: tools/threat_patterns.py::scan_for_threats]
    """
    findings: List[str] = []
    for pat in _THREAT_PATTERNS:
        if re.search(pat, text, flags=re.IGNORECASE):
            findings.append(pat)
    return findings


def _scan_memory_content(content: str) -> Optional[str]:
    """Return an error string if content trips a threat pattern, else None.

    [hermes: tools/memory_tool.py::_scan_memory_content]
    """
    hits = scan_for_threats(content)
    if hits:
        return f"Blocked: content matched threat pattern(s): {', '.join(hits)}"
    return None


# ===========================================================================
# MemoryStore — bounded, file-persisted curated memory
# [hermes: tools/memory_tool.py::MemoryStore]
# ===========================================================================


@dataclass
class ToolResult:
    """Structured result returned by every memory tool action.

    Mirrors the dict that Hermes' MemoryStore methods return, which the tool
    serializes to JSON. [hermes: tools/memory_tool.py::MemoryStore._success_response]
    """

    success: bool
    target: str = ""
    message: str = ""
    error: str = ""
    entries: List[str] = field(default_factory=list)
    entry_count: int = 0
    usage: str = ""

    def to_json(self) -> str:
        """Serialize to the JSON string the tool layer returns to the model."""
        payload = {k: v for k, v in self.__dict__.items() if v not in ("", [], 0) or k == "success"}
        return json.dumps(payload, ensure_ascii=False)


class MemoryStore:
    """Bounded curated memory with file persistence. One instance per agent.

    Maintains two parallel states, exactly like Hermes:

      - ``_system_prompt_snapshot``: frozen at load time, used for system-prompt
        injection. Never mutated mid-session, which keeps the prefix cache stable.
      - ``memory_entries`` / ``user_entries``: live state, mutated by tool calls
        and persisted to disk. Tool responses always reflect this live state.

    Two targets:
      - ``"memory"``: the agent's own notes (environment facts, conventions, lessons)
      - ``"user"``:   who the user is (name, role, preferences, communication style)

    [hermes: tools/memory_tool.py::MemoryStore]
    """

    def __init__(
        self,
        memory_dir: Path,
        memory_char_limit: int = DEFAULT_MEMORY_CHAR_LIMIT,
        user_char_limit: int = DEFAULT_USER_CHAR_LIMIT,
    ) -> None:
        self.memory_dir = Path(memory_dir)
        self.memory_entries: List[str] = []
        self.user_entries: List[str] = []
        self.memory_char_limit = memory_char_limit
        self.user_char_limit = user_char_limit
        # Frozen snapshot for the system prompt — set once at load_from_disk().
        self._system_prompt_snapshot: Dict[str, str] = {"memory": "", "user": ""}

    # -- persistence ---------------------------------------------------------

    def _path_for(self, target: str) -> Path:
        return self.memory_dir / ("USER.md" if target == "user" else "MEMORY.md")

    def _entries_for(self, target: str) -> List[str]:
        return self.user_entries if target == "user" else self.memory_entries

    def _set_entries(self, target: str, entries: List[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _char_limit(self, target: str) -> int:
        return self.user_char_limit if target == "user" else self.memory_char_limit

    def _char_count(self, target: str) -> int:
        entries = self._entries_for(target)
        return len(ENTRY_DELIMITER.join(entries)) if entries else 0

    @staticmethod
    def _read_file(path: Path) -> List[str]:
        """Read a memory file and split into entries on the § delimiter.

        [hermes: tools/memory_tool.py::MemoryStore._read_file]
        """
        if not path.exists():
            return []
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER)]
        return [e for e in entries if e]

    def _write_file(self, path: Path, entries: List[str]) -> None:
        """Persist entries joined by the § delimiter (atomic temp+rename).

        [hermes: tools/memory_tool.py::MemoryStore._write_file]
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)  # atomic rename — readers see old or new, never partial

    def save_to_disk(self, target: str) -> None:
        """Persist entries to the target file. Called after every mutation."""
        self._write_file(self._path_for(target), self._entries_for(target))

    def load_from_disk(self) -> None:
        """Load entries from MEMORY.md / USER.md and capture the frozen snapshot.

        Each entry is scanned at snapshot-build time; any threat-matching entry
        is replaced with a ``[BLOCKED: ...]`` placeholder in the snapshot only —
        the live list keeps the raw text so the user can inspect and remove it.

        [hermes: tools/memory_tool.py::MemoryStore.load_from_disk]
        """
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.memory_entries = list(dict.fromkeys(self._read_file(self._path_for("memory"))))
        self.user_entries = list(dict.fromkeys(self._read_file(self._path_for("user"))))
        self._system_prompt_snapshot = {
            "memory": self._render_block("memory", self._sanitize(self.memory_entries, "MEMORY.md")),
            "user": self._render_block("user", self._sanitize(self.user_entries, "USER.md")),
        }

    @staticmethod
    def _sanitize(entries: List[str], filename: str) -> List[str]:
        """Replace any threat-matching entry with a placeholder for the snapshot.

        [hermes: tools/memory_tool.py::MemoryStore._sanitize_entries_for_snapshot]
        """
        out: List[str] = []
        for entry in entries:
            if not entry or entry.startswith("[BLOCKED:"):
                out.append(entry)
                continue
            hits = scan_for_threats(entry)
            if hits:
                out.append(f"[BLOCKED: {filename} entry contained threat pattern(s): {', '.join(hits)}.]")
            else:
                out.append(entry)
        return out

    # -- mutations (tool actions) -------------------------------------------

    def add(self, target: str, content: str) -> ToolResult:
        """Append a new entry. Errors if it would exceed the char budget.

        [hermes: tools/memory_tool.py::MemoryStore.add]
        """
        content = (content or "").strip()
        if not content:
            return ToolResult(success=False, error="Content cannot be empty.")
        scan_error = _scan_memory_content(content)
        if scan_error:
            return ToolResult(success=False, error=scan_error)

        entries = self._entries_for(target)
        if content in entries:  # reject exact duplicates
            return self._ok(target, "Entry already exists (no duplicate added).")

        new_total = len(ENTRY_DELIMITER.join(entries + [content]))
        limit = self._char_limit(target)
        if new_total > limit:
            current = self._char_count(target)
            return ToolResult(
                success=False,
                error=(
                    f"Memory at {current:,}/{limit:,} chars. Adding this entry "
                    f"({len(content)} chars) would exceed the limit. Replace or remove first."
                ),
            )
        entries.append(content)
        self._set_entries(target, entries)
        self.save_to_disk(target)
        return self._ok(target, "Entry added.")

    def replace(self, target: str, old_text: str, new_content: str) -> ToolResult:
        """Find the entry containing ``old_text`` and replace it with ``new_content``.

        [hermes: tools/memory_tool.py::MemoryStore.replace]
        """
        old_text = (old_text or "").strip()
        new_content = (new_content or "").strip()
        if not old_text:
            return ToolResult(success=False, error="old_text cannot be empty.")
        if not new_content:
            return ToolResult(success=False, error="new_content cannot be empty. Use 'remove' to delete.")
        scan_error = _scan_memory_content(new_content)
        if scan_error:
            return ToolResult(success=False, error=scan_error)

        entries = self._entries_for(target)
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return ToolResult(success=False, error=f"No entry matched '{old_text}'.")
        if len({e for _, e in matches}) > 1:
            return ToolResult(success=False, error=f"Multiple entries matched '{old_text}'. Be more specific.")

        idx = matches[0][0]
        test = entries.copy()
        test[idx] = new_content
        limit = self._char_limit(target)
        if len(ENTRY_DELIMITER.join(test)) > limit:
            return ToolResult(success=False, error=f"Replacement would exceed {limit:,} chars. Shorten it.")
        entries[idx] = new_content
        self._set_entries(target, entries)
        self.save_to_disk(target)
        return self._ok(target, "Entry replaced.")

    def remove(self, target: str, old_text: str) -> ToolResult:
        """Remove the entry containing ``old_text``.

        [hermes: tools/memory_tool.py::MemoryStore.remove]
        """
        old_text = (old_text or "").strip()
        if not old_text:
            return ToolResult(success=False, error="old_text cannot be empty.")
        entries = self._entries_for(target)
        matches = [(i, e) for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return ToolResult(success=False, error=f"No entry matched '{old_text}'.")
        if len({e for _, e in matches}) > 1:
            return ToolResult(success=False, error=f"Multiple entries matched '{old_text}'. Be more specific.")
        entries.pop(matches[0][0])
        self._set_entries(target, entries)
        self.save_to_disk(target)
        return self._ok(target, "Entry removed.")

    # -- rendering / formatting ---------------------------------------------

    def format_for_system_prompt(self, target: str) -> Optional[str]:
        """Return the FROZEN snapshot block for system-prompt injection.

        Returns the state captured at ``load_from_disk()`` time, NOT live state —
        this keeps the system prompt stable across turns (prefix-cache invariant).

        [hermes: tools/memory_tool.py::MemoryStore.format_for_system_prompt]
        """
        block = self._system_prompt_snapshot.get(target, "")
        return block or None

    def _render_block(self, target: str, entries: List[str]) -> str:
        """Render a labeled system-prompt block with a usage indicator.

        [hermes: tools/memory_tool.py::MemoryStore._render_block]
        """
        if not entries:
            return ""
        limit = self._char_limit(target)
        content = ENTRY_DELIMITER.join(entries)
        current = len(content)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        if target == "user":
            header = f"USER PROFILE (who the user is) [{pct}% — {current:,}/{limit:,} chars]"
        else:
            header = f"MEMORY (your personal notes) [{pct}% — {current:,}/{limit:,} chars]"
        sep = "═" * 46
        return f"{sep}\n{header}\n{sep}\n{content}"

    def _ok(self, target: str, message: str) -> ToolResult:
        entries = self._entries_for(target)
        current = self._char_count(target)
        limit = self._char_limit(target)
        pct = min(100, int((current / limit) * 100)) if limit > 0 else 0
        return ToolResult(
            success=True,
            target=target,
            message=message,
            entries=list(entries),
            entry_count=len(entries),
            usage=f"{pct}% — {current:,}/{limit:,} chars",
        )


# ===========================================================================
# memory_tool — the agent-facing write entry point
# [hermes: tools/memory_tool.py::memory_tool + MEMORY_SCHEMA]
# ===========================================================================

# OpenAI function-calling schema the model sees. [hermes: tools/memory_tool.py::MEMORY_SCHEMA]
MEMORY_SCHEMA: Dict[str, Any] = {
    "name": "memory",
    "description": (
        "Save durable information to persistent memory that survives across sessions. "
        "Memory is injected into future turns, so keep it compact and focused on facts "
        "that will still matter later. Save user preferences/corrections > environment "
        "facts > procedural knowledge."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "replace", "remove"]},
            "target": {"type": "string", "enum": ["memory", "user"], "default": "memory"},
            "content": {"type": "string", "description": "Entry text (add/replace)."},
            "old_text": {"type": "string", "description": "Substring identifying the entry (replace/remove)."},
        },
        "required": ["action"],
    },
}


@dataclass
class MemoryToolCall:
    """Typed input for the memory tool — mirrors the model's function-call args.

    [hermes: tools/memory_tool.py::memory_tool signature]
    """

    action: str  # "add" | "replace" | "remove"
    target: str = "memory"  # "memory" | "user"
    content: Optional[str] = None
    old_text: Optional[str] = None


def memory_tool(call: MemoryToolCall, store: Optional[MemoryStore]) -> str:
    """Single entry point for the memory tool. Dispatches to MemoryStore methods.

    Returns a JSON string (the tool result), exactly like Hermes.

    [hermes: tools/memory_tool.py::memory_tool]
    """
    if store is None:
        return ToolResult(success=False, error="Memory is not available.").to_json()
    if call.target not in {"memory", "user"}:
        return ToolResult(success=False, error=f"Invalid target '{call.target}'.").to_json()

    if call.action == "add":
        if not call.content:
            return ToolResult(success=False, error="Content is required for 'add'.").to_json()
        result = store.add(call.target, call.content)
    elif call.action == "replace":
        if not call.old_text:
            return ToolResult(success=False, error="old_text is required for 'replace'.").to_json()
        if not call.content:
            return ToolResult(success=False, error="content is required for 'replace'.").to_json()
        result = store.replace(call.target, call.old_text, call.content)
    elif call.action == "remove":
        if not call.old_text:
            return ToolResult(success=False, error="old_text is required for 'remove'.").to_json()
        result = store.remove(call.target, call.old_text)
    else:
        return ToolResult(success=False, error=f"Unknown action '{call.action}'.").to_json()

    return result.to_json()


# ===========================================================================
# MemoryProvider — pluggable backend interface
# [hermes: agent/memory_provider.py::MemoryProvider]
# ===========================================================================


class MemoryProvider(ABC):
    """Abstract base class for memory providers.

    Providers give the agent persistent recall across sessions. The
    MemoryManager wires the lifecycle. We keep the core hooks Hermes exposes:
    ``system_prompt_block`` (static text), ``prefetch`` (recall for the next
    turn), and ``get_tool_schemas`` / ``handle_tool_call`` (tools).

    [hermes: agent/memory_provider.py::MemoryProvider]
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'builtin')."""

    def system_prompt_block(self) -> str:
        """STATIC text for the system prompt (instructions/status). Default empty.

        [hermes: agent/memory_provider.py::system_prompt_block]
        """
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall relevant context for the upcoming turn. Default empty.

        Return formatted text to inject, or "" if nothing relevant.

        [hermes: agent/memory_provider.py::prefetch]
        """
        return ""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas this provider exposes (OpenAI function format).

        [hermes: agent/memory_provider.py::get_tool_schemas]
        """
        return []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Dispatch a tool call for one of this provider's tools (JSON string out).

        [hermes: agent/memory_provider.py::handle_tool_call]
        """
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")


class BuiltinMemoryProvider(MemoryProvider):
    """The built-in provider backed by a local file ``MemoryStore``.

    This is the always-on provider in Hermes. It exposes the ``memory`` tool,
    injects the frozen MEMORY/USER snapshot into the system prompt, and
    retrieves relevant entries via a simple keyword overlap score for prefetch.

    [hermes: the builtin path in agent/memory_manager.py + tools/memory_tool.py]
    """

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    @property
    def name(self) -> str:
        return "builtin"

    def system_prompt_block(self) -> str:
        """Inject the FROZEN MEMORY + USER snapshots (stable across the session)."""
        parts: List[str] = []
        for target in ("user", "memory"):
            block = self.store.format_for_system_prompt(target)
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall the entries most relevant to ``query`` via keyword overlap.

        Real Hermes external providers use embeddings / FTS / HRR algebra; the
        builtin path injects everything via the snapshot. We model a tiny
        relevance ranker so the retrieve-and-inject step is observable.
        """
        scored = _rank_by_overlap(query, self.store.memory_entries + self.store.user_entries)
        relevant = [e for score, e in scored if score > 0][:3]
        if not relevant:
            return ""
        return "Relevant recalled memories:\n" + "\n".join(f"- {e}" for e in relevant)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [MEMORY_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if tool_name != "memory":
            raise NotImplementedError(tool_name)
        return memory_tool(
            MemoryToolCall(
                action=args.get("action", ""),
                target=args.get("target", "memory"),
                content=args.get("content"),
                old_text=args.get("old_text"),
            ),
            self.store,
        )


def _rank_by_overlap(query: str, entries: List[str]) -> List[tuple]:
    """Rank entries by keyword overlap with the query (descending).

    A deliberately simple stdlib stand-in for embedding similarity. Returns a
    list of ``(score, entry)`` tuples sorted by score descending.
    """
    q_tokens = {t for t in re.findall(r"\w+", query.lower()) if len(t) > 2}
    scored: List[tuple] = []
    for e in entries:
        e_tokens = {t for t in re.findall(r"\w+", e.lower()) if len(t) > 2}
        scored.append((len(q_tokens & e_tokens), e))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


# ===========================================================================
# build_memory_context_block — how recall context is fenced before injection
# [hermes: agent/memory_manager.py::build_memory_context_block]
# ===========================================================================


def build_memory_context_block(raw_context: str) -> str:
    """Wrap prefetched memory in a ``<memory-context>`` fence with a system note.

    The fence + system note tell the model this is authoritative recalled data,
    NOT new user input — so it can't be confused for a fresh instruction.

    [hermes: agent/memory_manager.py::build_memory_context_block]
    """
    if not raw_context or not raw_context.strip():
        return ""
    return (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, NOT new user input. "
        "Treat as authoritative reference data — this is the agent's persistent memory "
        "and should inform all responses.]\n\n"
        f"{raw_context.strip()}\n"
        "</memory-context>"
    )


# ===========================================================================
# MemoryManager — orchestrates providers, builds prompt, retrieves + injects
# [hermes: agent/memory_manager.py::MemoryManager]
# ===========================================================================


class MemoryManager:
    """Orchestrates the built-in provider plus at most one external provider.

    The builtin provider is always first. Only one non-builtin (external)
    provider is allowed. A failure in one provider never blocks the other.

    [hermes: agent/memory_manager.py::MemoryManager]
    """

    def __init__(self) -> None:
        self._providers: List[MemoryProvider] = []
        self._tool_to_provider: Dict[str, MemoryProvider] = {}
        self._has_external: bool = False

    def add_provider(self, provider: MemoryProvider) -> None:
        """Register a provider. Builtin always accepted; only one external allowed.

        [hermes: agent/memory_manager.py::MemoryManager.add_provider]
        """
        is_builtin = provider.name == "builtin"
        if not is_builtin:
            if self._has_external:
                return  # reject second external provider (tool-schema bloat guard)
            self._has_external = True
        self._providers.append(provider)
        for schema in provider.get_tool_schemas():
            name = schema.get("name", "")
            if name and name not in self._tool_to_provider:
                self._tool_to_provider[name] = provider

    @property
    def providers(self) -> List[MemoryProvider]:
        return list(self._providers)

    def build_system_prompt(self) -> str:
        """Collect system-prompt blocks from all providers.

        [hermes: agent/memory_manager.py::MemoryManager.build_system_prompt]
        """
        blocks: List[str] = []
        for provider in self._providers:
            try:
                block = provider.system_prompt_block()
                if block and block.strip():
                    blocks.append(block)
            except Exception:
                pass
        return "\n\n".join(blocks)

    def prefetch_all(self, query: str, *, session_id: str = "") -> str:
        """Collect and merge recall context from all providers.

        [hermes: agent/memory_manager.py::MemoryManager.prefetch_all]
        """
        parts: List[str] = []
        for provider in self._providers:
            try:
                result = provider.prefetch(query, session_id=session_id)
                if result and result.strip():
                    parts.append(result)
            except Exception:
                pass
        return "\n\n".join(parts)

    def inject_for_turn(self, query: str, *, session_id: str = "") -> str:
        """Retrieve relevant memories for ``query`` and return the fenced block.

        This is the retrieve-and-inject step: prefetch recall from providers,
        then wrap it in ``<memory-context>`` ready to prepend to the turn.
        (Convenience composing prefetch_all + build_memory_context_block.)
        """
        return build_memory_context_block(self.prefetch_all(query, session_id=session_id))

    def has_tool(self, tool_name: str) -> bool:
        return tool_name in self._tool_to_provider

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Route a tool call to the provider that owns the tool.

        [hermes: agent/memory_manager.py::MemoryManager.handle_tool_call]
        """
        provider = self._tool_to_provider.get(tool_name)
        if provider is None:
            return ToolResult(success=False, error=f"No provider for tool '{tool_name}'.").to_json()
        return provider.handle_tool_call(tool_name, args, **kwargs)

    def get_all_tool_schemas(self) -> List[Dict[str, Any]]:
        """Collect tool schemas from all providers (deduped by name)."""
        schemas: List[Dict[str, Any]] = []
        seen: set = set()
        for provider in self._providers:
            for schema in provider.get_tool_schemas():
                name = schema.get("name", "")
                if name and name not in seen:
                    schemas.append(schema)
                    seen.add(name)
        return schemas


# ===========================================================================
# Background nudge — periodic "save to memory" review of the transcript
# [hermes: agent/background_review.py + agent/conversation_loop.py]
# ===========================================================================


class LLMCallable(Protocol):
    """A pluggable LLM. Given a prompt, return the model's text response.

    In Hermes the nudge/curator pass forks a real AIAgent. We model it as a
    callable so the loop stays deterministic and mockable.
    """

    def __call__(self, prompt: str) -> str: ...


@dataclass
class NudgeOutcome:
    """Result of a background memory-review nudge.

    [hermes: agent/background_review.py::summarize_background_review_actions]
    """

    fired: bool
    tool_calls: List[MemoryToolCall] = field(default_factory=list)
    results: List[str] = field(default_factory=list)
    summary: str = ""


class BackgroundReviewNudge:
    """Periodically nudges the agent to review the transcript and save memories.

    Hermes increments a per-turn counter and, every ``nudge_interval`` user
    turns, spawns a forked agent that runs ``MEMORY_REVIEW_PROMPT`` against the
    transcript. The forked agent may call the ``memory`` tool to persist what it
    found, or reply "Nothing to save."

    We mirror the counter + trigger and route the nudged tool calls through the
    same MemoryManager.

    [hermes: agent/conversation_loop.py (counter), agent/background_review.py (prompt+spawn)]
    """

    def __init__(
        self,
        manager: MemoryManager,
        llm: LLMCallable,
        nudge_interval: int = DEFAULT_MEMORY_NUDGE_INTERVAL,
    ) -> None:
        self.manager = manager
        self.llm = llm
        self.nudge_interval = nudge_interval
        self._turns_since_memory = 0

    def on_user_turn(self, transcript: List[Dict[str, str]]) -> NudgeOutcome:
        """Tick the turn counter; fire a review when the interval elapses.

        ``transcript`` is the OpenAI-style message list ([{role, content}, ...]).
        Returns a NudgeOutcome describing whether the nudge fired and what it did.

        [hermes: agent/conversation_loop.py::_memory_nudge_interval trigger]
        """
        if self.nudge_interval <= 0:
            return NudgeOutcome(fired=False)
        self._turns_since_memory += 1
        if self._turns_since_memory < self.nudge_interval:
            return NudgeOutcome(fired=False)
        self._turns_since_memory = 0
        return self.run_review(transcript)

    def run_review(self, transcript: List[Dict[str, str]]) -> NudgeOutcome:
        """Run one memory-review pass: feed transcript + nudge prompt to the LLM,
        parse any ``memory(...)`` tool calls it emits, and apply them.

        The mock LLM returns a JSON list of tool-call dicts (or "Nothing to save").

        [hermes: agent/background_review.py::_run_review_in_thread]
        """
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in transcript)
        prompt = f"<transcript>\n{rendered}\n</transcript>\n\n{MEMORY_REVIEW_PROMPT}"
        raw = self.llm(prompt).strip()

        if "nothing to save" in raw.lower():
            return NudgeOutcome(fired=True, summary="Nothing to save.")

        calls = _parse_tool_calls(raw)
        results: List[str] = []
        for call in calls:
            results.append(
                self.manager.handle_tool_call(
                    "memory",
                    {
                        "action": call.action,
                        "target": call.target,
                        "content": call.content,
                        "old_text": call.old_text,
                    },
                )
            )
        summary = f"{len(calls)} memory write(s) from nudged review" if calls else "Nothing to save."
        return NudgeOutcome(fired=True, tool_calls=calls, results=results, summary=summary)


def _parse_tool_calls(raw: str) -> List[MemoryToolCall]:
    """Parse a JSON list of tool-call dicts from an LLM response into typed calls."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    calls: List[MemoryToolCall] = []
    for item in data:
        if isinstance(item, dict) and item.get("action"):
            calls.append(
                MemoryToolCall(
                    action=item["action"],
                    target=item.get("target", "memory"),
                    content=item.get("content"),
                    old_text=item.get("old_text"),
                )
            )
    return calls


# ===========================================================================
# Curator — periodic review/consolidation of the stored memories
# [hermes: agent/curator.py]
# ===========================================================================


@dataclass
class CuratorDecision:
    """One curation action the LLM proposes over the existing memory store.

    Mirrors the structured action the Hermes curator emits in its review pass
    (Hermes curates skills via skill_manage; here we apply the same shape —
    merge/replace/remove/keep — directly to memory entries).

    [hermes: agent/curator.py::CURATOR_REVIEW_PROMPT + _parse_structured_summary]
    """

    action: str  # "merge" | "replace" | "remove" | "keep"
    target: str = "memory"  # "memory" | "user"
    old_texts: List[str] = field(default_factory=list)  # entries this action consumes
    new_content: str = ""  # the consolidated/refined entry (merge/replace)
    rationale: str = ""


@dataclass
class CuratorReport:
    """Human-readable + structured result of one curator pass.

    [hermes: agent/curator.py::_write_run_report / _render_report_markdown]
    """

    decisions: List[CuratorDecision] = field(default_factory=list)
    applied: int = 0
    skipped: int = 0
    before_counts: Dict[str, int] = field(default_factory=dict)
    after_counts: Dict[str, int] = field(default_factory=dict)
    summary: str = ""


# The curator's review prompt. Hermes' real prompt is a long "umbrella-building
# consolidation" instruction; we keep its spirit: consolidate redundant entries
# into class-level umbrellas, refine, prune — never silently lose information.
# [hermes: agent/curator.py::CURATOR_REVIEW_PROMPT]
CURATOR_REVIEW_PROMPT: str = (
    "You are running as the background memory CURATOR. This is an "
    "UMBRELLA-BUILDING consolidation pass, not a passive audit.\n\n"
    "Goal: keep memory as a small set of CLASS-LEVEL, durable facts. Merge "
    "redundant or overlapping entries into one richer entry. Refine vague "
    "entries. Remove entries that are stale or no longer true. NEVER silently "
    "lose information — fold it into the consolidated entry instead.\n\n"
    "Return a JSON list of decisions. Each decision is one of:\n"
    '  {"action":"merge","target":"...","old_texts":[...],"new_content":"...","rationale":"..."}\n'
    '  {"action":"replace","target":"...","old_texts":["<one>"],"new_content":"...","rationale":"..."}\n'
    '  {"action":"remove","target":"...","old_texts":["<one>"],"rationale":"..."}\n'
    "Return [] if nothing needs consolidation."
)


class Curator:
    """Periodic reviewer that consolidates and refines the stored memories.

    The slow loop (Hermes default: every 7 days) that keeps the store healthy:
      1. Render the current entries as a candidate list.
      2. Ask the LLM (pluggable) for a JSON list of consolidation decisions.
      3. Apply each decision through the SAME memory tool path the agent uses
         (add/replace/remove) so all writes go through one validated door.

    This is the "curator refines" stage of the closed learning loop: the agent
    accumulates entries during sessions; the curator periodically merges and
    prunes them so memory stays compact and high-signal.

    [hermes: agent/curator.py::run_curator_review / apply_automatic_transitions]
    """

    def __init__(self, store: MemoryStore, llm: LLMCallable) -> None:
        self.store = store
        self.llm = llm

    def _render_candidate_list(self) -> str:
        """Render current entries as a numbered candidate list for the prompt.

        [hermes: agent/curator.py::_render_candidate_list]
        """
        lines: List[str] = []
        for target in ("user", "memory"):
            entries = self.store._entries_for(target)
            lines.append(f"[{target}] ({len(entries)} entries):")
            if not entries:
                lines.append("  (none)")
            for i, e in enumerate(entries):
                lines.append(f"  {i}. {e}")
        return "\n".join(lines)

    def run_review(self, dry_run: bool = False) -> CuratorReport:
        """Execute one curation pass.

        If ``dry_run`` is True, decisions are computed and reported but NOT
        applied — mirroring Hermes' ``hermes curator run --dry-run``.

        [hermes: agent/curator.py::run_curator_review]
        """
        before = {
            "user": len(self.store.user_entries),
            "memory": len(self.store.memory_entries),
        }
        prompt = f"{CURATOR_REVIEW_PROMPT}\n\nCANDIDATES:\n{self._render_candidate_list()}"
        decisions = self._parse_decisions(self.llm(prompt))

        applied = 0
        skipped = 0
        if not dry_run:
            for d in decisions:
                if self._apply(d):
                    applied += 1
                else:
                    skipped += 1
        else:
            skipped = len(decisions)

        after = {
            "user": len(self.store.user_entries),
            "memory": len(self.store.memory_entries),
        }
        verb = "would apply" if dry_run else "applied"
        report = CuratorReport(
            decisions=decisions,
            applied=applied,
            skipped=skipped,
            before_counts=before,
            after_counts=after,
            summary=(
                f"curator: {verb} {len(decisions) if dry_run else applied} decision(s); "
                f"entries {before} -> {after}"
            ),
        )
        return report

    def _apply(self, d: CuratorDecision) -> bool:
        """Apply one decision through the memory tool path. Returns True on success.

        merge   -> add the consolidated entry, then remove each consumed entry.
        replace -> replace the single old entry with new_content.
        remove  -> remove the single old entry.

        [hermes: agent/curator.py — curator mutates via the same tool surface]
        """
        target = d.target if d.target in {"memory", "user"} else "memory"

        if d.action == "merge":
            if not d.new_content or not d.old_texts:
                return False
            res = self.store.add(target, d.new_content)
            if not res.success:
                return False
            for old in d.old_texts:
                self.store.remove(target, old)  # best-effort; merged content already saved
            return True

        if d.action == "replace":
            if len(d.old_texts) != 1 or not d.new_content:
                return False
            return self.store.replace(target, d.old_texts[0], d.new_content).success

        if d.action == "remove":
            if len(d.old_texts) != 1:
                return False
            return self.store.remove(target, d.old_texts[0]).success

        return False  # "keep" / unknown — no-op

    @staticmethod
    def _parse_decisions(raw: str) -> List[CuratorDecision]:
        """Parse the LLM's JSON decision list into typed CuratorDecision objects.

        [hermes: agent/curator.py::_parse_structured_summary]
        """
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(data, list):
            return []
        out: List[CuratorDecision] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("action"):
                continue
            out.append(
                CuratorDecision(
                    action=item["action"],
                    target=item.get("target", "memory"),
                    old_texts=list(item.get("old_texts", [])),
                    new_content=item.get("new_content", ""),
                    rationale=item.get("rationale", ""),
                )
            )
        return out
