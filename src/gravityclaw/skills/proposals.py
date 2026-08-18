"""Skill proposal lifecycle — create, approve, reject with transactional guarantees.

Approval is transactional: validates base revision, atomically mutates the filesystem,
updates the registry, records a revision snapshot, and emits an audit event.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from .discovery import HISTORY_DIR, SKILL_FILENAME, ensure_skill_directory
from .models import ProposalStatus, SkillOwner, SkillState, SkillTrust
from .registry import SkillRegistry, StaleRevisionError

LOGGER = logging.getLogger(__name__)


class ProposalService:
    """Manages the lifecycle of skill proposals with transactional approval."""

    def __init__(self, registry: SkillRegistry, home: Path) -> None:
        self._registry = registry
        self._home = home

    def approve(
        self,
        proposal_id: str,
        *,
        reason: str | None = None,
    ) -> str:
        """Approve a pending proposal — transactionally create or patch the skill.

        Returns the skill_id of the created/updated skill.
        Raises StaleRevisionError if base_revision doesn't match current.
        Raises KeyError if proposal not found.
        Raises ValueError if proposal already resolved.
        """
        proposal = self._registry.get_proposal(proposal_id)

        if proposal.operation == "create":
            return self._approve_create(proposal, reason)
        elif proposal.operation == "patch":
            return self._approve_patch(proposal, reason)
        elif proposal.operation == "archive":
            return self._approve_archive(proposal, reason)
        else:
            raise ValueError(f"unsupported proposal operation: {proposal.operation}")

    def reject(
        self,
        proposal_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        """Reject a pending proposal — no filesystem mutation."""
        self._registry.resolve_proposal(
            proposal_id, ProposalStatus.REJECTED, reason=reason,
        )

    def _approve_create(self, proposal, reason: str | None) -> str:
        """Handle approval of a 'create' proposal."""
        # Check if a skill with this name already exists
        existing = self._registry.get_skill_by_name(proposal.skill_name)
        if existing is not None:
            self._registry.resolve_proposal(
                proposal.id, ProposalStatus.CONFLICT,
                reason=f"skill '{proposal.skill_name}' already exists",
            )
            raise StaleRevisionError(
                f"skill '{proposal.skill_name}' already exists"
            )

        # Create the filesystem structure
        skill_dir = ensure_skill_directory(self._home, proposal.skill_name)
        skill_md_path = skill_dir / SKILL_FILENAME

        # Write SKILL.md atomically
        _atomic_write(skill_md_path, proposal.content)

        # Write skill.json metadata
        meta = {
            "name": proposal.skill_name,
            "description": proposal.description,
            "owner": SkillOwner.AGENT,
            "source_run_id": proposal.source_run_id,
            "review_model": proposal.review_model,
        }
        _atomic_write(skill_dir / "skill.json", json.dumps(meta, indent=2))

        # Register in the metadata layer
        rel_path = f"skills/{proposal.skill_name}"
        skill = self._registry.register_skill(
            name=proposal.skill_name,
            description=proposal.description,
            path=rel_path,
            owner=SkillOwner.AGENT,
            trust=SkillTrust.APPROVED,
        )

        # Bump revision to 1
        self._registry.update_skill(skill.skill_id, revision=1)

        # Record revision history
        self._registry.record_revision(
            skill_id=skill.skill_id,
            revision=1,
            operation="create",
            reason=proposal.reason,
            parent_revision=None,
            source_run_id=proposal.source_run_id,
            proposal_id=proposal.id,
            model=proposal.review_model,
        )

        # Snapshot revision 1 in .history/
        history_dir = skill_dir / HISTORY_DIR
        history_dir.mkdir(exist_ok=True)
        _atomic_write(
            history_dir / "000001.SKILL.md",
            proposal.content,
        )
        _append_manifest(history_dir, 1, "create", proposal.reason)

        # Resolve the proposal
        self._registry.resolve_proposal(
            proposal.id, ProposalStatus.APPROVED, reason=reason,
        )

        LOGGER.info(
            "skill proposal %s approved: created skill '%s' (id=%s)",
            proposal.id, proposal.skill_name, skill.skill_id,
        )
        return skill.skill_id

    def _approve_patch(self, proposal, reason: str | None) -> str:
        """Handle approval of a 'patch' proposal."""
        if proposal.skill_id is None:
            # Try to find by name
            existing = self._registry.get_skill_by_name(proposal.skill_name)
            if existing is None:
                self._registry.resolve_proposal(
                    proposal.id, ProposalStatus.CONFLICT,
                    reason=f"skill '{proposal.skill_name}' not found",
                )
                raise KeyError(f"skill '{proposal.skill_name}' not found for patch")
            skill = existing
        else:
            skill = self._registry.get_skill(proposal.skill_id)

        # Validate base revision (stale-patch protection)
        if proposal.base_revision is not None and proposal.base_revision != skill.revision:
            self._registry.resolve_proposal(
                proposal.id, ProposalStatus.CONFLICT,
                reason=f"stale base_revision: proposal has {proposal.base_revision}, "
                       f"skill is at {skill.revision}",
            )
            raise StaleRevisionError(
                f"proposal base_revision={proposal.base_revision} "
                f"but skill is at revision={skill.revision}"
            )

        # Check ownership — user-owned skills cannot be auto-patched
        if skill.owner == SkillOwner.USER:
            self._registry.resolve_proposal(
                proposal.id, ProposalStatus.REJECTED,
                reason="user-owned skills cannot be automatically patched",
            )
            raise ValueError("user-owned skills cannot be automatically patched")

        # Filesystem mutation
        skill_dir = self._home / "skills" / skill.name
        skill_md_path = skill_dir / SKILL_FILENAME
        new_revision = skill.revision + 1

        # Snapshot old content before overwrite
        history_dir = skill_dir / HISTORY_DIR
        history_dir.mkdir(exist_ok=True)
        if skill_md_path.is_file():
            old_content = skill_md_path.read_text(encoding="utf-8")
            _atomic_write(
                history_dir / f"{skill.revision:06d}.SKILL.md",
                old_content,
            )

        # Write new content
        _atomic_write(skill_md_path, proposal.content)

        # Snapshot new revision
        _atomic_write(
            history_dir / f"{new_revision:06d}.SKILL.md",
            proposal.content,
        )
        _append_manifest(history_dir, new_revision, "patch", proposal.reason)

        # Update registry
        self._registry.update_skill(
            skill.skill_id,
            revision=new_revision,
            description=proposal.description or skill.description,
        )

        # Record revision
        self._registry.record_revision(
            skill_id=skill.skill_id,
            revision=new_revision,
            operation="patch",
            reason=proposal.reason,
            parent_revision=skill.revision,
            source_run_id=proposal.source_run_id,
            proposal_id=proposal.id,
            model=proposal.review_model,
        )

        # Resolve proposal
        self._registry.resolve_proposal(
            proposal.id, ProposalStatus.APPROVED, reason=reason,
        )

        LOGGER.info(
            "skill proposal %s approved: patched skill '%s' rev %d→%d",
            proposal.id, skill.name, skill.revision, new_revision,
        )
        return skill.skill_id

    def _approve_archive(self, proposal, reason: str | None) -> str:
        """Handle approval of an 'archive' proposal."""
        if proposal.skill_id is None:
            existing = self._registry.get_skill_by_name(proposal.skill_name)
            if existing is None:
                raise KeyError(f"skill '{proposal.skill_name}' not found for archive")
            skill = existing
        else:
            skill = self._registry.get_skill(proposal.skill_id)

        # Archive the skill in registry
        self._registry.update_skill(skill.skill_id, state=SkillState.ARCHIVED)

        # Record revision
        new_revision = skill.revision + 1
        self._registry.update_skill(skill.skill_id, revision=new_revision)
        self._registry.record_revision(
            skill_id=skill.skill_id,
            revision=new_revision,
            operation="archive",
            reason=proposal.reason,
            parent_revision=skill.revision,
            source_run_id=proposal.source_run_id,
            proposal_id=proposal.id,
            model=proposal.review_model,
        )

        # Resolve proposal
        self._registry.resolve_proposal(
            proposal.id, ProposalStatus.APPROVED, reason=reason,
        )

        LOGGER.info(
            "skill proposal %s approved: archived skill '%s'",
            proposal.id, skill.name,
        )
        return skill.skill_id


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem utilities
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically via temp file + rename."""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


def _append_manifest(history_dir: Path, revision: int, operation: str, reason: str) -> None:
    """Append a line to manifest.jsonl in the .history/ directory."""
    from ..store import utc_now
    entry = json.dumps({
        "revision": revision,
        "operation": operation,
        "reason": reason,
        "created_at": utc_now(),
    }, ensure_ascii=False)
    manifest = history_dir / "manifest.jsonl"
    with manifest.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")
