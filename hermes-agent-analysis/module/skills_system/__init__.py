"""Hermes Agent skills-system mirror (stdlib-only, runnable).

Re-exports the public surface so callers can do::

    from skills_system import SkillLoader, SkillCreator, match_skills
"""

from .skills_system import (
    EXCLUDED_SKILL_DIRS,
    MAX_DESCRIPTION_LENGTH,
    PLATFORM_MAP,
    SKILL_INDEX_FILENAME,
    CreatedSkill,
    LLMCallable,
    Skill,
    SkillCommandRegistry,
    SkillCreator,
    SkillLoader,
    SkillMatch,
    SkillPreprocessor,
    TaskTranscript,
    build_injection_prompt,
    build_skill_message,
    extract_skill_description,
    match_skills,
    parse_frontmatter,
    skill_matches_platform,
    slugify,
    validate_frontmatter,
)

__all__ = [
    "EXCLUDED_SKILL_DIRS",
    "MAX_DESCRIPTION_LENGTH",
    "PLATFORM_MAP",
    "SKILL_INDEX_FILENAME",
    "CreatedSkill",
    "LLMCallable",
    "Skill",
    "SkillCommandRegistry",
    "SkillCreator",
    "SkillLoader",
    "SkillMatch",
    "SkillPreprocessor",
    "TaskTranscript",
    "build_injection_prompt",
    "build_skill_message",
    "extract_skill_description",
    "match_skills",
    "parse_frontmatter",
    "skill_matches_platform",
    "slugify",
    "validate_frontmatter",
]
