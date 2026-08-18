"""SkillRegistry — SQLite-backed metadata layer for skills.

The filesystem remains the authoritative content layer. The registry indexes
skill metadata for discovery, telemetry, and lifecycle management.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from ..store import Store, utc_now
from .models import (
    ProposalStatus,
    SkillOwner,
    SkillProposal,
    SkillRecord,
    SkillRevisionRecord,
    SkillState,
    SkillTrust,
    SkillUsageEvent,
)


class StaleRevisionError(ValueError):
    """Raised when a proposal's base_revision doesn't match the skill's current revision."""


class SkillRegistry:
    """SQLite-backed skill metadata registry.

    Methods operate on the Store's shared connection pool. Schema DDL is
    applied during Store.initialize() (see _SKILLS_SCHEMA in store.py).
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    # ──────────────────────────────────────────────────────────────────
    # Skill CRUD
    # ──────────────────────────────────────────────────────────────────

    def register_skill(
        self,
        name: str,
        description: str,
        path: str,
        *,
        owner: str = SkillOwner.AGENT,
        trust: str = SkillTrust.UNREVIEWED,
        skill_id: str | None = None,
    ) -> SkillRecord:
        """Register a new skill in the metadata layer."""
        now = utc_now()
        record_id = skill_id or str(uuid.uuid4())
        with self._store._connect() as conn:
            conn.execute(
                """INSERT INTO learned_skills(
                    id, name, description, path, owner, state, trust,
                    revision, pinned, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)""",
                (record_id, name, description, path, owner,
                 SkillState.ACTIVE, trust, now, now),
            )
        return SkillRecord(
            skill_id=record_id, name=name, description=description,
            path=path, owner=owner, state=SkillState.ACTIVE,
            trust=trust, revision=0, pinned=False,
            created_at=now, updated_at=now,
        )

    def get_skill(self, skill_id: str) -> SkillRecord:
        """Get a skill by ID."""
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learned_skills WHERE id=?", (skill_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"skill not found: {skill_id}")
        return _skill_record(row)

    def get_skill_by_name(self, name: str) -> SkillRecord | None:
        """Get a skill by its unique name."""
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM learned_skills WHERE name=?", (name,)
            ).fetchone()
        return _skill_record(row) if row else None

    def list_skills(
        self,
        *,
        owner: str | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[SkillRecord]:
        """List skills with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if owner:
            clauses.append("owner=?")
            params.append(owner)
        if state:
            clauses.append("state=?")
            params.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._store._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM learned_skills{where} ORDER BY name LIMIT ?",
                params,
            ).fetchall()
        return [_skill_record(row) for row in rows]

    def update_skill(
        self,
        skill_id: str,
        *,
        description: str | None = None,
        state: str | None = None,
        trust: str | None = None,
        revision: int | None = None,
        pinned: bool | None = None,
    ) -> SkillRecord:
        """Update skill metadata fields."""
        sets: list[str] = []
        params: list[Any] = []
        if description is not None:
            sets.append("description=?")
            params.append(description)
        if state is not None:
            sets.append("state=?")
            params.append(state)
        if trust is not None:
            sets.append("trust=?")
            params.append(trust)
        if revision is not None:
            sets.append("revision=?")
            params.append(revision)
        if pinned is not None:
            sets.append("pinned=?")
            params.append(1 if pinned else 0)
        if not sets:
            return self.get_skill(skill_id)
        sets.append("updated_at=?")
        params.append(utc_now())
        params.append(skill_id)
        with self._store._connect() as conn:
            cursor = conn.execute(
                f"UPDATE learned_skills SET {', '.join(sets)} WHERE id=?",
                params,
            )
            if cursor.rowcount == 0:
                raise KeyError(f"skill not found: {skill_id}")
        return self.get_skill(skill_id)

    def delete_skill(self, skill_id: str) -> None:
        """Hard-delete a skill record (prefer archive for soft-delete)."""
        with self._store._connect() as conn:
            cursor = conn.execute("DELETE FROM learned_skills WHERE id=?", (skill_id,))
            if cursor.rowcount == 0:
                raise KeyError(f"skill not found: {skill_id}")

    # ──────────────────────────────────────────────────────────────────
    # Proposals
    # ──────────────────────────────────────────────────────────────────

    def create_proposal(
        self,
        skill_name: str,
        operation: str,
        description: str,
        reason: str,
        content: str,
        confidence: float,
        *,
        skill_id: str | None = None,
        before: str | None = None,
        base_revision: int | None = None,
        source_run_id: str | None = None,
        review_model: str | None = None,
        proposal_id: str | None = None,
    ) -> SkillProposal:
        """Persist a new skill proposal as pending."""
        now = utc_now()
        record_id = proposal_id or str(uuid.uuid4())
        with self._store._connect() as conn:
            conn.execute(
                """INSERT INTO skill_proposals(
                    id, skill_id, skill_name, operation, description, reason,
                    confidence, content, before, base_revision,
                    source_run_id, review_model, status, status_reason,
                    created_at, resolved_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL)""",
                (record_id, skill_id, skill_name, operation, description,
                 reason, confidence, content, before, base_revision,
                 source_run_id, review_model, ProposalStatus.PENDING, now),
            )
        return SkillProposal(
            id=record_id, skill_id=skill_id, skill_name=skill_name,
            operation=operation, description=description, reason=reason,
            confidence=confidence, content=content, before=before,
            base_revision=base_revision, source_run_id=source_run_id,
            review_model=review_model, status=ProposalStatus.PENDING,
            status_reason=None, created_at=now, resolved_at=None,
        )

    def get_proposal(self, proposal_id: str) -> SkillProposal:
        """Get a proposal by ID."""
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_proposals WHERE id=?", (proposal_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"skill proposal not found: {proposal_id}")
        return _proposal_record(row)

    def list_proposals(
        self,
        *,
        skill_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[SkillProposal]:
        """List proposals with optional filters."""
        clauses: list[str] = []
        params: list[Any] = []
        if skill_id:
            clauses.append("skill_id=?")
            params.append(skill_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        with self._store._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM skill_proposals{where} ORDER BY created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [_proposal_record(row) for row in rows]

    def resolve_proposal(
        self,
        proposal_id: str,
        status: str,
        *,
        reason: str | None = None,
    ) -> SkillProposal:
        """Set proposal status to approved/rejected/conflict/expired."""
        now = utc_now()
        with self._store._connect() as conn:
            cursor = conn.execute(
                """UPDATE skill_proposals SET status=?, status_reason=?, resolved_at=?
                   WHERE id=? AND status='pending'""",
                (status, reason, now, proposal_id),
            )
            if cursor.rowcount == 0:
                # Check if it exists but is already resolved
                existing = conn.execute(
                    "SELECT status FROM skill_proposals WHERE id=?", (proposal_id,)
                ).fetchone()
                if existing is None:
                    raise KeyError(f"skill proposal not found: {proposal_id}")
                raise ValueError(
                    f"proposal already resolved with status: {existing['status']}"
                )
        return self.get_proposal(proposal_id)

    # ──────────────────────────────────────────────────────────────────
    # Revisions
    # ──────────────────────────────────────────────────────────────────

    def record_revision(
        self,
        skill_id: str,
        revision: int,
        operation: str,
        reason: str,
        *,
        parent_revision: int | None = None,
        source_run_id: str | None = None,
        proposal_id: str | None = None,
        model: str | None = None,
        revision_id: str | None = None,
    ) -> SkillRevisionRecord:
        """Record a new revision entry for a skill."""
        now = utc_now()
        record_id = revision_id or str(uuid.uuid4())
        with self._store._connect() as conn:
            conn.execute(
                """INSERT INTO skill_revisions(
                    id, skill_id, revision, parent_revision, operation,
                    source_run_id, proposal_id, model, reason, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (record_id, skill_id, revision, parent_revision, operation,
                 source_run_id, proposal_id, model, reason, now),
            )
        return SkillRevisionRecord(
            id=record_id, skill_id=skill_id, revision=revision,
            parent_revision=parent_revision, operation=operation,
            source_run_id=source_run_id, proposal_id=proposal_id,
            model=model, reason=reason, created_at=now,
        )

    def list_revisions(
        self,
        skill_id: str,
        *,
        limit: int = 50,
    ) -> list[SkillRevisionRecord]:
        """List revision history for a skill."""
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM skill_revisions WHERE skill_id=? ORDER BY revision DESC LIMIT ?",
                (skill_id, limit),
            ).fetchall()
        return [_revision_record(row) for row in rows]

    def get_revision(self, revision_id: str) -> SkillRevisionRecord:
        """Get a single revision record."""
        with self._store._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_revisions WHERE id=?", (revision_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"skill revision not found: {revision_id}")
        return _revision_record(row)

    # ──────────────────────────────────────────────────────────────────
    # Usage Telemetry
    # ──────────────────────────────────────────────────────────────────

    def record_usage(
        self,
        skill_id: str,
        event: str,
        *,
        run_id: str | None = None,
        event_id: str | None = None,
    ) -> SkillUsageEvent:
        """Record a usage telemetry event."""
        now = utc_now()
        record_id = event_id or str(uuid.uuid4())
        with self._store._connect() as conn:
            conn.execute(
                """INSERT INTO skill_usage(id, skill_id, run_id, event, created_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (record_id, skill_id, run_id, event, now),
            )
        return SkillUsageEvent(
            id=record_id, skill_id=skill_id, run_id=run_id,
            event=event, created_at=now,
        )

    def usage_stats(self, skill_id: str) -> dict[str, int]:
        """Get aggregated usage counts by event type for a skill."""
        with self._store._connect() as conn:
            rows = conn.execute(
                "SELECT event, COUNT(*) as count FROM skill_usage WHERE skill_id=? GROUP BY event",
                (skill_id,),
            ).fetchall()
        return {row["event"]: row["count"] for row in rows}


# ─────────────────────────────────────────────────────────────────────────────
# Row mapping helpers
# ─────────────────────────────────────────────────────────────────────────────

def _skill_record(row) -> SkillRecord:
    return SkillRecord(
        skill_id=row["id"],
        name=row["name"],
        description=row["description"],
        path=row["path"],
        owner=row["owner"],
        state=row["state"],
        trust=row["trust"],
        revision=row["revision"],
        pinned=bool(row["pinned"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _proposal_record(row) -> SkillProposal:
    return SkillProposal(
        id=row["id"],
        skill_id=row["skill_id"],
        skill_name=row["skill_name"],
        operation=row["operation"],
        description=row["description"],
        reason=row["reason"],
        confidence=row["confidence"],
        content=row["content"],
        before=row["before"],
        base_revision=row["base_revision"],
        source_run_id=row["source_run_id"],
        review_model=row["review_model"],
        status=row["status"],
        status_reason=row["status_reason"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


def _revision_record(row) -> SkillRevisionRecord:
    return SkillRevisionRecord(
        id=row["id"],
        skill_id=row["skill_id"],
        revision=row["revision"],
        parent_revision=row["parent_revision"],
        operation=row["operation"],
        source_run_id=row["source_run_id"],
        proposal_id=row["proposal_id"],
        model=row["model"],
        reason=row["reason"],
        created_at=row["created_at"],
    )
