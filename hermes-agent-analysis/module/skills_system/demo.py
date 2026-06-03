#!/usr/bin/env python3
"""
demo.py — end-to-end walkthrough of the Hermes skills-system mirror.

Run:  python3 demo.py        (no external dependencies)

It:
  1. writes a couple of fake SKILL.md files into a temp skills dir,
  2. discovers + parses them with SkillLoader,
  3. matches a user query against triggers and injects the right skill,
  4. registers + invokes slash-commands,
  5. has SkillCreator synthesize a brand-new skill from a fake transcript
     (mock LLM), and prints the skill store evolving.
"""

import tempfile
from pathlib import Path

from skills_system import (
    SkillCommandRegistry,
    SkillCreator,
    SkillLoader,
    SkillPreprocessor,
    TaskTranscript,
    build_injection_prompt,
    match_skills,
)

# ── fake on-disk skills ──────────────────────────────────────────────────────

PDF_SKILL = """\
---
name: pdf-extract
description: "Extract text and tables from PDF files reliably."
triggers: [pdf, extract pdf, parse pdf, pdf table]
platforms: [linux, macos, windows]
---

# PDF Extraction

## Overview
Pull clean text and tables out of a PDF.

## Procedure
1. Run `scripts/extract.py` against the input PDF.
2. Prefer table-aware extraction for tabular pages.
3. Validate the output is non-empty before returning.

Skill files live under: ${HERMES_SKILL_DIR}
"""

GIT_SKILL = """\
---
name: git-bisect
description: "Find the commit that introduced a bug with git bisect."
triggers: [bisect, find bad commit, regression hunt]
---

# Git Bisect

## Procedure
1. `git bisect start`, mark a known-good and known-bad commit.
2. Test each midpoint; mark good/bad.
3. `git bisect reset` when the culprit is found.
"""


def seed_skills(root: Path) -> None:
    (root / "data-science" / "pdf-extract").mkdir(parents=True, exist_ok=True)
    (root / "data-science" / "pdf-extract" / "SKILL.md").write_text(PDF_SKILL, encoding="utf-8")
    (root / "software-development" / "git-bisect").mkdir(parents=True, exist_ok=True)
    (root / "software-development" / "git-bisect" / "SKILL.md").write_text(GIT_SKILL, encoding="utf-8")
    # An excluded dir that must NOT be discovered:
    (root / "node_modules" / "evil").mkdir(parents=True, exist_ok=True)
    (root / "node_modules" / "evil" / "SKILL.md").write_text(
        "---\nname: evil\ndescription: should be skipped\n---\nnope", encoding="utf-8")


def mock_llm(prompt: str) -> str:
    """A deterministic stand-in for the auxiliary model.

    A real Hermes Curator/creator would call an LLM here; we synthesize a
    plausible SKILL.md so the demo is fully offline.
    """
    assert "Completed task" in prompt  # sanity: it got the synthesis prompt
    return """\
---
name: csv-dedupe
description: "Deduplicate rows in a CSV while preserving column order."
triggers: [dedupe csv, remove duplicate rows, csv duplicates]
---

# CSV Deduplication

## Overview
Remove duplicate rows from a CSV without disturbing column order.

## Procedure
1. Read the CSV with the stdlib `csv` module.
2. Track seen rows by a tuple of their values.
3. Write unique rows back, header first.
"""


def rule(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hermes_skills_"))
    skills_root = tmp / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    seed_skills(skills_root)

    # 1. DISCOVERY ------------------------------------------------------------
    rule("1. DISCOVERY — SkillLoader walks the tree, prunes excluded dirs")
    loader = SkillLoader([skills_root])
    skills = loader.load()
    print(f"skills dir: {skills_root}")
    print(f"discovered {len(skills)} skills (node_modules/evil correctly skipped):")
    for s in skills:
        print(f"  {s.command:18} {s.name:14} triggers={s.triggers}")

    # 2. MATCH + INJECT -------------------------------------------------------
    rule("2. MATCH + INJECT — autonomous trigger matching for a user query")
    pre = SkillPreprocessor(template_vars=True)  # expands ${HERMES_SKILL_DIR}
    query = "Can you parse pdf invoices and pull out the pdf table totals?"
    print(f"query: {query!r}")
    matches = match_skills(query, skills)
    for m in matches:
        print(f"  matched {m.skill.command} (score={m.score}) via {m.matched_triggers}")
    injected = build_injection_prompt(query, skills, pre)
    print("\n--- injected prompt block (truncated) ---")
    print("\n".join(injected.splitlines()[:8]))
    print("  ...")
    assert "[Skill directory:" in injected and "node_modules" not in str(skills_root.name)

    # 3. SLASH COMMANDS -------------------------------------------------------
    rule("3. SLASH COMMANDS — register + invoke /slug")
    registry = SkillCommandRegistry(skills, pre)
    print("registered commands:", ", ".join(sorted(registry.commands)))
    # underscores/hyphens interchangeable, leading slash optional:
    print("resolve('git_bisect') ->", registry.resolve("git_bisect"))
    msg = registry.build_invocation_message("/pdf-extract", user_instruction="invoice.pdf")
    print("\n--- /pdf-extract invocation message (truncated) ---")
    print("\n".join(msg.splitlines()[:5]))
    print("  ...")
    assert registry.build_invocation_message("/does-not-exist") is None

    # 4. SELF-IMPROVING CREATION ---------------------------------------------
    rule("4. SELF-IMPROVING — SkillCreator synthesizes a new skill from a transcript")
    print(f"skill store before: {sorted(s.name for s in skills)}")
    creator = SkillCreator(skills_root, llm=mock_llm)
    transcript = TaskTranscript(
        task="Clean a messy CSV export that had duplicate rows.",
        steps=[
            "Loaded the CSV with the csv module.",
            "Tracked seen rows by value tuple.",
            "Wrote unique rows back, header first.",
        ],
        outcome="Duplicates removed, column order preserved.",
        tags=["csv", "dedupe", "data-cleaning"],
    )
    created = creator.create_from_transcript(transcript, category="data-science")
    print(f"created.success={created.success}  name={created.name!r}")
    print(f"written to: {created.skill_md_path}")
    print(f"agent_created_names: {sorted(creator.agent_created_names)}")

    # Re-discover so the new skill becomes a first-class store member.
    loader2 = SkillLoader([skills_root], agent_created_names=creator.agent_created_names)
    skills_after = loader2.load()
    print(f"\nskill store after re-discovery: {sorted(s.name for s in skills_after)}")
    new_skill = next(s for s in skills_after if s.name == "csv-dedupe")
    print(f"  new skill agent_created={new_skill.agent_created}  command={new_skill.command}")

    # The newly-created skill is now immediately usable via its triggers:
    rule("5. CLOSING THE LOOP — the new skill is now matchable + invocable")
    q2 = "help me remove duplicate rows from this file (dedupe csv)"
    follow = match_skills(q2, skills_after)
    print(f"query: {q2!r}")
    for m in follow:
        print(f"  -> {m.skill.command} fired on {m.matched_triggers}")
    assert any(m.skill.name == "csv-dedupe" for m in follow), "new skill should match"

    print("\nDemo complete. Temp dir:", tmp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
