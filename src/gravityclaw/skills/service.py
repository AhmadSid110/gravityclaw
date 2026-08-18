"""SkillService — unified facade for the skill subsystem.

All LLM-facing tools and the learning engine dispatch through this service
rather than manipulating files or registry directly. This preserves the
core invariant: model → typed operation → validated application.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..store import Store
from .discovery import DiscoveredSkill, discover_skills, read_skill_content, read_skill_file
from .models import (
    ProposalStatus,
    SkillOperation,
    SkillOwner,
    SkillRecord,
    SkillRevisionRecord,
    SkillState,
    SkillTrust,
)
from .proposals import ProposalService
from .registry import SkillRegistry, StaleRevisionError
from .revisions import RevisionService
from .runtime import (
    LoadedSkill,
    PromptIntegration,
    RunSkillContext,
    SkillCandidate,
    SkillDiscovery,
    SkillManageResult,
    SkillViewResult,
    skill_manage,
    skill_view,
)
from .telemetry import TelemetryService
from .trust import (
    OperationContext,
    OperationKind,
    TrustDecision,
    TrustMode,
    TrustPolicy,
)

LOGGER = logging.getLogger(__name__)


class SkillService:
    """Unified facade for all skill operations.

    Exposes a clean API for:
    - Discovery (filesystem scan)
    - Registry CRUD
    - Proposal lifecycle (create, approve, reject)
    - Revision history + rollback
    - Telemetry
    - Processing skill operations from the learning reviewer
    """

    def __init__(
        self,
        store: Store,
        home: Path,
        *,
        create_approval_required: bool = True,
        modify_approval_required: bool = True,
        trust_policy: TrustPolicy | None = None,
    ) -> None:
        self._store = store
        self._home = home
        self._create_approval_required = create_approval_required
        self._modify_approval_required = modify_approval_required
        self._trust_policy = trust_policy or TrustPolicy(mode=TrustMode.STRICT)

        # Sub-services
        self._registry = SkillRegistry(store)
        self._proposals = ProposalService(self._registry, home)
        self._revisions = RevisionService(self._registry, home)
        self._telemetry = TelemetryService(self._registry)

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    @property
    def proposals(self) -> ProposalService:
        return self._proposals

    @property
    def revisions(self) -> RevisionService:
        return self._revisions

    @property
    def telemetry(self) -> TelemetryService:
        return self._telemetry

    @property
    def trust_policy(self) -> TrustPolicy:
        return self._trust_policy

    # ──────────────────────────────────────────────────────────────────
    # Discovery
    # ──────────────────────────────────────────────────────────────────

    def discover(self) -> list[DiscoveredSkill]:
        """Scan the filesystem for skills and sync with registry.

        Returns discovered skills. Does NOT load full SKILL.md content —
        only metadata for progressive disclosure.
        """
        discovered = discover_skills(self._home)

        # Sync discovered skills with registry
        for skill in discovered:
            existing = self._registry.get_skill_by_name(skill.name)
            if existing is None:
                # Register newly found skill
                owner = skill.meta.get("owner", SkillOwner.USER)
                if owner not in (SkillOwner.USER, SkillOwner.AGENT, SkillOwner.BUNDLED):
                    owner = SkillOwner.USER
                self._registry.register_skill(
                    name=skill.name,
                    description=skill.description,
                    path=skill.path,
                    owner=owner,
                    trust=SkillTrust.APPROVED if owner == SkillOwner.USER else SkillTrust.UNREVIEWED,
                )

        return discovered

    # ──────────────────────────────────────────────────────────────────
    # Content Access (progressive loading)
    # ──────────────────────────────────────────────────────────────────

    def get(self, skill_name: str) -> SkillRecord | None:
        """Get a skill record by name (metadata only, no content)."""
        return self._registry.get_skill_by_name(skill_name)

    def view(self, skill_name: str, *, run_id: str | None = None) -> str | None:
        """Load full SKILL.md content for a skill.

        Records a 'loaded' telemetry event.
        """
        content = read_skill_content(self._home, skill_name)
        if content is None:
            return None

        # Record telemetry
        record = self._registry.get_skill_by_name(skill_name)
        if record:
            self._telemetry.record(record.skill_id, "loaded", run_id=run_id)

        return content

    def view_file(
        self, skill_name: str, relative_path: str,
    ) -> str | None:
        """Load a specific file within a skill (e.g., references/debugging.md)."""
        return read_skill_file(self._home, skill_name, relative_path)

    # ──────────────────────────────────────────────────────────────────
    # Skill CRUD (direct)
    # ──────────────────────────────────────────────────────────────────

    def create(
        self,
        name: str,
        description: str,
        content: str,
        *,
        owner: str = SkillOwner.AGENT,
        source_run_id: str | None = None,
    ) -> SkillRecord:
        """Directly create a skill (bypasses proposal flow — use for user-created skills)."""
        from .discovery import ensure_skill_directory, SKILL_FILENAME
        from .proposals import _atomic_write
        import json

        skill_dir = ensure_skill_directory(self._home, name)
        _atomic_write(skill_dir / SKILL_FILENAME, content)

        meta = {"name": name, "description": description, "owner": owner}
        _atomic_write(skill_dir / "skill.json", json.dumps(meta, indent=2))

        skill = self._registry.register_skill(
            name=name, description=description,
            path=f"skills/{name}", owner=owner,
            trust=SkillTrust.APPROVED,
        )
        self._registry.update_skill(skill.skill_id, revision=1)
        self._registry.record_revision(
            skill_id=skill.skill_id, revision=1, operation="create",
            reason="direct creation",
            source_run_id=source_run_id,
        )

        # Snapshot
        history_dir = skill_dir / ".history"
        history_dir.mkdir(exist_ok=True)
        _atomic_write(history_dir / "000001.SKILL.md", content)

        return self._registry.get_skill(skill.skill_id)

    def patch(
        self,
        skill_name: str,
        content: str,
        reason: str,
        *,
        source_run_id: str | None = None,
    ) -> SkillRecord:
        """Directly patch a skill (bypasses proposal flow — use for user edits)."""
        from .proposals import _atomic_write, _append_manifest
        from .discovery import SKILL_FILENAME, HISTORY_DIR

        skill = self._registry.get_skill_by_name(skill_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_name}")

        skill_dir = self._home / "skills" / skill_name
        skill_md_path = skill_dir / SKILL_FILENAME
        new_revision = skill.revision + 1

        # Snapshot old content
        history_dir = skill_dir / HISTORY_DIR
        history_dir.mkdir(exist_ok=True)
        if skill_md_path.is_file():
            old = skill_md_path.read_text(encoding="utf-8")
            _atomic_write(history_dir / f"{skill.revision:06d}.SKILL.md", old)

        _atomic_write(skill_md_path, content)
        _atomic_write(history_dir / f"{new_revision:06d}.SKILL.md", content)
        _append_manifest(history_dir, new_revision, "patch", reason)

        self._registry.update_skill(skill.skill_id, revision=new_revision)
        self._registry.record_revision(
            skill_id=skill.skill_id, revision=new_revision,
            operation="patch", reason=reason,
            parent_revision=skill.revision,
            source_run_id=source_run_id,
        )
        return self._registry.get_skill(skill.skill_id)

    def archive(self, skill_name: str, *, reason: str = "archived") -> SkillRecord:
        """Archive a skill (soft-delete)."""
        skill = self._registry.get_skill_by_name(skill_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_name}")
        self._registry.update_skill(skill.skill_id, state=SkillState.ARCHIVED)
        return self._registry.get_skill(skill.skill_id)

    def restore(self, skill_name: str) -> SkillRecord:
        """Restore an archived skill."""
        skill = self._registry.get_skill_by_name(skill_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_name}")
        self._registry.update_skill(skill.skill_id, state=SkillState.ACTIVE)
        return self._registry.get_skill(skill.skill_id)

    def pin(self, skill_name: str) -> SkillRecord:
        """Pin a skill (prevents curator from archiving)."""
        skill = self._registry.get_skill_by_name(skill_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_name}")
        self._registry.update_skill(skill.skill_id, pinned=True)
        return self._registry.get_skill(skill.skill_id)

    def unpin(self, skill_name: str) -> SkillRecord:
        """Unpin a skill."""
        skill = self._registry.get_skill_by_name(skill_name)
        if skill is None:
            raise KeyError(f"skill not found: {skill_name}")
        self._registry.update_skill(skill.skill_id, pinned=False)
        return self._registry.get_skill(skill.skill_id)

    def rollback(self, skill_name: str, target_revision: int, *, reason: str = "") -> int:
        """Rollback a skill to a previous revision. Returns new revision number."""
        return self._revisions.rollback(skill_name, target_revision, reason=reason)

    # ──────────────────────────────────────────────────────────────────
    # Learning Integration — process operations from the reviewer
    # ──────────────────────────────────────────────────────────────────

    def process_skill_operation(
        self,
        op: SkillOperation,
        *,
        source_run_id: str | None = None,
        review_model: str | None = None,
    ) -> str | None:
        """Process a skill operation from the learning reviewer.

        Depending on configuration, this either:
        - Creates a pending proposal (approval required), or
        - Directly applies the operation (autonomous mode)

        Returns the proposal_id if a proposal was created, or
        the skill_id if directly applied.
        """
        if op.operation == "create":
            return self._process_create(op, source_run_id, review_model)
        elif op.operation == "patch":
            return self._process_patch(op, source_run_id, review_model)
        elif op.operation == "archive":
            return self._process_archive(op, source_run_id, review_model)
        else:
            LOGGER.warning("unsupported skill operation: %s", op.operation)
            return None

    def _process_create(
        self, op: SkillOperation, source_run_id: str | None, review_model: str | None,
    ) -> str | None:
        """Process a 'create' skill operation."""
        # Evaluate trust policy
        ctx = OperationContext(
            kind=OperationKind.SKILL_CREATE,
            skill_owner=SkillOwner.AGENT,
            confidence=op.confidence,
        )
        policy_result = self._trust_policy.evaluate(ctx)

        if policy_result.decision == TrustDecision.DENY:
            LOGGER.warning("skill create denied by trust policy: %s", policy_result.reason)
            return None

        # Determine whether approval is required:
        # - If trust policy says REQUIRE_APPROVAL AND service config requires it → proposal
        # - If service config says no approval required → direct create (trust ALLOW or REQUIRE)
        # - If trust policy says ALLOW and service says no approval → direct create
        needs_approval = (
            self._create_approval_required
            or policy_result.decision == TrustDecision.REQUIRE_APPROVAL
        ) and not (
            # Explicit service override: when approval is disabled, only DENY blocks
            not self._create_approval_required
        )

        if needs_approval and self._create_approval_required:
            # Create a pending proposal
            proposal = self._registry.create_proposal(
                skill_name=op.name,
                operation="create",
                description=op.description,
                reason=op.reason,
                content=op.content,
                confidence=op.confidence,
                source_run_id=source_run_id,
                review_model=review_model,
            )
            LOGGER.info(
                "skill proposal created: %s (create '%s', confidence=%.2f)",
                proposal.id, op.name, op.confidence,
            )
            return proposal.id
        else:
            # Directly create
            skill = self.create(
                op.name, op.description, op.content,
                owner=SkillOwner.AGENT, source_run_id=source_run_id,
            )
            return skill.skill_id

    def _process_patch(
        self, op: SkillOperation, source_run_id: str | None, review_model: str | None,
    ) -> str | None:
        """Process a 'patch' skill operation."""
        existing = self._registry.get_skill_by_name(op.name)
        if existing is None:
            LOGGER.warning("patch target not found: %s", op.name)
            return None

        # Evaluate trust policy
        ctx = OperationContext(
            kind=OperationKind.SKILL_PATCH,
            skill_owner=existing.owner,
            confidence=op.confidence,
        )
        policy_result = self._trust_policy.evaluate(ctx)

        if policy_result.decision == TrustDecision.DENY:
            LOGGER.warning(
                "skill patch denied by trust policy for '%s': %s",
                op.name, policy_result.reason,
            )
            return None

        # Load current content as 'before'
        current_content = read_skill_content(self._home, op.name)

        # Determine whether approval is needed:
        # - User-owned always requires approval (hard rule).
        # - Service config `modify_approval_required` is the explicit setting.
        # - Trust policy REQUIRE_APPROVAL only forces approval when service config also agrees.
        needs_approval = (
            existing.owner == SkillOwner.USER
            or self._modify_approval_required
        )

        if needs_approval:
            proposal = self._registry.create_proposal(
                skill_name=op.name,
                operation="patch",
                description=op.description,
                reason=op.reason,
                content=op.content,
                confidence=op.confidence,
                skill_id=existing.skill_id,
                before=current_content,
                base_revision=existing.revision,
                source_run_id=source_run_id,
                review_model=review_model,
            )
            LOGGER.info(
                "skill proposal created: %s (patch '%s', base_rev=%d, confidence=%.2f)",
                proposal.id, op.name, existing.revision, op.confidence,
            )
            return proposal.id
        else:
            self.patch(op.name, op.content, op.reason, source_run_id=source_run_id)
            return existing.skill_id

    def _process_archive(
        self, op: SkillOperation, source_run_id: str | None, review_model: str | None,
    ) -> str | None:
        """Process an 'archive' skill operation."""
        existing = self._registry.get_skill_by_name(op.name)
        if existing is None:
            LOGGER.warning("archive target not found: %s", op.name)
            return None

        if existing.owner == SkillOwner.USER:
            LOGGER.warning("cannot archive user-owned skill: %s", op.name)
            return None

        # Archive always requires approval
        current_content = read_skill_content(self._home, op.name)
        proposal = self._registry.create_proposal(
            skill_name=op.name,
            operation="archive",
            description=op.description,
            reason=op.reason,
            content=current_content or "",
            confidence=op.confidence,
            skill_id=existing.skill_id,
            before=current_content,
            base_revision=existing.revision,
            source_run_id=source_run_id,
            review_model=review_model,
        )
        return proposal.id

    # ──────────────────────────────────────────────────────────────────
    # Phase 2.5 — Runtime Interface
    # ──────────────────────────────────────────────────────────────────

    @property
    def discovery(self) -> SkillDiscovery:
        """Two-stage FTS-based skill discovery."""
        if not hasattr(self, "_discovery"):
            self._discovery_instance = SkillDiscovery(self._registry, self._store)
        return self._discovery_instance

    @property
    def prompt_integration(self) -> PromptIntegration:
        """Prompt integration for injecting skill context."""
        if not hasattr(self, "_prompt_integration"):
            self._prompt_integration_instance = PromptIntegration(
                self.discovery, self._registry,
            )
        return self._prompt_integration_instance

    def skill_view(
        self,
        skill_id: str,
        *,
        path: str | None = None,
        run_id: str | None = None,
        max_bytes: int | None = None,
    ) -> SkillViewResult:
        """Safe read-only access to skill content.

        Validates path traversal, symlink escape, and byte limits.
        Records 'loaded' telemetry.
        """
        return skill_view(
            self._registry, self._home, skill_id,
            path=path, run_id=run_id, max_bytes=max_bytes,
        )

    def skill_manage(
        self,
        action: str,
        **kwargs: Any,
    ) -> SkillManageResult:
        """Narrow agent mutation interface.

        Allowed actions: create_proposal, improve_proposal, archive_proposal.
        """
        return skill_manage(
            self._registry, self._home, action, **kwargs,
        )

    def search_skills(
        self,
        query: str,
        *,
        limit: int = 8,
        run_id: str | None = None,
    ) -> list[SkillCandidate]:
        """Search for relevant skills using FTS."""
        return self.discovery.search(query, limit=limit, run_id=run_id)

    def build_skill_prompt(
        self,
        task_summary: str,
        *,
        limit: int = 8,
        run_id: str | None = None,
        pinned_skills: list[str] | None = None,
    ) -> tuple[str, list[SkillCandidate]]:
        """Build the skill section for an agent prompt."""
        return self.prompt_integration.build_skill_prompt(
            task_summary, limit=limit, run_id=run_id,
            pinned_skills=pinned_skills,
        )

    def record_skill_selection(
        self, skill_id: str, *, run_id: str | None = None,
    ) -> None:
        """Record that the agent selected a skill for use."""
        self._telemetry.record(skill_id, "selected", run_id=run_id)

    def record_skill_execution(
        self, skill_id: str, *, run_id: str | None = None, success: bool = True,
    ) -> None:
        """Record execution outcome for a skill."""
        self._telemetry.record(skill_id, "executed", run_id=run_id)
        if success:
            self._telemetry.record(skill_id, "successful", run_id=run_id)
        else:
            self._telemetry.record(skill_id, "failed", run_id=run_id)

    def record_skill_correction(
        self, skill_id: str, *, run_id: str | None = None,
    ) -> None:
        """Record that the agent deviated from a skill (corrected it)."""
        self._telemetry.record(skill_id, "corrected", run_id=run_id)

    def save_run_context(self, context: RunSkillContext) -> None:
        """Persist the RunSkillContext for a run."""
        self._store.save_run_skill_context(context.run_id, context.to_json())

    def get_run_context(self, run_id: str) -> RunSkillContext | None:
        """Load the RunSkillContext for a run."""
        json_str = self._store.get_run_skill_context(run_id)
        if json_str is None:
            return None
        import json
        return RunSkillContext.from_dict(json.loads(json_str))

    def sync_fts(self) -> None:
        """Rebuild the FTS index for skill search."""
        self._store.sync_skills_fts()

    def upsert_fts(self, skill_name: str) -> None:
        """Update FTS entry for a single skill."""
        self._store.upsert_skill_fts(skill_name)
