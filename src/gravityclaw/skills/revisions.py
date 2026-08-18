"""Skill revision history and rollback support.

Revision snapshots live on the filesystem:
  skills/<name>/.history/
    ├── 000001.SKILL.md
    ├── 000002.SKILL.md
    ├── 000003.patch   (optional diff representation)
    └── manifest.jsonl

The registry records structured revision metadata in SQLite.
This module provides rollback and history inspection.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .discovery import HISTORY_DIR, SKILL_FILENAME
from .models import SkillRevisionRecord
from .registry import SkillRegistry

LOGGER = logging.getLogger(__name__)


class RevisionService:
    """Manages skill revision history, snapshots, and rollback."""

    def __init__(self, registry: SkillRegistry, home: Path) -> None:
        self._registry = registry
        self._home = home

    def get_revision_content(self, skill_name: str, revision: int) -> str | None:
        """Read the SKILL.md snapshot for a specific revision from .history/.

        Returns None if the snapshot doesn't exist.
        """
        history_dir = self._home / "skills" / skill_name / HISTORY_DIR
        snapshot_path = history_dir / f"{revision:06d}.SKILL.md"
        if not snapshot_path.is_file():
            return None
        try:
            return snapshot_path.read_text(encoding="utf-8")
        except OSError:
            return None

    def list_revision_snapshots(self, skill_name: str) -> list[int]:
        """List available revision numbers that have filesystem snapshots."""
        history_dir = self._home / "skills" / skill_name / HISTORY_DIR
        if not history_dir.is_dir():
            return []
        revisions: list[int] = []
        for path in sorted(history_dir.iterdir()):
            if path.suffix == ".md" and path.stem.endswith(".SKILL"):
                # Parse "000007.SKILL" → 7
                try:
                    num_str = path.stem.replace(".SKILL", "")
                    revisions.append(int(num_str))
                except ValueError:
                    continue
            elif path.suffix == ".md" and path.stem.isdigit():
                # Alternate format: "000007.SKILL.md" vs "000007.md"
                pass
        # Also handle the format we actually use: "000007.SKILL.md"
        # where the full filename is "000007.SKILL.md"
        if not revisions:
            for path in sorted(history_dir.iterdir()):
                name = path.name
                if name.endswith(".SKILL.md"):
                    try:
                        num_str = name.replace(".SKILL.md", "")
                        revisions.append(int(num_str))
                    except ValueError:
                        continue
        return sorted(set(revisions))

    def rollback(
        self,
        skill_name: str,
        target_revision: int,
        *,
        reason: str = "manual rollback",
        source_run_id: str | None = None,
    ) -> int:
        """Rollback a skill to a previous revision.

        Reads the snapshot from .history/, writes it as the current SKILL.md,
        bumps the revision counter, and records the rollback.

        Returns the new revision number.
        Raises KeyError if skill not found.
        Raises ValueError if target revision snapshot doesn't exist.
        """
        # Get the skill record
        skill = self._registry.get_skill_by_name(skill_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_name}")

        # Load the target revision content
        content = self.get_revision_content(skill_name, target_revision)
        if content is None:
            raise ValueError(
                f"no snapshot found for revision {target_revision} of skill '{skill_name}'"
            )

        # Write the restored content as current SKILL.md
        skill_dir = self._home / "skills" / skill_name
        skill_md_path = skill_dir / SKILL_FILENAME
        from .proposals import _atomic_write, _append_manifest
        _atomic_write(skill_md_path, content)

        # Bump revision
        new_revision = skill.revision + 1

        # Snapshot the new revision (same content as target, but new number)
        history_dir = skill_dir / HISTORY_DIR
        history_dir.mkdir(exist_ok=True)
        _atomic_write(
            history_dir / f"{new_revision:06d}.SKILL.md",
            content,
        )
        _append_manifest(history_dir, new_revision, "rollback", reason)

        # Update registry
        self._registry.update_skill(skill.skill_id, revision=new_revision)

        # Record revision in DB
        self._registry.record_revision(
            skill_id=skill.skill_id,
            revision=new_revision,
            operation="rollback",
            reason=f"rollback to revision {target_revision}: {reason}",
            parent_revision=skill.revision,
            source_run_id=source_run_id,
        )

        LOGGER.info(
            "skill '%s' rolled back: rev %d → %d (restored from rev %d)",
            skill_name, skill.revision, new_revision, target_revision,
        )
        return new_revision
