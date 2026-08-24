"""Autonomous Memory Curator for GravityClaw.

Evaluates conversation events, run outputs, and daily episodic journals to decide
what deserves promotion into durable long-term memory (MEMORY.md / USER.md + SQLite FTS5).

Handles:
- Negative filters (rejects ephemeral status, speculation, secrets, prompt injections)
- 7-factor scoring model (durability, future utility, specificity, confidence, novelty, trust, sensitivity)
- Contradiction and supersession detection with historical revision tracking
- Three governance modes: manual, assisted (default), automatic
- Periodic episodic journal consolidation ("dreaming")
- Explicit memory intent (/remember, "Remember that...")
"""

from __future__ import annotations

import enum
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

from .identity import IdentityStore
from .memory import MemoryService
from .store import PersistedEvent, RunRecord, Store, utc_now

LOGGER = logging.getLogger("gravityclaw.curator")


class MemoryCandidateCategory(str, enum.Enum):
    USER_PREFERENCE = "user_preference"
    PROJECT_DECISION = "project_decision"
    STABLE_FACT = "stable_fact"
    PERSISTENT_CONSTRAINT = "persistent_constraint"
    ONGOING_GOAL = "ongoing_goal"
    CORRECTION = "correction"
    RELATIONSHIP = "relationship"
    CONFIGURATION_DECISION = "configuration_decision"


class SourceTrustTier(float, enum.Enum):
    USER_EXPLICIT = 1.0
    USER_CORRECTION = 0.95
    USER_CONFIRMED_DECISION = 0.90
    DETERMINISTIC_RESULT = 0.85
    REPEATED_RUN_CONCLUSION = 0.80
    AGENT_INFERENCE = 0.50
    TOOL_OUTPUT = 0.30
    WEB_CONTENT = 0.10


class MemoryCurationMode(str, enum.Enum):
    MANUAL = "manual"
    ASSISTED = "assisted"
    AUTOMATIC = "automatic"


# ─── Negative Filter Patterns (What MUST NOT go into long-term memory) ──────────

EPHEMERAL_PATTERNS = [
    re.compile(r"\b(?:tests? (?:are|is) (?:currently )?running|running tests?)\b", re.I),
    re.compile(r"\b(?:cpu|memory|disk|gpu) (?:usage|utilization|load) is \d+", re.I),
    re.compile(r"\btask #?\d+ (?:failed once|retried|is pending|in progress)\b", re.I),
    re.compile(r"\bstep \d+ of \d+\b", re.I),
    re.compile(r"\b(?:current (?:time|timestamp|clock|date)|right now it is)\b", re.I),
    re.compile(r"\b(?:exited with code \d+|process id \d+|pid \d+)\b", re.I),
]

SPECULATION_PATTERNS = [
    re.compile(r"\b(?:maybe we (?:should|could)|perhaps we (?:can|could|should))\b", re.I),
    re.compile(r"\b(?:could potentially|might consider|i think we might|not sure if)\b", re.I),
    re.compile(r"\b(?:it is possible that|hypothetically|as an alternative concept)\b", re.I),
]

PROMPT_INJECTION_PATTERNS = [
    re.compile(r"\b(?:ignore previous instructions|ignore all previous|system prompt:)\b", re.I),
    re.compile(r"\b(?:you are now DAN|jailbreak|bypass security|DAN mode)\b", re.I),
    re.compile(r"\b(?:remember permanently that (?:you are|your rules are))\b", re.I),
]

SECRET_PATTERNS = [
    re.compile(r"(?:Bearer\s+[A-Za-z0-9_\-\.]{20,})"),
    re.compile(r"(?:ghp_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{30,})"),
    re.compile(r"(?:sk-[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_\-]{20,})"),
    re.compile(r"(?:AKIA[0-9A-Z]{16})"),
    re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC|DSA|PRIVATE) KEY-----"),
    re.compile(r"(?:api[_-]?key|secret|password|auth[_-]?token)\s*[:=]\s*['\"][A-Za-z0-9_\-\.]{8,}['\"]", re.I),
]


@dataclass
class MemoryCandidate:
    id: str
    content: str
    category: MemoryCandidateCategory
    source_tier: SourceTrustTier
    confidence: float
    source_run_id: str | None = None
    source_conversation_id: str | None = None
    reason: str = ""
    durability: float = 0.85
    future_utility: float = 0.85
    specificity: float = 0.85
    novelty: float = 1.0
    sensitivity_risk: float = 0.0
    contradiction_risk: float = 0.0
    total_score: float = 0.0
    verdict: str = "suggest"  # "promote", "suggest", "supersede", "merge", "discard"
    target_memory_id: str | None = None
    previous_content: str | None = None
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "category": self.category.value if isinstance(self.category, MemoryCandidateCategory) else str(self.category),
            "source_tier": self.source_tier.name if isinstance(self.source_tier, SourceTrustTier) else str(self.source_tier),
            "confidence": self.confidence,
            "source_run_id": self.source_run_id,
            "source_conversation_id": self.source_conversation_id,
            "reason": self.reason,
            "durability": round(self.durability, 2),
            "future_utility": round(self.future_utility, 2),
            "specificity": round(self.specificity, 2),
            "novelty": round(self.novelty, 2),
            "sensitivity_risk": round(self.sensitivity_risk, 2),
            "contradiction_risk": round(self.contradiction_risk, 2),
            "total_score": round(self.total_score, 2),
            "verdict": self.verdict,
            "target_memory_id": self.target_memory_id,
            "previous_content": self.previous_content,
            "created_at": self.created_at,
        }


@dataclass
class MemoryCurationResult:
    promoted: list[dict[str, Any]] = field(default_factory=list)
    suggested: list[dict[str, Any]] = field(default_factory=list)
    superseded: list[dict[str, Any]] = field(default_factory=list)
    discarded: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "promoted": self.promoted,
            "suggested": self.suggested,
            "superseded": self.superseded,
            "discarded": self.discarded,
        }


@dataclass
class ConsolidationReport:
    started_at: str
    completed_at: str
    journals_scanned: int
    entries_analyzed: int
    candidates_discovered: int
    memories_promoted: int
    memories_superseded: int
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MemoryCurator:
    """The Memory Curator manages the promotion, supersession, and consolidation of durable memory."""

    def __init__(
        self,
        store: Store,
        identity: IdentityStore,
        memory_service: MemoryService,
        *,
        mode: MemoryCurationMode | str = MemoryCurationMode.ASSISTED,
    ) -> None:
        self.store = store
        self.identity = identity
        self.memory_service = memory_service
        self._mode = MemoryCurationMode(mode) if isinstance(mode, str) else mode

    @property
    def mode(self) -> MemoryCurationMode:
        setting = self.store.get_memory_curator_setting("curator_mode", self._mode.value)
        try:
            return MemoryCurationMode(setting)
        except ValueError:
            return MemoryCurationMode.ASSISTED

    @mode.setter
    def mode(self, new_mode: MemoryCurationMode | str) -> None:
        val = new_mode.value if isinstance(new_mode, MemoryCurationMode) else str(new_mode)
        self._mode = MemoryCurationMode(val)
        self.store.set_memory_curator_setting("curator_mode", val)

    def check_negative_filters(self, text: str, source_tier: SourceTrustTier) -> tuple[bool, str]:
        """Verify that candidate does not violate any negative filters."""
        # 1. Secret / token detection
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                return False, "contains potential secret, token, or private key"

        # 2. Prompt injection detection
        for pattern in PROMPT_INJECTION_PATTERNS:
            if pattern.search(text):
                return False, "contains prompt injection or adversarial phrasing"

        # 3. Unverified low-trust sources
        if source_tier in (SourceTrustTier.TOOL_OUTPUT, SourceTrustTier.WEB_CONTENT):
            return False, "unverified external tool or web output cannot directly become memory"

        # 4. Ephemeral status
        for pattern in EPHEMERAL_PATTERNS:
            if pattern.search(text):
                return False, "ephemeral operational state should remain in episodic records"

        # 5. Speculation
        for pattern in SPECULATION_PATTERNS:
            if pattern.search(text):
                return False, "speculative statements do not qualify for durable long-term memory"

        return True, "passed negative filters"

    def score_candidate(self, candidate: MemoryCandidate) -> float:
        """Compute comprehensive memory_score across durability, future utility, confidence, novelty, trust."""
        # Baseline durability & future utility from category
        cat_weights: dict[MemoryCandidateCategory, tuple[float, float]] = {
            MemoryCandidateCategory.USER_PREFERENCE: (0.95, 0.95),
            MemoryCandidateCategory.PROJECT_DECISION: (0.90, 0.90),
            MemoryCandidateCategory.PERSISTENT_CONSTRAINT: (0.95, 0.95),
            MemoryCandidateCategory.STABLE_FACT: (0.85, 0.85),
            MemoryCandidateCategory.CONFIGURATION_DECISION: (0.90, 0.90),
            MemoryCandidateCategory.CORRECTION: (0.95, 0.95),
            MemoryCandidateCategory.RELATIONSHIP: (0.80, 0.80),
            MemoryCandidateCategory.ONGOING_GOAL: (0.80, 0.75),
        }
        dur, fut = cat_weights.get(candidate.category, (0.75, 0.75))
        candidate.durability = dur
        candidate.future_utility = fut

        # Specificity
        words = len(candidate.content.split())
        if words < 3:
            candidate.specificity = 0.4
        elif words > 50:
            candidate.specificity = 0.7
        else:
            candidate.specificity = 0.9

        # Trust value
        trust_val = candidate.source_tier.value

        # Formula
        score = (
            candidate.durability * 0.25
            + candidate.future_utility * 0.25
            + candidate.confidence * 0.20
            + candidate.novelty * 0.15
            + trust_val * 0.15
            - candidate.sensitivity_risk * 1.0
            - candidate.contradiction_risk * 0.5
        )
        candidate.total_score = max(0.0, min(1.0, score))
        return candidate.total_score

    def check_contradiction_and_overlap(
        self, candidate: MemoryCandidate
    ) -> tuple[str, dict[str, Any] | None, str]:
        """Check if candidate duplicates or contradicts existing curated memories.

        Returns: (verdict, target_existing_memory, reason)
        verdict is one of: 'new', 'duplicate', 'supersede'
        """
        # Search existing memories with terms from candidate
        existing = self.store.search_memories(candidate.content, limit=10)
        curated_existing = [m for m in existing if m.get("kind") == "curated"]

        candidate_lower = candidate.content.lower().strip()
        candidate_words = set(re.findall(r"\w+", candidate_lower))

        for existing_mem in curated_existing:
            existing_content = str(existing_mem.get("content", "")).strip()
            existing_lower = existing_content.lower()
            existing_words = set(re.findall(r"\w+", existing_lower))

            # 1. Check for near-exact duplicate
            overlap = len(candidate_words & existing_words)
            union = len(candidate_words | existing_words) or 1
            jaccard = overlap / union

            if candidate_lower == existing_lower or jaccard >= 0.85:
                return "duplicate", existing_mem, "identical or near-duplicate of existing curated memory"

            # 2. Check for contradiction / supersession on key topics
            # Examples: "Use VPS" vs "Use Modal", "Single gateway" vs "Separate frontend", "Never X" vs "Always X"
            if self._is_contradiction(candidate_lower, existing_lower, candidate_words, existing_words):
                return (
                    "supersede",
                    existing_mem,
                    f"supersedes previous memory '{existing_content[:60]}...'",
                )

        return "new", None, "novel durable memory candidate"

    def _is_contradiction(
        self,
        candidate_text: str,
        existing_text: str,
        cand_words: set[str],
        exist_words: set[str],
    ) -> bool:
        """Heuristic contradiction detector for user preferences and project decisions."""
        # Inversion triggers
        inversions = [
            ("always", "never"),
            ("enable", "disable"),
            ("rootless", "rootful"),
            ("modal", "vps"),
            ("postgres", "sqlite"),
            ("separate", "single"),
        ]
        for a, b in inversions:
            if (a in cand_words and b in exist_words) or (b in cand_words and a in exist_words):
                shared = cand_words & exist_words - {a, b}
                if len(shared) >= 2:
                    return True

        # Explicit user change markers: "changed my mind", "instead of", "no longer", "switch to"
        change_markers = ["changed my mind", "instead of", "no longer", "switch to", "now use", "prefer"]
        if any(m in candidate_text for m in change_markers):
            # Check if there is significant subject overlap with existing
            shared = cand_words & exist_words
            if len(shared) >= 2:
                return True

        return False

    def detect_candidates_from_text(
        self,
        text: str,
        source_tier: SourceTrustTier,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> list[MemoryCandidate]:
        """Detect memory candidates from raw text or conversation turn."""
        candidates: list[MemoryCandidate] = []
        if not text or not text.strip():
            return candidates

        # Normalize lines
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        # Pattern detectors
        patterns: list[tuple[re.Pattern, MemoryCandidateCategory, str]] = [
            # Explicit remember
            (
                re.compile(r"^(?:(?:please\s+)?remember(?:\s+that|\s*:)?)\s+(.+)$", re.I),
                MemoryCandidateCategory.USER_PREFERENCE,
                "Explicit user request to remember",
            ),
            # Directives
            (
                re.compile(r"^(?:always|never|do not ever|must always)\s+(.+)$", re.I),
                MemoryCandidateCategory.PERSISTENT_CONSTRAINT,
                "Explicit operational rule / constraint",
            ),
            # User preferences
            (
                re.compile(r"^(?:i prefer|my preference is|we prefer)\s+(.+)$", re.I),
                MemoryCandidateCategory.USER_PREFERENCE,
                "Explicit user preference",
            ),
            # Project decisions
            (
                re.compile(r"^(?:we decided (?:to|that)|project decision:?|architecture:?)\s+(.+)$", re.I),
                MemoryCandidateCategory.PROJECT_DECISION,
                "Project architectural decision",
            ),
            # Corrections
            (
                re.compile(r"^(?:(?:no,\s*)?(?:don't|do not)\s+.+instead|correction:?\s*.+)$", re.I),
                MemoryCandidateCategory.CORRECTION,
                "Explicit user correction",
            ),
            # Stable facts
            (
                re.compile(r"^(?:(?:the\s+)?(?:production|system|gateway|core)\s+.+\s+uses|the\s+architecture\s+is)\s+(.+)$", re.I),
                MemoryCandidateCategory.STABLE_FACT,
                "Stable system fact",
            ),
        ]

        for line in lines:
            line_str = line.strip()
            # Strip common conversational / episodic introductory labels
            stripped = re.sub(
                r"^(?:discussed\s+(?:architecture|design|decision|requirements):?|note:?|decided:?|summary:?)\s*",
                "",
                line_str,
                flags=re.I,
            ).strip()

            for candidate_text in (line_str, stripped):
                matched = False
                for pat, cat, reason_desc in patterns:
                    m = pat.match(candidate_text)
                    if m:
                        extracted = candidate_text
                        # Clean prefix if explicit remember
                        if pat.pattern.startswith("^(?:(?:please\\s+)?remember"):
                            extracted = m.group(1).strip()

                        # Filter check
                        passed, filter_reason = self.check_negative_filters(extracted, source_tier)
                        if not passed:
                            LOGGER.debug("Memory candidate rejected by filter: %s (%s)", extracted, filter_reason)
                            matched = True
                            break

                        cand = MemoryCandidate(
                            id=str(uuid.uuid4()),
                            content=extracted,
                            category=cat,
                            source_tier=source_tier,
                            confidence=0.95 if source_tier == SourceTrustTier.USER_EXPLICIT else 0.85,
                            source_run_id=run_id,
                            source_conversation_id=conversation_id,
                            reason=reason_desc,
                        )
                        self.score_candidate(cand)
                        candidates.append(cand)
                        matched = True
                        break
                if matched:
                    break

        return candidates

    async def process_run(self, run: RunRecord) -> MemoryCurationResult:
        """Evaluate a completed run and curate durable long-term memory."""
        result = MemoryCurationResult()
        mode = self.mode

        # Retrieve run events
        events = self.store.list_events(run.id)

        # 1. Extract prompt and user messages
        user_prompt = ""
        if run.request and isinstance(run.request, dict):
            user_prompt = str(run.request.get("prompt", "")).strip()

        # Check prompt for explicit user directives (Highest Trust)
        candidates = self.detect_candidates_from_text(
            user_prompt,
            source_tier=SourceTrustTier.USER_EXPLICIT,
            run_id=run.id,
            conversation_id=run.conversation_id,
        )

        # Also check assistant response for self-contained decision summaries if prompted
        if run.assistant_response:
            resp_candidates = self.detect_candidates_from_text(
                run.assistant_response,
                source_tier=SourceTrustTier.AGENT_INFERENCE,
                run_id=run.id,
                conversation_id=run.conversation_id,
            )
            # Only include agent inference if high confidence
            for c in resp_candidates:
                if c.confidence >= 0.85:
                    candidates.append(c)

        for candidate in candidates:
            # Overlap & contradiction check
            verdict, existing_mem, reason = self.check_contradiction_and_overlap(candidate)
            candidate.verdict = verdict
            if existing_mem:
                candidate.target_memory_id = existing_mem.get("id")
                candidate.previous_content = existing_mem.get("content")

            # Governance decision based on mode
            if candidate.verdict == "discard" or candidate.verdict == "duplicate":
                result.discarded.append(candidate.to_dict())
                continue

            if mode == MemoryCurationMode.MANUAL:
                # In manual mode, only explicit remember directives are processed
                if candidate.source_tier == SourceTrustTier.USER_EXPLICIT and candidate.verdict != "supersede":
                    promoted = self.promote_candidate(candidate)
                    result.promoted.append(promoted)
                else:
                    result.discarded.append(candidate.to_dict())

            elif mode == MemoryCurationMode.ASSISTED:
                # High-confidence explicit facts promoted directly; supersessions / suggestions queued
                if candidate.source_tier == SourceTrustTier.USER_EXPLICIT and candidate.verdict == "new":
                    promoted = self.promote_candidate(candidate)
                    result.promoted.append(promoted)
                elif candidate.verdict == "supersede":
                    # Record as suggestion with supersession details for user approval
                    self._record_candidate_event(candidate, status="pending_approval")
                    result.suggested.append(candidate.to_dict())
                else:
                    # Inferred or lower confidence -> suggestion
                    self._record_candidate_event(candidate, status="pending_approval")
                    result.suggested.append(candidate.to_dict())

            elif mode == MemoryCurationMode.AUTOMATIC:
                # Automatically promotes high-confidence candidate or supersession
                if candidate.verdict == "supersede" and candidate.target_memory_id:
                    superseded = self.supersede_memory(
                        candidate.target_memory_id,
                        candidate.content,
                        reason=candidate.reason,
                        source_run_id=run.id,
                        source_conversation_id=run.conversation_id,
                    )
                    result.superseded.append(superseded)
                elif candidate.total_score >= 0.70:
                    promoted = self.promote_candidate(candidate)
                    result.promoted.append(promoted)
                else:
                    self._record_candidate_event(candidate, status="pending_approval")
                    result.suggested.append(candidate.to_dict())

        return result

    def promote_candidate(self, candidate: MemoryCandidate) -> dict[str, Any]:
        """Promote a candidate into curated long-term memory (MEMORY.md + SQLite FTS5)."""
        now = utc_now()
        source_label = f"run:{candidate.source_run_id[:8]}" if candidate.source_run_id else "user_explicit"

        # 1. Insert into memories table (FTS5 trigger synchronizes automatically)
        mem_id = self.store.add_memory(
            candidate.content,
            kind="curated",
            source=source_label,
            source_conversation_id=candidate.source_conversation_id,
            confidence=candidate.confidence,
            created_at=now,
        )

        # 2. Append to MEMORY.md (or USER.md if user preference)
        target_doc = "USER.md" if candidate.category == MemoryCandidateCategory.USER_PREFERENCE else "MEMORY.md"
        try:
            doc = self.identity.load((target_doc,))[0]
            marker = f"<!-- memory:{mem_id} -->"
            if marker not in doc.content:
                header = "\n\n## Curated Knowledge\n" if "## Curated Knowledge" not in doc.content else ""
                entry = f"\n- {candidate.content} {marker}\n"
                new_content = doc.content.rstrip() + header + entry
                self.identity.update(target_doc, new_content)
        except Exception:
            LOGGER.exception("Failed to update identity document %s for promoted memory %s", target_doc, mem_id)

        # 3. Record audit event
        self.store.record_audit(
            actor="memory_curator",
            action="memory.curator.promoted",
            resource_type="memory",
            resource_id=mem_id,
            payload={
                "content": candidate.content,
                "category": candidate.category.value if isinstance(candidate.category, MemoryCandidateCategory) else str(candidate.category),
                "score": candidate.total_score,
                "source_tier": candidate.source_tier.name if isinstance(candidate.source_tier, SourceTrustTier) else str(candidate.source_tier),
            },
        )

        return {
            "memory_id": mem_id,
            "content": candidate.content,
            "category": candidate.category.value if isinstance(candidate.category, MemoryCandidateCategory) else str(candidate.category),
            "status": "applied",
            "promoted_at": now,
        }

    def supersede_memory(
        self,
        memory_id: str,
        new_content: str,
        *,
        reason: str = "",
        source_run_id: str | None = None,
        source_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Supersede an existing memory with new content, preserving revision history."""
        existing = self.store.get_memory(memory_id)
        previous_content = str(existing.get("content", ""))
        now = utc_now()

        # 1. Record historical revision
        revision_record = self.store.record_memory_revision(
            memory_id,
            previous_content=previous_content,
            new_content=new_content,
            reason=reason or f"Superseded by updated statement",
            superseded_at=now,
            source_run_id=source_run_id,
            source_conversation_id=source_conversation_id,
        )

        # 2. Update memory record in SQLite
        self.store.update_memory(memory_id, content=new_content)

        # 3. Update MEMORY.md or USER.md marker
        marker = f"<!-- memory:{memory_id} -->"
        for doc_name in ("MEMORY.md", "USER.md"):
            try:
                doc = self.identity.load((doc_name,))[0]
                if marker in doc.content:
                    lines = doc.content.splitlines()
                    new_lines = []
                    for line in lines:
                        if marker in line:
                            new_lines.append(f"- {new_content} {marker}")
                        else:
                            new_lines.append(line)
                    self.identity.update(doc_name, "\n".join(new_lines) + "\n")
                    break
            except Exception:
                LOGGER.exception("Failed to update marker %s in %s", marker, doc_name)

        # 4. Record audit event
        self.store.record_audit(
            actor="memory_curator",
            action="memory.curator.superseded",
            resource_type="memory",
            resource_id=memory_id,
            payload={
                "previous_content": previous_content,
                "new_content": new_content,
                "revision": revision_record.get("revision", 1),
                "reason": reason,
            },
        )

        return {
            "memory_id": memory_id,
            "revision": revision_record.get("revision", 1),
            "previous_content": previous_content,
            "new_content": new_content,
            "superseded_at": now,
            "reason": reason,
        }

    def remember_explicit(
        self,
        text: str,
        *,
        category: MemoryCandidateCategory | str = MemoryCandidateCategory.USER_PREFERENCE,
        conversation_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Handle an explicit user remember command (/remember or 'Remember that...')."""
        clean_text = text.strip()
        # Strip common prefixes
        clean_text = re.sub(r"^(?:(?:please\s+)?remember(?:\s+that|\s*:)?|\/remember)\s+", "", clean_text, flags=re.I).strip()
        if not clean_text:
            raise ValueError("content to remember must not be empty")

        cat_enum = MemoryCandidateCategory(category) if isinstance(category, str) else category

        # 1. Negative filter check
        passed, filter_reason = self.check_negative_filters(clean_text, SourceTrustTier.USER_EXPLICIT)
        if not passed:
            raise ValueError(f"Memory rejected: {filter_reason}")

        cand = MemoryCandidate(
            id=str(uuid.uuid4()),
            content=clean_text,
            category=cat_enum,
            source_tier=SourceTrustTier.USER_EXPLICIT,
            confidence=1.0,
            source_run_id=run_id,
            source_conversation_id=conversation_id,
            reason="Explicit user directive",
        )
        self.score_candidate(cand)

        # 2. Contradiction & supersession check
        verdict, existing_mem, reason = self.check_contradiction_and_overlap(cand)

        if verdict == "duplicate" and existing_mem:
            return {
                "status": "duplicate",
                "memory_id": existing_mem.get("id"),
                "content": existing_mem.get("content"),
                "message": "Memory already exists and is up to date.",
            }

        if verdict == "supersede" and existing_mem:
            superseded = self.supersede_memory(
                existing_mem["id"],
                clean_text,
                reason=reason,
                source_run_id=run_id,
                source_conversation_id=conversation_id,
            )
            return {
                "status": "superseded",
                "memory_id": existing_mem["id"],
                "content": clean_text,
                "previous_content": superseded.get("previous_content"),
                "revision": superseded.get("revision"),
                "message": f"Updated existing memory (revision {superseded.get('revision')}).",
            }

        # 3. Promote fresh memory
        promoted = self.promote_candidate(cand)
        return {
            "status": "promoted",
            "memory_id": promoted["memory_id"],
            "content": clean_text,
            "message": "Saved to curated long-term memory.",
        }

    def consolidate_journals(self, days_back: int = 7) -> ConsolidationReport:
        """Periodic episodic journal consolidation ('dreaming' layer).

        Scans daily journals (memory/*.md) and extracts recurring patterns,
        stable facts, and user preferences into long-term memory candidates.
        """
        start_time = utc_now()
        journals = self.memory_service.list_journals()
        cutoff_date = (datetime.now(UTC) - timedelta(days=days_back)).date().isoformat()

        scanned_journals = 0
        analyzed_entries = 0
        discovered = 0
        promoted_count = 0
        superseded_count = 0

        for j in journals:
            date_str = str(j.get("date", ""))
            if date_str < cutoff_date:
                continue
            scanned_journals += 1
            journal_data = self.memory_service.read_journal(date_str)
            content = str(journal_data.get("content", ""))

            # Extract entries
            for line in content.splitlines():
                line = line.strip()
                if not line.startswith("- "):
                    continue
                analyzed_entries += 1

                # Clean entry content
                clean_line = re.sub(r"^-\s+[0-9T:\-\.\+Z]+\s+\[[^\]]+\]\s*(?:<!--\s*memory:[^>]+-->)?\s*", "", line).strip()
                if not clean_line:
                    continue

                candidates = self.detect_candidates_from_text(
                    clean_line,
                    source_tier=SourceTrustTier.REPEATED_RUN_CONCLUSION,
                )
                for cand in candidates:
                    discovered += 1
                    verdict, existing_mem, reason = self.check_contradiction_and_overlap(cand)
                    cand.verdict = verdict

                    if verdict == "duplicate":
                        continue

                    if self.mode == MemoryCurationMode.AUTOMATIC:
                        if verdict == "supersede" and existing_mem:
                            self.supersede_memory(existing_mem["id"], cand.content, reason=reason)
                            superseded_count += 1
                        elif cand.total_score >= 0.75:
                            self.promote_candidate(cand)
                            promoted_count += 1
                        else:
                            self._record_candidate_event(cand, status="pending_approval")
                    else:
                        # Assisted / Manual mode: record as suggestion for user review
                        self._record_candidate_event(cand, status="pending_approval")

        completed_time = utc_now()
        summary = (
            f"Consolidation completed across {scanned_journals} daily journals. "
            f"Analyzed {analyzed_entries} episodic entries, discovered {discovered} candidates, "
            f"promoted {promoted_count}, superseded {superseded_count}."
        )

        return ConsolidationReport(
            started_at=start_time,
            completed_at=completed_time,
            journals_scanned=scanned_journals,
            entries_analyzed=analyzed_entries,
            candidates_discovered=discovered,
            memories_promoted=promoted_count,
            memories_superseded=superseded_count,
            summary=summary,
        )

    def _record_candidate_event(self, candidate: MemoryCandidate, status: str = "pending_approval") -> None:
        """Persist a memory candidate suggestion into the learning_events table."""
        try:
            target_label = f"memory:{candidate.category.value}:{candidate.id[:8]}"
            # Ensure dummy run_id / conversation_id if None to satisfy constraints
            run_id = candidate.source_run_id or f"curator:{candidate.id[:8]}"
            conv_id = candidate.source_conversation_id or f"curator_conv"
            
            # Check if event already exists for identical summary
            existing_events = self.store.list_learning_events(status=status, limit=100)
            if any(e.summary == candidate.content for e in existing_events):
                return

            self.store.record_learning_event(
                run_id=run_id,
                conversation_id=conv_id,
                event_type=f"memory_{candidate.verdict}",
                target=target_label,
                status=status,
                summary=candidate.content,
                reviewer_model="memory_curator",
            )
        except Exception:
            LOGGER.exception("Failed to record memory candidate event for '%s'", candidate.content)
