"""Filesystem skill discovery — scans GRAVITYCLAW_HOME/skills/ for skill directories.

Each skill directory must contain a SKILL.md file. Optionally includes:
- skill.json (metadata overrides)
- references/ (supporting documents)
- scripts/ (automation)
- .history/ (revision snapshots)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

SKILL_FILENAME = "SKILL.md"
SKILL_META_FILENAME = "skill.json"
HISTORY_DIR = ".history"


@dataclass(frozen=True, slots=True)
class DiscoveredSkill:
    """A skill found on the filesystem during discovery."""
    name: str
    description: str
    path: str  # Relative to GRAVITYCLAW_HOME (e.g., "skills/modal-deployment")
    absolute_path: Path
    has_meta: bool
    meta: dict[str, Any]


def discover_skills(home: Path) -> list[DiscoveredSkill]:
    """Scan GRAVITYCLAW_HOME/skills/ for valid skill directories.

    A valid skill directory is one that contains SKILL.md.
    Returns metadata extracted from SKILL.md header + optional skill.json.
    """
    skills_dir = home / "skills"
    if not skills_dir.is_dir():
        return []

    discovered: list[DiscoveredSkill] = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue

        skill_md = entry / SKILL_FILENAME
        if not skill_md.is_file():
            LOGGER.debug("skipping %s: no SKILL.md", entry.name)
            continue

        # Extract description from first non-header line of SKILL.md
        description = _extract_description(skill_md)

        # Load optional skill.json
        meta: dict[str, Any] = {}
        meta_path = entry / SKILL_META_FILENAME
        has_meta = meta_path.is_file()
        if has_meta:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                LOGGER.warning("invalid skill.json in %s: %s", entry.name, exc)
                meta = {}

        # skill.json can override description
        if meta.get("description"):
            description = str(meta["description"])

        rel_path = f"skills/{entry.name}"
        discovered.append(DiscoveredSkill(
            name=entry.name,
            description=description,
            path=rel_path,
            absolute_path=entry,
            has_meta=has_meta,
            meta=meta,
        ))

    return discovered


def read_skill_content(home: Path, skill_name: str) -> str | None:
    """Read the full SKILL.md content for a named skill.

    Returns None if the skill directory or SKILL.md doesn't exist.
    """
    skill_md = home / "skills" / skill_name / SKILL_FILENAME
    if not skill_md.is_file():
        return None
    try:
        return skill_md.read_text(encoding="utf-8")
    except OSError:
        return None


def read_skill_file(home: Path, skill_name: str, relative_path: str) -> str | None:
    """Read a specific file within a skill directory (e.g., references/debugging.md).

    Validates that the path stays within the skill directory (no traversal).
    """
    skill_dir = home / "skills" / skill_name
    target = (skill_dir / relative_path).resolve()

    # Prevent path traversal
    try:
        target.relative_to(skill_dir.resolve())
    except ValueError:
        return None

    if not target.is_file():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except OSError:
        return None


def ensure_skill_directory(home: Path, skill_name: str) -> Path:
    """Create the skill directory structure if it doesn't exist.

    Returns the absolute path to the skill directory.
    """
    skill_dir = home / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "references").mkdir(exist_ok=True)
    (skill_dir / "scripts").mkdir(exist_ok=True)
    (skill_dir / HISTORY_DIR).mkdir(exist_ok=True)
    return skill_dir


def _extract_description(skill_md: Path) -> str:
    """Extract a short description from the SKILL.md content.

    Strategy: use the first non-empty, non-heading line after the title.
    """
    try:
        content = skill_md.read_text(encoding="utf-8")
    except OSError:
        return ""

    lines = content.splitlines()
    past_title = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            past_title = True
            continue
        if past_title:
            return stripped[:200]
    return ""
