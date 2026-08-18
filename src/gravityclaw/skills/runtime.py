"""Skill Runtime — Phase 2.5: connects learned skills to agent behavior.

Components:
  - skill_view: safe read-only content access with traversal protection
  - skill_manage: narrow agent mutation interface (propose/improve/archive)
  - SkillDiscovery: two-stage FTS-based runtime discovery
  - PromptIntegration: compact manifest + preamble for agent context
  - RunSkillContext: per-run record of which skills influenced behavior
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..store import Store, utc_now
from .discovery import SKILL_FILENAME, read_skill_content, read_skill_file
from .models import (
    ProposalStatus,
    SkillOwner,
    SkillRecord,
    SkillState,
    SkillTrust,
)
from .registry import SkillRegistry

LOGGER = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MAX_SKILL_BYTES = 64_000  # Maximum bytes returned from skill_view
MAX_FILE_BYTES = 32_000   # Maximum bytes for sub-files (references, scripts)

# Telemetry event types (Phase 2.5 extended)
RUNTIME_EVENTS = frozenset({
    "matched",       # appeared in FTS search results
    "presented",     # metadata injected into agent prompt
    "selected",      # agent chose this skill for potential use
    "loaded",        # full SKILL.md loaded into context
    "executed",      # skill procedure was followed
    "successful",    # skill led to successful outcome
    "failed",        # skill procedure failed
    "corrected",     # skill content was corrected/overridden
})

# ─────────────────────────────────────────────────────────────────────────────
# RunSkillContext — per-run record of skill influence
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class LoadedSkill:
    """Record of a skill loaded during a run."""
    skill_id: str
    name: str
    revision: int
    trust: str


@dataclass(slots=True)
class RunSkillContext:
    """Tracks which skills influenced a specific run.

    Persisted as JSON alongside the run for debugging and telemetry.
    """
    run_id: str
    candidates: list[str] = field(default_factory=list)   # skill_ids from FTS match
    presented: list[str] = field(default_factory=list)    # skill_ids injected in prompt
    selected: list[str] = field(default_factory=list)     # skill_ids the agent selected
    loaded: list[LoadedSkill] = field(default_factory=list)  # full load records

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "candidates": self.candidates,
            "presented": self.presented,
            "selected": self.selected,
            "loaded": [
                {
                    "skill_id": ls.skill_id,
                    "name": ls.name,
                    "revision": ls.revision,
                    "trust": ls.trust,
                }
                for ls in self.loaded
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunSkillContext":
        loaded = [
            LoadedSkill(
                skill_id=item["skill_id"],
                name=item["name"],
                revision=item["revision"],
                trust=item["trust"],
            )
            for item in data.get("loaded", [])
        ]
        return cls(
            run_id=data["run_id"],
            candidates=data.get("candidates", []),
            presented=data.get("presented", []),
            selected=data.get("selected", []),
            loaded=loaded,
        )


# ─────────────────────────────────────────────────────────────────────────────
# skill_view — safe read-only content access
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SkillViewResult:
    """Result from skill_view — content + metadata."""
    skill_id: str
    name: str
    revision: int
    trust: str
    owner: str
    content: str
    path: str | None = None  # None for SKILL.md, relative path for sub-files
    truncated: bool = False


class SkillViewError(ValueError):
    """Raised when skill_view encounters a safety violation."""


def skill_view(
    registry: SkillRegistry,
    home: Path,
    skill_id: str,
    *,
    path: str | None = None,
    run_id: str | None = None,
    max_bytes: int | None = None,
) -> SkillViewResult:
    """Load skill content safely with path traversal protection.

    Args:
        registry: The skill registry for metadata lookup.
        home: GRAVITYCLAW_HOME path.
        skill_id: Skill ID or name to view.
        path: Optional relative path within the skill directory.
              None means load SKILL.md.
        run_id: Optional run_id for telemetry recording.
        max_bytes: Override max byte limit for content.

    Returns:
        SkillViewResult with content and metadata.

    Raises:
        KeyError: If skill not found.
        SkillViewError: If path traversal or symlink escape detected.
    """
    # Resolve skill record — accept both ID and name
    record = _resolve_skill(registry, skill_id)

    # Determine the skill directory
    skill_dir = (home / record.path).resolve()

    # Validate the skill directory exists
    if not skill_dir.is_dir():
        raise KeyError(f"skill directory not found: {record.path}")

    if path is None:
        # Load SKILL.md
        target = skill_dir / SKILL_FILENAME
        byte_limit = max_bytes or MAX_SKILL_BYTES
    else:
        # Validate relative path — no traversal, no absolute, no symlink escape
        _validate_relative_path(path)
        target = (skill_dir / path).resolve()
        _validate_no_escape(target, skill_dir)
        byte_limit = max_bytes or MAX_FILE_BYTES

    if not target.is_file():
        raise KeyError(
            f"file not found: {path or 'SKILL.md'} in skill '{record.name}'"
        )

    # Check for symlink pointing outside the skill directory
    if target.is_symlink():
        real_target = target.resolve()
        _validate_no_escape(real_target, skill_dir)

    # Read content with byte limit
    content = _read_bounded(target, byte_limit)
    truncated = len(target.read_bytes()) > byte_limit

    # Record telemetry
    registry.record_usage(record.skill_id, "loaded", run_id=run_id)

    return SkillViewResult(
        skill_id=record.skill_id,
        name=record.name,
        revision=record.revision,
        trust=record.trust,
        owner=record.owner,
        content=content,
        path=path,
        truncated=truncated,
    )


def _resolve_skill(registry: SkillRegistry, skill_id: str) -> SkillRecord:
    """Resolve a skill by ID or name."""
    # Try by name first (more common in agent usage)
    record = registry.get_skill_by_name(skill_id)
    if record:
        return record
    # Try by ID
    try:
        return registry.get_skill(skill_id)
    except KeyError:
        raise KeyError(f"skill not found: {skill_id}")


def _validate_relative_path(path: str) -> None:
    """Validate a relative path has no traversal or absolute components."""
    if not path:
        raise SkillViewError("empty path")

    # No absolute paths
    if os.path.isabs(path):
        raise SkillViewError(f"absolute paths not allowed: {path}")

    # No parent directory traversal
    normalized = os.path.normpath(path)
    if normalized.startswith("..") or "/../" in f"/{normalized}/" or normalized == "..":
        raise SkillViewError(f"path traversal not allowed: {path}")

    # Also check each component
    parts = Path(path).parts
    if ".." in parts:
        raise SkillViewError(f"path traversal not allowed: {path}")

    # No hidden directories (except .history which we don't expose)
    for part in parts:
        if part.startswith("."):
            raise SkillViewError(f"hidden paths not allowed: {path}")


def _validate_no_escape(target: Path, boundary: Path) -> None:
    """Validate the resolved target is within the boundary directory."""
    try:
        target.relative_to(boundary)
    except ValueError:
        raise SkillViewError(
            f"path escapes skill directory: resolved to {target}"
        )


def _read_bounded(path: Path, max_bytes: int) -> str:
    """Read a file with a byte limit, returning valid UTF-8 text."""
    try:
        raw = path.read_bytes()
        if len(raw) > max_bytes:
            # Truncate at byte boundary, then decode safely
            raw = raw[:max_bytes]
        return raw.decode("utf-8", errors="replace")
    except OSError as exc:
        raise SkillViewError(f"cannot read file: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# skill_manage — narrow agent mutation interface
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class SkillManageResult:
    """Result from skill_manage operations."""
    action: str
    proposal_id: str | None = None
    skill_id: str | None = None
    message: str = ""


class SkillManageError(ValueError):
    """Raised when skill_manage encounters an error."""


def skill_manage(
    registry: SkillRegistry,
    home: Path,
    action: str,
    *,
    skill_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
    content: str | None = None,
    reason: str | None = None,
    base_revision: int | None = None,
    confidence: float = 0.8,
    source_run_id: str | None = None,
    review_model: str | None = None,
) -> SkillManageResult:
    """Narrow agent interface for skill mutations.

    Allowed actions:
      - create_proposal: Propose a new skill
      - improve_proposal: Propose an improvement to an existing skill
      - archive_proposal: Propose archiving a skill

    NOT exposed (control-plane only):
      - approve, reject, rollback, delete

    Args:
        registry: The skill registry.
        home: GRAVITYCLAW_HOME.
        action: One of create_proposal, improve_proposal, archive_proposal.
        skill_id: Skill ID or name (for improve/archive).
        name: Skill name (for create).
        description: Skill description.
        content: New SKILL.md content.
        reason: Why this change is proposed.
        base_revision: Expected current revision (for improve).
        confidence: Confidence score 0.0–1.0.
        source_run_id: Originating run ID.
        review_model: Model that generated this proposal.

    Returns:
        SkillManageResult with the proposal_id.

    Raises:
        SkillManageError: On invalid action or missing required fields.
    """
    if action == "create_proposal":
        return _create_proposal(
            registry, name=name, description=description,
            content=content, reason=reason, confidence=confidence,
            source_run_id=source_run_id, review_model=review_model,
        )
    elif action == "improve_proposal":
        return _improve_proposal(
            registry, home, skill_id=skill_id,
            description=description, content=content,
            reason=reason, base_revision=base_revision,
            confidence=confidence, source_run_id=source_run_id,
            review_model=review_model,
        )
    elif action == "archive_proposal":
        return _archive_proposal(
            registry, home, skill_id=skill_id,
            reason=reason, confidence=confidence,
            source_run_id=source_run_id, review_model=review_model,
        )
    else:
        raise SkillManageError(
            f"invalid action: {action}. "
            "Allowed: create_proposal, improve_proposal, archive_proposal"
        )


def _create_proposal(
    registry: SkillRegistry,
    *,
    name: str | None,
    description: str | None,
    content: str | None,
    reason: str | None,
    confidence: float,
    source_run_id: str | None,
    review_model: str | None,
) -> SkillManageResult:
    """Create a proposal for a new skill."""
    if not name:
        raise SkillManageError("name is required for create_proposal")
    if not content:
        raise SkillManageError("content is required for create_proposal")
    if not description:
        raise SkillManageError("description is required for create_proposal")

    # Validate name format (kebab-case)
    if not re.match(r"^[a-z][a-z0-9-]*$", name):
        raise SkillManageError(
            f"invalid skill name '{name}': must be kebab-case (lowercase, hyphens)"
        )

    # Check for duplicates
    existing = registry.get_skill_by_name(name)
    if existing:
        raise SkillManageError(f"skill '{name}' already exists — use improve_proposal")

    proposal = registry.create_proposal(
        skill_name=name,
        operation="create",
        description=description,
        reason=reason or "agent-proposed skill creation",
        content=content,
        confidence=confidence,
        source_run_id=source_run_id,
        review_model=review_model,
    )

    return SkillManageResult(
        action="create_proposal",
        proposal_id=proposal.id,
        message=f"Proposal created for new skill '{name}'",
    )


def _improve_proposal(
    registry: SkillRegistry,
    home: Path,
    *,
    skill_id: str | None,
    description: str | None,
    content: str | None,
    reason: str | None,
    base_revision: int | None,
    confidence: float,
    source_run_id: str | None,
    review_model: str | None,
) -> SkillManageResult:
    """Create a proposal to improve an existing skill."""
    if not skill_id:
        raise SkillManageError("skill_id (or name) is required for improve_proposal")
    if not content:
        raise SkillManageError("content is required for improve_proposal")
    if not reason:
        raise SkillManageError("reason is required for improve_proposal")

    # Resolve skill
    record = _resolve_skill(registry, skill_id)

    # Validate base_revision if provided
    if base_revision is not None and base_revision != record.revision:
        raise SkillManageError(
            f"stale base_revision: provided {base_revision}, "
            f"skill is at revision {record.revision}"
        )

    # User-owned skills: allowed to propose, but will require manual approval
    # (enforced at approval time, not proposal time)

    # Load current content
    current_content = read_skill_content(home, record.name)

    proposal = registry.create_proposal(
        skill_name=record.name,
        operation="patch",
        description=description or record.description,
        reason=reason,
        content=content,
        confidence=confidence,
        skill_id=record.skill_id,
        before=current_content,
        base_revision=record.revision,
        source_run_id=source_run_id,
        review_model=review_model,
    )

    return SkillManageResult(
        action="improve_proposal",
        proposal_id=proposal.id,
        skill_id=record.skill_id,
        message=f"Improvement proposal created for skill '{record.name}' (base_revision={record.revision})",
    )


def _archive_proposal(
    registry: SkillRegistry,
    home: Path,
    *,
    skill_id: str | None,
    reason: str | None,
    confidence: float,
    source_run_id: str | None,
    review_model: str | None,
) -> SkillManageResult:
    """Create a proposal to archive a skill."""
    if not skill_id:
        raise SkillManageError("skill_id (or name) is required for archive_proposal")

    record = _resolve_skill(registry, skill_id)

    # User-owned skills cannot be archived by the agent
    if record.owner == SkillOwner.USER:
        raise SkillManageError(
            f"cannot propose archiving user-owned skill '{record.name}'"
        )

    # Bundled skills cannot be archived
    if record.owner == SkillOwner.BUNDLED:
        raise SkillManageError(
            f"cannot propose archiving bundled skill '{record.name}'"
        )

    current_content = read_skill_content(home, record.name)

    proposal = registry.create_proposal(
        skill_name=record.name,
        operation="archive",
        description=f"Archive skill: {record.name}",
        reason=reason or "agent-proposed archive",
        content=current_content or "",
        confidence=confidence,
        skill_id=record.skill_id,
        before=current_content,
        base_revision=record.revision,
        source_run_id=source_run_id,
        review_model=review_model,
    )

    return SkillManageResult(
        action="archive_proposal",
        proposal_id=proposal.id,
        skill_id=record.skill_id,
        message=f"Archive proposal created for skill '{record.name}'",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Two-Stage Runtime Discovery
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """A candidate skill from FTS search — metadata only."""
    skill_id: str
    name: str
    description: str
    trust: str
    owner: str
    revision: int
    relevance_score: float = 0.0


class SkillDiscovery:
    """Two-stage skill discovery for runtime.

    Stage 1: FTS/metadata search over the registry → candidate list
    Stage 2: Agent selects from candidates → skill_view loads full content

    This ensures:
    - Only metadata enters the prompt (cheap)
    - Full content is loaded on demand (progressive disclosure)
    - FTS + tags + metadata are sufficient initially (no embeddings)
    """

    def __init__(self, registry: SkillRegistry, store: Store) -> None:
        self._registry = registry
        self._store = store

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        run_id: str | None = None,
        include_archived: bool = False,
    ) -> list[SkillCandidate]:
        """Search the skill registry using FTS on name + description.

        Returns candidate metadata sorted by relevance.
        Records 'matched' telemetry for all results.
        """
        if not query or not query.strip():
            return []

        # Query the FTS table
        candidates = self._fts_search(query, limit=limit, include_archived=include_archived)

        # Record telemetry for matched skills
        for candidate in candidates:
            self._registry.record_usage(
                candidate.skill_id, "matched", run_id=run_id,
            )

        return candidates

    def _fts_search(
        self,
        query: str,
        *,
        limit: int = 8,
        include_archived: bool = False,
    ) -> list[SkillCandidate]:
        """Perform FTS search against the skills_fts table.

        Falls back to LIKE-based search if FTS table doesn't exist.
        """
        import re as _re

        # Compile tokens for FTS MATCH (same pattern as memories_fts)
        terms = _re.findall(r"[^\W_]+", query, flags=_re.UNICODE)
        if not terms:
            return []

        # Try FTS first
        try:
            return self._fts_query(terms, limit, include_archived)
        except Exception:
            # FTS table may not exist yet — fall back to LIKE
            return self._like_search(terms, limit, include_archived)

    def _fts_query(
        self, terms: list[str], limit: int, include_archived: bool,
    ) -> list[SkillCandidate]:
        """Execute FTS5 MATCH query against skills_fts."""
        fts_expr = " OR ".join(
            f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:20]
        )
        state_clause = "" if include_archived else "AND s.state = 'active'"

        with self._store._connect() as conn:
            rows = conn.execute(
                f"""SELECT s.*, bm25(skills_fts) AS rank
                    FROM skills_fts f
                    JOIN learned_skills s ON s.name = f.name
                    WHERE skills_fts MATCH ?
                    {state_clause}
                    ORDER BY rank
                    LIMIT ?""",
                (fts_expr, limit),
            ).fetchall()

        return [
            SkillCandidate(
                skill_id=row["id"],
                name=row["name"],
                description=row["description"],
                trust=row["trust"],
                owner=row["owner"],
                revision=row["revision"],
                relevance_score=abs(row["rank"]),  # bm25 returns negative scores
            )
            for row in rows
        ]

    def _like_search(
        self, terms: list[str], limit: int, include_archived: bool,
    ) -> list[SkillCandidate]:
        """Fallback LIKE-based search when FTS is unavailable."""
        state_clause = "AND state = 'active'" if not include_archived else ""
        like_clauses = []
        params: list[Any] = []
        for term in terms[:10]:
            like_clauses.append("(name LIKE ? OR description LIKE ?)")
            params.extend([f"%{term}%", f"%{term}%"])

        where = " OR ".join(like_clauses)
        params.append(limit)

        with self._store._connect() as conn:
            rows = conn.execute(
                f"""SELECT * FROM learned_skills
                    WHERE ({where}) {state_clause}
                    ORDER BY name
                    LIMIT ?""",
                params,
            ).fetchall()

        return [
            SkillCandidate(
                skill_id=row["id"],
                name=row["name"],
                description=row["description"],
                trust=row["trust"],
                owner=row["owner"],
                revision=row["revision"],
                relevance_score=1.0,  # No ranking in LIKE mode
            )
            for row in rows
        ]


# ─────────────────────────────────────────────────────────────────────────────
# Prompt Integration
# ─────────────────────────────────────────────────────────────────────────────

SKILL_PREAMBLE = """\
Skills are reusable procedures learned or provided previously.

Use them when relevant, but treat them as procedural guidance rather \
than unquestionable truth.

If a skill conflicts with current evidence, environment state, tool \
output, or explicit user instructions, prefer current evidence.

If you discover that an approved skill is materially incomplete or \
incorrect, complete the task using the correct approach and then \
propose an improvement via skill_manage(action="improve_proposal").
"""


class PromptIntegration:
    """Generates the skill section for agent prompts.

    Implements the two-stage flow:
    1. Search → compact manifest of candidate metadata
    2. Agent calls skill_view to load full content on demand

    The prompt injection is lightweight: preamble + metadata list.
    Full content is never auto-injected.
    """

    def __init__(
        self,
        discovery: SkillDiscovery,
        registry: SkillRegistry,
    ) -> None:
        self._discovery = discovery
        self._registry = registry

    def build_skill_prompt(
        self,
        task_summary: str,
        *,
        limit: int = 8,
        run_id: str | None = None,
        pinned_skills: list[str] | None = None,
    ) -> tuple[str, list[SkillCandidate]]:
        """Build the skill section for the agent prompt.

        Returns (prompt_text, candidates) — candidates are tracked for
        RunSkillContext.

        Args:
            task_summary: A short summary of the current task/query.
            limit: Maximum number of candidate skills to present.
            run_id: Current run ID for telemetry.
            pinned_skills: Always-include skill names.

        Returns:
            Tuple of (formatted prompt section, candidate list).
        """
        # Stage 1: search for relevant skills
        candidates = self._discovery.search(
            task_summary, limit=limit, run_id=run_id,
        )

        # Add pinned skills that weren't found in search
        if pinned_skills:
            found_names = {c.name for c in candidates}
            for pin_name in pinned_skills:
                if pin_name not in found_names:
                    record = self._registry.get_skill_by_name(pin_name)
                    if record and record.state == SkillState.ACTIVE:
                        candidates.append(SkillCandidate(
                            skill_id=record.skill_id,
                            name=record.name,
                            description=record.description,
                            trust=record.trust,
                            owner=record.owner,
                            revision=record.revision,
                            relevance_score=0.0,
                        ))

        if not candidates:
            return "", []

        # Record 'presented' telemetry
        for candidate in candidates:
            self._registry.record_usage(
                candidate.skill_id, "presented", run_id=run_id,
            )

        # Build compact manifest
        lines = [SKILL_PREAMBLE, "", "AVAILABLE SKILLS", ""]
        for candidate in candidates:
            trust_badge = " [approved]" if candidate.trust == SkillTrust.APPROVED else ""
            lines.append(f"  {candidate.name}{trust_badge}")
            lines.append(f"    {candidate.description}")
            lines.append("")

        lines.append(
            "To use a skill, call skill_view(skill_id=\"<name>\") to load its full procedure."
        )

        return "\n".join(lines), candidates

    def build_empty_prompt(self) -> str:
        """Return an empty skill prompt (no skills available)."""
        return ""
