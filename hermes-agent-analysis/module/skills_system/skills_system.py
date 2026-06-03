"""
skills_system.py — a self-contained, stdlib-only *mirror* of Hermes Agent's
skills system (Nous Research), compatible with the agentskills.io SKILL.md
standard.

It distills, in one readable file, five things the real Hermes does across
several modules:

  1. On-disk skill format ............ a directory holding a ``SKILL.md`` whose
     YAML frontmatter carries ``name`` / ``description`` / ``triggers`` plus an
     optional ``platforms`` gate, followed by a markdown body.
     (mirrors: agent/skill_utils.py :: parse_frontmatter / skill_matches_platform)

  2. Discovery + parsing ............. ``SkillLoader`` walks a directory tree,
     prunes excluded dirs (.git, node_modules, ...), parses each SKILL.md and
     yields ``Skill`` dataclasses.
     (mirrors: agent/skill_utils.py :: iter_skill_index_files /
      EXCLUDED_SKILL_DIRS / extract_skill_description)

  3. Prompt injection / matching ..... ``SkillPreprocessor`` expands template
     vars (``${HERMES_SKILL_DIR}``) and inline-shell snippets, and
     ``match_skills`` / ``build_skill_message`` select the skills whose triggers
     fire for a user query and render them into a prompt block.
     (mirrors: agent/skill_preprocessing.py :: substitute_template_vars /
      expand_inline_shell ; agent/skill_commands.py :: _build_skill_message)

  4. Slash-command exposure .......... ``SkillCommandRegistry`` turns each skill
     name into a ``/slug`` command and resolves a typed command back to a skill.
     (mirrors: agent/skill_commands.py :: scan_skill_commands /
      resolve_skill_command_key / build_skill_invocation_message)

  5. The self-improving part ......... ``SkillCreator`` takes a completed-task
     transcript and, via a *pluggable* LLM callable, synthesizes a brand-new
     SKILL.md which it validates and writes to disk — the same loop the Hermes
     agent runs ("If you've discovered a new way to do something ... save it as
     a skill", prompt_builder.py:150) and that the background Curator later
     consolidates.
     (mirrors: tools/skill_manager_tool.py :: _validate_frontmatter /
      _create_skill ; tools/skill_usage.py :: mark_agent_created ;
      agent/curator.py :: consolidation orchestration)

Everything here is stdlib-only. The real Hermes uses PyYAML; we ship a tiny
frontmatter parser that handles the flat ``key: value`` and inline-list
(``[a, b]``) cases the standard needs, which is all the SKILL.md frontmatter
schema actually requires for name/description/triggers/platforms.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Constants — mirror agent/skill_utils.py
# ─────────────────────────────────────────────────────────────────────────────

#: Directories pruned during discovery so dependencies/VCS/caches can't smuggle
#: in nested skills. Mirror of ``skill_utils.EXCLUDED_SKILL_DIRS``.
EXCLUDED_SKILL_DIRS = frozenset((
    ".git", ".github", ".hub", ".archive", ".venv", "venv",
    "node_modules", "site-packages", "__pycache__",
    ".tox", ".nox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
))

#: agentskills.io / Hermes frontmatter -> OS-platform mapping
#: (mirror of ``skill_utils.PLATFORM_MAP``).
PLATFORM_MAP = {"macos": "darwin", "linux": "linux", "windows": "win32"}

#: The canonical on-disk index filename for a skill.
SKILL_INDEX_FILENAME = "SKILL.md"

#: Max description length enforced on creation (mirror of
#: ``skill_manager_tool.MAX_DESCRIPTION_LENGTH``).
MAX_DESCRIPTION_LENGTH = 1024

#: ``${HERMES_SKILL_DIR}`` / ``${HERMES_SESSION_ID}`` template tokens.
_SKILL_TEMPLATE_RE = re.compile(r"\$\{(HERMES_SKILL_DIR|HERMES_SESSION_ID)\}")

#: Inline shell snippet:  !`date +%Y-%m-%d`   (single line, no newlines)
_INLINE_SHELL_RE = re.compile(r"!`([^`\n]+)`")
_INLINE_SHELL_MAX_OUTPUT = 4000

#: Slug normalization, mirroring agent/skill_commands.py.
_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9-]")
_SLUG_MULTI_HYPHEN = re.compile(r"-{2,}")


# ─────────────────────────────────────────────────────────────────────────────
# Frontmatter parsing — mirror of agent/skill_utils.py::parse_frontmatter
# ─────────────────────────────────────────────────────────────────────────────


def _coerce_scalar(value: str) -> Any:
    """Coerce an unquoted YAML scalar to str / int / float / bool / None."""
    v = value.strip()
    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    return v


def _parse_inline_list(value: str) -> List[Any]:
    """Parse a ``[a, b, c]`` inline YAML list into a Python list."""
    inner = value.strip()[1:-1].strip()
    if not inner:
        return []
    return [_coerce_scalar(part) for part in inner.split(",") if part.strip()]


def parse_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    """Parse YAML frontmatter from a markdown string.

    Mirrors ``agent/skill_utils.py::parse_frontmatter``: a ``---`` fenced block
    at the top of the file is the metadata; everything after is the body. The
    real Hermes hands the fenced block to PyYAML (CSafeLoader); we use a tiny
    stdlib parser that covers the flat ``key: value`` and inline-list forms the
    SKILL.md schema relies on (name, description, triggers, platforms).

    Returns:
        ``(frontmatter_dict, body)``. When there is no frontmatter, returns
        ``({}, original_content)``.
    """
    frontmatter: Dict[str, Any] = {}
    body = content
    if not content.startswith("---"):
        return frontmatter, body

    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return frontmatter, body

    yaml_block = content[3 : end_match.start() + 3]
    body = content[end_match.end() + 3 :]

    for line in yaml_block.strip().split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, raw = stripped.split(":", 1)
        key = key.strip()
        raw = raw.strip()
        if raw.startswith("[") and raw.endswith("]"):
            frontmatter[key] = _parse_inline_list(raw)
        else:
            frontmatter[key] = _coerce_scalar(raw)
    return frontmatter, body


def skill_matches_platform(frontmatter: Dict[str, Any]) -> bool:
    """Return True when the skill is compatible with the current OS.

    Mirror of ``agent/skill_utils.py::skill_matches_platform``. Skills declare
    ``platforms: [macos, linux]`` in frontmatter; an absent/empty field means
    "all platforms" (backward-compatible default).
    """
    platforms = frontmatter.get("platforms")
    if not platforms:
        return True
    if not isinstance(platforms, list):
        platforms = [platforms]
    current = sys.platform
    for platform in platforms:
        mapped = PLATFORM_MAP.get(str(platform).lower().strip(), str(platform).lower().strip())
        if current.startswith(mapped):
            return True
    return False


def extract_skill_description(frontmatter: Dict[str, Any]) -> str:
    """Return a truncated (<=60 char) description for banner/listing display.

    Mirror of ``agent/skill_utils.py::extract_skill_description``.
    """
    raw = frontmatter.get("description", "")
    if not raw:
        return ""
    desc = str(raw).strip().strip("'\"")
    return desc[:57] + "..." if len(desc) > 60 else desc


def slugify(name: str) -> str:
    """Normalize a skill name into a clean ``/slug`` command stem.

    Mirror of the slug logic in ``agent/skill_commands.py::scan_skill_commands``.
    """
    cmd = name.lower().replace(" ", "-").replace("_", "-")
    cmd = _SLUG_INVALID_CHARS.sub("", cmd)
    cmd = _SLUG_MULTI_HYPHEN.sub("-", cmd).strip("-")
    return cmd


# ─────────────────────────────────────────────────────────────────────────────
# Dataclasses — typed I/O surface
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Skill:
    """A parsed skill loaded from a ``<dir>/SKILL.md`` on disk.

    Attributes mirror the fields the Hermes loader/command layer reads off the
    frontmatter (skill_utils / skill_commands).
    """

    name: str
    description: str
    triggers: List[str]
    body: str
    skill_dir: Path
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    agent_created: bool = False  # mirror of skill_usage.is_agent_created

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def command(self) -> str:
        return f"/{self.slug}"


@dataclass
class SkillMatch:
    """A skill selected for a query, with the trigger(s) that fired and a score."""

    skill: Skill
    matched_triggers: List[str]
    score: int


@dataclass
class TaskTranscript:
    """A completed-task transcript handed to :class:`SkillCreator`.

    The real agent draws this from its own conversation/session history; here we
    model the essentials a synthesis prompt needs.
    """

    task: str
    steps: List[str]
    outcome: str
    tags: List[str] = field(default_factory=list)


@dataclass
class CreatedSkill:
    """Result of :meth:`SkillCreator.create_from_transcript`."""

    success: bool
    name: str
    skill_md_path: Optional[Path] = None
    content: str = ""
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# SkillLoader — discovery + parsing (mirror of skill_utils discovery helpers)
# ─────────────────────────────────────────────────────────────────────────────


class SkillLoader:
    """Discovers and parses skills from one or more directories.

    Mirrors ``agent/skill_utils.py``: it walks each root with
    :func:`iter_skill_index_files`-style pruning of ``EXCLUDED_SKILL_DIRS``,
    parses frontmatter, applies platform gating, and de-duplicates by name
    (first directory wins, same as Hermes' local-then-external ordering).
    """

    def __init__(self, skills_dirs: List[Path], *, agent_created_names: Optional[set] = None):
        self.skills_dirs = [Path(d) for d in skills_dirs]
        #: Names the agent itself authored — drives Curator eligibility.
        self.agent_created_names = agent_created_names or set()

    def iter_skill_index_files(self, skills_dir: Path):
        """Yield SKILL.md paths under *skills_dir*, pruning excluded dirs.

        Mirror of ``skill_utils.iter_skill_index_files``.
        """
        matches: List[Path] = []
        for root, dirs, files in os.walk(skills_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_SKILL_DIRS]
            if SKILL_INDEX_FILENAME in files:
                matches.append(Path(root) / SKILL_INDEX_FILENAME)
        for path in sorted(matches, key=lambda p: str(p.relative_to(skills_dir))):
            yield path

    def load(self) -> List[Skill]:
        """Discover, parse, platform-gate and de-duplicate all skills.

        Returns the list of :class:`Skill` objects, in discovery order.
        """
        skills: List[Skill] = []
        seen_names: set = set()
        for skills_dir in self.skills_dirs:
            if not skills_dir.is_dir():
                continue
            for skill_md in self.iter_skill_index_files(skills_dir):
                skill = self._parse_one(skill_md)
                if skill is None:
                    continue
                if skill.name in seen_names:
                    continue  # first dir wins (local before external)
                seen_names.add(skill.name)
                skills.append(skill)
        return skills

    def _parse_one(self, skill_md: Path) -> Optional[Skill]:
        try:
            raw = skill_md.read_text(encoding="utf-8")
        except OSError:
            return None
        frontmatter, body = parse_frontmatter(raw)

        # Name falls back to the directory name, mirroring Hermes.
        name = str(frontmatter.get("name") or skill_md.parent.name)
        if not skill_matches_platform(frontmatter):
            return None

        description = str(frontmatter.get("description") or "").strip()
        if not description:
            # Hermes' fallback: first non-heading body line, truncated.
            for line in body.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#"):
                    description = line[:80]
                    break

        triggers = frontmatter.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [triggers]
        triggers = [str(t).strip() for t in triggers if str(t).strip()]

        return Skill(
            name=name,
            description=description,
            triggers=triggers,
            body=body.strip(),
            skill_dir=skill_md.parent,
            frontmatter=frontmatter,
            agent_created=name in self.agent_created_names,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Trigger matching — used for autonomous, query-driven skill injection
# ─────────────────────────────────────────────────────────────────────────────


def match_skills(query: str, skills: List[Skill]) -> List[SkillMatch]:
    """Select skills whose ``triggers`` fire for *query*, ranked by score.

    Hermes exposes skills to the model by name/description and lets the model
    pick (or the user types ``/slug``); for an autonomous mirror we resolve the
    same intent here with simple case-insensitive substring trigger matching —
    the contract the agentskills.io ``triggers`` field is meant for. Score is the
    number of distinct triggers that fired, so the most specific skill wins.
    """
    q = query.lower()
    results: List[SkillMatch] = []
    for skill in skills:
        fired = [t for t in skill.triggers if t.lower() in q]
        if fired:
            results.append(SkillMatch(skill=skill, matched_triggers=fired, score=len(fired)))
    results.sort(key=lambda m: m.score, reverse=True)
    return results


# ─────────────────────────────────────────────────────────────────────────────
# SkillPreprocessor — template + inline-shell expansion
#   (mirror of agent/skill_preprocessing.py)
# ─────────────────────────────────────────────────────────────────────────────


class SkillPreprocessor:
    """Applies SKILL.md template-var substitution and inline-shell expansion.

    Mirror of ``agent/skill_preprocessing.py``. ``template_vars`` defaults on,
    ``inline_shell`` defaults off — same as Hermes' ``skills`` config, because
    running shell from skill content is opt-in for safety.
    """

    def __init__(self, *, template_vars: bool = True, inline_shell: bool = False,
                 inline_shell_timeout: int = 10):
        self.template_vars = template_vars
        self.inline_shell = inline_shell
        self.inline_shell_timeout = inline_shell_timeout

    def substitute_template_vars(self, content: str, skill_dir: Optional[Path],
                                 session_id: Optional[str]) -> str:
        """Replace ``${HERMES_SKILL_DIR}`` / ``${HERMES_SESSION_ID}`` tokens.

        Unresolved tokens are left in place so the author can spot them — mirror
        of ``skill_preprocessing.substitute_template_vars``.
        """
        if not content:
            return content
        skill_dir_str = str(skill_dir) if skill_dir else None

        def _replace(m: re.Match) -> str:
            token = m.group(1)
            if token == "HERMES_SKILL_DIR" and skill_dir_str:
                return skill_dir_str
            if token == "HERMES_SESSION_ID" and session_id:
                return str(session_id)
            return m.group(0)

        return _SKILL_TEMPLATE_RE.sub(_replace, content)

    def expand_inline_shell(self, content: str, skill_dir: Optional[Path]) -> str:
        """Replace every ``!`cmd`` snippet with its stdout (CWD = skill_dir).

        Mirror of ``skill_preprocessing.expand_inline_shell`` /
        ``run_inline_shell``: failures return a short marker instead of raising.
        """
        if "!`" not in content:
            return content

        def _replace(m: re.Match) -> str:
            cmd = m.group(1).strip()
            if not cmd:
                return ""
            try:
                completed = subprocess.run(
                    ["bash", "-c", cmd],
                    cwd=str(skill_dir) if skill_dir else None,
                    capture_output=True, text=True,
                    timeout=max(1, self.inline_shell_timeout), check=False,
                )
            except subprocess.TimeoutExpired:
                return f"[inline-shell timeout after {self.inline_shell_timeout}s: {cmd}]"
            except FileNotFoundError:
                return "[inline-shell error: bash not found]"
            except Exception as exc:  # noqa: BLE001 — match Hermes' fail-soft
                return f"[inline-shell error: {exc}]"
            out = (completed.stdout or "").rstrip("\n")
            if not out and completed.stderr:
                out = completed.stderr.rstrip("\n")
            if len(out) > _INLINE_SHELL_MAX_OUTPUT:
                out = out[:_INLINE_SHELL_MAX_OUTPUT] + "...[truncated]"
            return out

        return _INLINE_SHELL_RE.sub(_replace, content)

    def preprocess(self, content: str, skill_dir: Optional[Path],
                   session_id: Optional[str] = None) -> str:
        """Apply configured template + inline-shell preprocessing in order.

        Mirror of ``skill_preprocessing.preprocess_skill_content``.
        """
        if not content:
            return content
        if self.template_vars:
            content = self.substitute_template_vars(content, skill_dir, session_id)
        if self.inline_shell:
            content = self.expand_inline_shell(content, skill_dir)
        return content


def build_skill_message(skill: Skill, preprocessor: SkillPreprocessor,
                        activation_note: str, *, user_instruction: str = "",
                        session_id: Optional[str] = None) -> str:
    """Render one loaded skill into a prompt block injected into the message.

    Mirror of ``agent/skill_commands.py::_build_skill_message``: preprocess the
    body, then prepend an activation note and append a ``[Skill directory: ...]``
    hint so the agent can resolve the skill's bundled scripts/templates.
    """
    content = preprocessor.preprocess(skill.body, skill.skill_dir, session_id)
    parts: List[str] = [activation_note, "", content.strip()]
    parts.append("")
    parts.append(f"[Skill directory: {skill.skill_dir}]")
    parts.append(
        "Resolve any relative paths in this skill (e.g. `scripts/foo.py`, "
        "`templates/config.yaml`) against that directory, then run them by "
        "absolute path."
    )
    if user_instruction:
        parts.append("")
        parts.append(
            "The user provided the following instruction alongside the skill "
            f"invocation: {user_instruction}"
        )
    return "\n".join(parts)


def build_injection_prompt(query: str, skills: List[Skill],
                           preprocessor: SkillPreprocessor,
                           session_id: Optional[str] = None) -> str:
    """Autonomous path: match *query* against triggers and inject the winners.

    Returns a single prompt block containing every matched skill, in score
    order. This is the "preprocessing step that injects matching skills into the
    prompt" the feature description asks for.
    """
    matches = match_skills(query, skills)
    if not matches:
        return ""
    blocks: List[str] = []
    for m in matches:
        note = (
            f'[Skill "{m.skill.name}" auto-activated — its triggers '
            f"({', '.join(m.matched_triggers)}) matched the request. "
            "Follow its instructions below.]"
        )
        blocks.append(build_skill_message(m.skill, preprocessor, note, session_id=session_id))
    return "\n\n".join(blocks)


# ─────────────────────────────────────────────────────────────────────────────
# SkillCommandRegistry — slash-command exposure
#   (mirror of agent/skill_commands.py scan/resolve/build)
# ─────────────────────────────────────────────────────────────────────────────


class SkillCommandRegistry:
    """Exposes loaded skills as ``/slug`` slash commands.

    Mirror of the ``_skill_commands`` map built by
    ``agent/skill_commands.py::scan_skill_commands`` plus
    ``resolve_skill_command_key`` / ``build_skill_invocation_message``.
    """

    def __init__(self, skills: List[Skill], preprocessor: Optional[SkillPreprocessor] = None):
        self.preprocessor = preprocessor or SkillPreprocessor()
        self._commands: Dict[str, Skill] = {}
        self.scan(skills)

    def scan(self, skills: List[Skill]) -> Dict[str, Skill]:
        """Build the ``{"/slug": Skill}`` command map (first name wins)."""
        self._commands = {}
        seen: set = set()
        for skill in skills:
            slug = skill.slug
            if not slug or skill.name in seen:
                continue
            seen.add(skill.name)
            self._commands[f"/{slug}"] = skill
        return self._commands

    @property
    def commands(self) -> Dict[str, Skill]:
        return dict(self._commands)

    def resolve(self, command: str) -> Optional[str]:
        """Resolve a user-typed command to its canonical ``/slug`` key.

        Hyphens and underscores are interchangeable (Telegram converts hyphens
        to underscores), mirroring ``resolve_skill_command_key``.
        """
        if not command:
            return None
        bare = command.lstrip("/").replace("_", "-")
        key = f"/{bare}"
        return key if key in self._commands else None

    def build_invocation_message(self, command: str, user_instruction: str = "",
                                 session_id: Optional[str] = None) -> Optional[str]:
        """Build the user-message payload for a slash-command invocation.

        Mirror of ``build_skill_invocation_message``: returns ``None`` if the
        command doesn't resolve to a skill.
        """
        key = self.resolve(command)
        if key is None:
            return None
        skill = self._commands[key]
        note = (
            f'[IMPORTANT: The user invoked the "{skill.name}" skill, indicating '
            "they want you to follow its instructions. Full content below.]"
        )
        return build_skill_message(
            skill, self.preprocessor, note,
            user_instruction=user_instruction, session_id=session_id,
        )


# ─────────────────────────────────────────────────────────────────────────────
# SkillCreator — the self-improving part
#   (mirror of tools/skill_manager_tool.py::_create_skill +
#    tools/skill_usage.py::mark_agent_created, driven by the agent prompt
#    "save it as a skill with the skill tool" — prompt_builder.py:150)
# ─────────────────────────────────────────────────────────────────────────────

#: Pluggable LLM signature: takes a synthesis prompt, returns SKILL.md text.
LLMCallable = Callable[[str], str]

SKILL_SYNTHESIS_PROMPT_TEMPLATE = """\
You just completed a task. Crystallize what you learned into a reusable skill.

Produce a complete SKILL.md file: YAML frontmatter (--- fenced) followed by a
markdown body. Required frontmatter fields: name, description, triggers (a list
of short phrases that should activate this skill). Keep the description under
{max_desc} characters. The body should be a CLASS-LEVEL, reusable procedure —
not a one-off log of this exact session.

Completed task: {task}

Steps that worked:
{steps}

Outcome: {outcome}
Suggested triggers/tags: {tags}
"""


def validate_frontmatter(content: str) -> Optional[str]:
    """Validate a candidate SKILL.md the way Hermes does before writing it.

    Mirror of ``tools/skill_manager_tool.py::_validate_frontmatter``: requires a
    closed ``---`` block, a YAML mapping carrying ``name`` and ``description``
    (within length), and a non-empty body. Returns an error string or ``None``.
    """
    if not content.strip():
        return "Content cannot be empty."
    if not content.startswith("---"):
        return "SKILL.md must start with YAML frontmatter (---)."
    end_match = re.search(r"\n---\s*\n", content[3:])
    if not end_match:
        return "SKILL.md frontmatter is not closed (missing a second '---')."
    frontmatter, body = parse_frontmatter(content)
    if not isinstance(frontmatter, dict) or not frontmatter:
        return "Frontmatter must be a YAML mapping (key: value pairs)."
    if "name" not in frontmatter:
        return "Frontmatter must include 'name' field."
    if "description" not in frontmatter:
        return "Frontmatter must include 'description' field."
    if len(str(frontmatter["description"])) > MAX_DESCRIPTION_LENGTH:
        return f"Description exceeds {MAX_DESCRIPTION_LENGTH} characters."
    if not body.strip():
        return "SKILL.md must have content after the frontmatter."
    return None


class SkillCreator:
    """Turns a completed-task transcript into a new SKILL.md on disk.

    This is the self-improving loop in miniature:

      transcript -> synthesis prompt -> LLM -> SKILL.md -> validate -> write
                 -> mark agent_created (so the Curator can later consolidate).

    The LLM is injected as a plain callable so the whole thing is mockable in
    tests/demos (mirrors Hermes' auxiliary-client indirection used by the
    Curator in ``agent/curator.py``).
    """

    def __init__(self, skills_dir: Path, llm: LLMCallable):
        self.skills_dir = Path(skills_dir)
        self.llm = llm
        #: Names this creator authored — feeds ``Skill.agent_created`` / Curator.
        self.agent_created_names: set = set()

    def build_synthesis_prompt(self, transcript: TaskTranscript) -> str:
        """Render the transcript into the skill-synthesis prompt."""
        steps = "\n".join(f"- {s}" for s in transcript.steps) or "- (none recorded)"
        return SKILL_SYNTHESIS_PROMPT_TEMPLATE.format(
            max_desc=MAX_DESCRIPTION_LENGTH,
            task=transcript.task,
            steps=steps,
            outcome=transcript.outcome,
            tags=", ".join(transcript.tags) or "(none)",
        )

    def create_from_transcript(self, transcript: TaskTranscript,
                               category: Optional[str] = None) -> CreatedSkill:
        """Synthesize, validate and write a new skill from *transcript*.

        Mirror of ``_create_skill``: validate frontmatter, reject name
        collisions, write ``<dir>/SKILL.md``, and record the skill as
        agent-created. Returns a :class:`CreatedSkill`.
        """
        prompt = self.build_synthesis_prompt(transcript)
        content = self.llm(prompt)

        err = validate_frontmatter(content)
        if err:
            return CreatedSkill(success=False, name="", error=err, content=content)

        frontmatter, _ = parse_frontmatter(content)
        name = str(frontmatter["name"]).strip()
        slug = slugify(name)
        if not slug:
            return CreatedSkill(success=False, name=name,
                                error="Skill name normalizes to an empty slug.")

        skill_dir = (self.skills_dir / category / slug) if category else (self.skills_dir / slug)
        skill_md = skill_dir / SKILL_INDEX_FILENAME
        if skill_md.exists():
            return CreatedSkill(success=False, name=name,
                                error=f"A skill named '{name}' already exists at {skill_dir}.")

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md.write_text(content, encoding="utf-8")
        self.agent_created_names.add(name)  # mirror skill_usage.mark_agent_created

        return CreatedSkill(success=True, name=name, skill_md_path=skill_md, content=content)
