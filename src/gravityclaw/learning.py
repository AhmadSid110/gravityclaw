"""GravityClaw Learning Mode — Phase 1 kernel.

Post-run learning pipeline:
  RunManager._finalize()
    -> LearningEligibilityGate (deterministic, scored)
    -> background LearningJob (durable, retryable)
    -> auxiliary reviewer (cheap/fast model)
    -> typed LearningOperations
    -> LearningEngine validates, applies, audits

The model never directly mutates long-term knowledge. It proposes a typed
Learning Operation; the Learning Engine validates, applies, versions, audits,
and optionally requires approval.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

from .identity import IdentityStore
from .memory import MemoryService
from .store import RunRecord, Store


LOGGER = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Eligibility Gate — cheap deterministic scoring before any LLM call
# ─────────────────────────────────────────────────────────────────────────────

# Signal weights: each detected signal adds its weight to the score.
# The gate fires when total >= threshold.
DEFAULT_SIGNAL_WEIGHTS: dict[str, float] = {
    "tool_calls": 2.0,
    "user_correction": 4.0,
    "task_success_after_failure": 3.0,
    "configuration_change": 3.0,
    "new_environment_fact": 2.5,
    "explicit_preference": 3.5,
    "long_or_complex_turn": 1.5,
    "skill_was_loaded": 1.5,
    "skill_was_corrected": 4.0,
    "explicit_learn_request": 10.0,
    "new_tool_discovered": 2.5,
    "multi_step_resolution": 2.0,
}

DEFAULT_GATE_THRESHOLD = 3.0


@dataclass(frozen=True, slots=True)
class GateResult:
    eligible: bool
    score: float
    fired_signals: tuple[str, ...]


class LearningEligibilityGate:
    """Deterministic scored gate — no LLM call.

    Examines the completed run's events to detect learning-worthy signals.
    If accumulated weight >= threshold, the turn is eligible for review.
    """

    def __init__(
        self,
        threshold: float = DEFAULT_GATE_THRESHOLD,
        signal_weights: dict[str, float] | None = None,
    ) -> None:
        self.threshold = threshold
        self.weights = signal_weights or dict(DEFAULT_SIGNAL_WEIGHTS)

    def evaluate(self, run: RunRecord, events: list[dict[str, Any]]) -> GateResult:
        """Score a completed run for learning eligibility.

        ``events`` should be the denormalized event list for the run
        (event_type + payload dicts).
        """
        fired: list[str] = []
        score = 0.0

        # Detect signals from event stream
        tool_call_count = 0
        tool_names_seen: set[str] = set()
        has_failure_then_success = False
        has_correction = False
        event_types = [e.get("event_type", "") for e in events]

        for event in events:
            etype = event.get("event_type", "")
            payload = event.get("payload", {})
            if not isinstance(payload, dict):
                payload = {}

            if etype == "tool.executed":
                tool_call_count += 1
                name = payload.get("tool_name") or payload.get("name", "")
                if name:
                    tool_names_seen.add(name)
            elif etype == "tool.failed":
                # If there's a subsequent success, that's learning-worthy
                pass

        # Signal: tool_calls > 0
        if tool_call_count > 0:
            fired.append("tool_calls")
            score += self.weights.get("tool_calls", 0)

        # Signal: long_or_complex_turn (many events or many tool calls)
        if tool_call_count >= 5 or len(events) >= 15:
            fired.append("long_or_complex_turn")
            score += self.weights.get("long_or_complex_turn", 0)

        # Signal: task_success_after_failure
        failure_indices = [i for i, e in enumerate(events) if e.get("event_type") == "tool.failed"]
        success_indices = [i for i, e in enumerate(events) if e.get("event_type") == "agent.completed"]
        if failure_indices and success_indices and max(success_indices) > min(failure_indices):
            fired.append("task_success_after_failure")
            score += self.weights.get("task_success_after_failure", 0)

        # Signal: new_tool_discovered (tool name not previously seen — approximated
        # by checking if any tool is used for the first time in this conversation;
        # callers can pass ``known_tools`` context in the future)
        if tool_names_seen:
            fired.append("new_tool_discovered")
            score += self.weights.get("new_tool_discovered", 0)

        # Signals that require request/prompt introspection
        request = run.request if isinstance(run.request, dict) else {}
        prompt = str(request.get("prompt", "")).lower()

        # Signal: explicit_learn_request
        learn_markers = ("remember this", "learn this", "/learn", "note this down")
        if any(marker in prompt for marker in learn_markers):
            fired.append("explicit_learn_request")
            score += self.weights.get("explicit_learn_request", 0)

        # Signal: explicit_preference
        preference_markers = ("i prefer", "always use", "never use", "default to", "i like", "i don't like")
        if any(marker in prompt for marker in preference_markers):
            fired.append("explicit_preference")
            score += self.weights.get("explicit_preference", 0)

        # Signal: user_correction
        correction_markers = ("no, ", "wrong", "that's not", "actually ", "i meant", "correction:")
        if any(marker in prompt for marker in correction_markers):
            fired.append("user_correction")
            score += self.weights.get("user_correction", 0)

        # Signal: configuration_change (tool calls that modify config-like files)
        config_tools = {"write_file", "edit_file", "fs_write", "str_replace"}
        if tool_names_seen & config_tools:
            for event in events:
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                path = str(payload.get("path", "") or payload.get("file", ""))
                if any(marker in path.lower() for marker in (
                    "config", ".toml", ".yaml", ".yml", ".env", ".json",
                    "settings", "gravityclaw.toml",
                )):
                    fired.append("configuration_change")
                    score += self.weights.get("configuration_change", 0)
                    break

        # Signal: goal_continuation runs typically aren't learning-worthy
        # (they're intermediate steps) — skip unless other strong signals
        if request.get("goal_continuation"):
            score *= 0.5  # Dampen, don't zero

        eligible = score >= self.threshold
        return GateResult(eligible=eligible, score=score, fired_signals=tuple(fired))


# ─────────────────────────────────────────────────────────────────────────────
# Structured Learning Result — what the reviewer produces
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MemoryOperation:
    """A single proposed memory mutation."""
    namespace: str  # "agent" (MEMORY.md) or "user" (USER.md)
    operation: str  # "upsert" | "remove"
    key: str  # Short identifier for the fact
    content: str  # The knowledge to store
    confidence: float = 0.9
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """A proposed skill creation/patch from the reviewer (Phase 2)."""
    operation: str  # "create" | "patch" | "archive"
    name: str
    description: str
    content: str
    reason: str = ""
    confidence: float = 0.8
    skill_id: str | None = None


@dataclass(frozen=True, slots=True)
class LearningResult:
    """Structured output from the auxiliary reviewer."""
    worth_learning: bool
    memory_operations: tuple[MemoryOperation, ...] = ()
    skill_candidates: tuple[SkillCandidate, ...] = ()
    summary: str = ""
    reviewer_model: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Learning Engine — validates and applies operations
# ─────────────────────────────────────────────────────────────────────────────

LEARNING_JOB_STATES = ("pending", "running", "completed", "failed", "expired")


@dataclass(frozen=True, slots=True)
class LearningJob:
    id: str
    run_id: str
    conversation_id: str
    state: str
    gate_score: float
    gate_signals: tuple[str, ...]
    context_json: str  # Serialized context for the reviewer
    result_json: str | None
    attempts: int
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class LearningEvent:
    id: str
    run_id: str
    conversation_id: str
    event_type: str  # "memory_upsert" | "memory_remove" | "skill_candidate" | "review_skipped"
    target: str  # e.g., "MEMORY.md", "USER.md", or skill name
    status: str  # "applied" | "pending_approval" | "rejected" | "failed"
    summary: str
    reviewer_model: str
    created_at: str


# Reviewer prompt template
_REVIEWER_SYSTEM_PROMPT = """\
You are a learning extraction engine for GravityClaw, a personal AI agent platform.

Analyze the conversation turn below and extract durable knowledge worth remembering.

Rules:
- Extract ONLY knowledge that would be useful across future conversations.
- DO NOT extract ephemeral facts (today's weather, one-time calculations).
- DO NOT extract information already obvious from the conversation context.
- Prefer concise, actionable facts over verbose descriptions.
- namespace "agent" → operational knowledge for the agent (goes to MEMORY.md)
- namespace "user" → facts about the user (goes to USER.md)
- Each memory operation needs a short `key` for deduplication.
- Confidence: 0.0–1.0 (how certain this is durable, reusable knowledge).
- Set worth_learning to false if nothing durable was learned.

Respond with ONLY valid JSON matching this schema:
{
  "worth_learning": boolean,
  "summary": "one-line summary of what was learned",
  "memory": [
    {
      "namespace": "agent" | "user",
      "operation": "upsert" | "remove",
      "key": "short-identifier",
      "content": "the knowledge to store",
      "confidence": 0.0-1.0,
      "reason": "why this is worth remembering"
    }
  ],
  "skills": [
    {
      "operation": "create" | "patch",
      "name": "kebab-case-skill-name",
      "description": "one-line description of the skill",
      "content": "full SKILL.md content",
      "reason": "why this procedure is reusable",
      "confidence": 0.0-1.0
    }
  ]
}

Skill guidelines:
- Only propose a skill when the turn contains a reusable multi-step procedure.
- Use "create" for new procedures, "patch" for improving existing ones.
- The content should be a complete SKILL.md with clear steps.
- Do NOT propose a skill for one-off tasks or simple lookups.
- Confidence: how certain this is genuinely reusable (not just done once).
"""

_REVIEWER_USER_TEMPLATE = """\
## Conversation Turn

User prompt:
{user_prompt}

Assistant response (summary):
{assistant_summary}

Tool calls made: {tool_summary}

Signals detected: {signals}
"""


class LearningEngine:
    """Coordinates the learning pipeline: gate → job → review → apply.

    The engine is instantiated once at app startup and called from
    RunManager._finalize() after goal evaluation.
    """

    def __init__(
        self,
        store: Store,
        identity: IdentityStore,
        memory: MemoryService,
        *,
        gate: LearningEligibilityGate | None = None,
        enabled: bool = True,
        reviewer_provider: str = "google",
        reviewer_model: str = "gemini-2.0-flash",
        fallback_to_primary: bool = False,
        max_input_tokens: int = 12_000,
        max_output_tokens: int = 1_200,
        max_retries: int = 2,
        memory_approval_required: bool = False,
        skill_service: Any | None = None,
    ) -> None:
        self.store = store
        self.identity = identity
        self.memory = memory
        self.gate = gate or LearningEligibilityGate()
        self.enabled = enabled
        self.reviewer_provider = reviewer_provider
        self.reviewer_model = reviewer_model
        self.fallback_to_primary = fallback_to_primary
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self.memory_approval_required = memory_approval_required
        self.skill_service = skill_service  # Phase 2: SkillService instance
        self._background_tasks: set[asyncio.Task[None]] = set()

    async def process_run(self, run: RunRecord) -> None:
        """Entry point called from RunManager._finalize().

        This method is non-blocking: it evaluates the gate synchronously,
        and if eligible, enqueues a durable background job for async review.
        """
        if not self.enabled:
            return

        # Gather events for gate evaluation
        events = self._get_run_events(run)
        gate_result = self.gate.evaluate(run, events)

        if not gate_result.eligible:
            LOGGER.debug(
                "learning gate: run %s not eligible (score=%.1f, threshold=%.1f)",
                run.id, gate_result.score, self.gate.threshold,
            )
            return

        LOGGER.info(
            "learning gate: run %s eligible (score=%.1f, signals=%s)",
            run.id, gate_result.score, gate_result.fired_signals,
        )

        # Build reviewer context
        context = self._build_review_context(run, events, gate_result)

        # Create durable learning job
        job = self.store.create_learning_job(
            run_id=run.id,
            conversation_id=run.conversation_id,
            gate_score=gate_result.score,
            gate_signals=list(gate_result.fired_signals),
            context=context,
        )

        # Fire background task (non-blocking)
        task = asyncio.create_task(
            self._execute_review(job),
            name=f"learning-{job.id[:8]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _execute_review(self, job: LearningJob) -> None:
        """Background: call the auxiliary reviewer and apply results."""
        try:
            self.store.update_learning_job(job.id, state="running")
            context = json.loads(job.context_json)

            # Call the reviewer model
            result = await self._call_reviewer(context)

            if result is None or not result.worth_learning:
                self.store.update_learning_job(
                    job.id,
                    state="completed",
                    result={"worth_learning": False, "summary": "nothing worth learning"},
                )
                self.store.record_learning_event(
                    run_id=job.run_id,
                    conversation_id=job.conversation_id,
                    event_type="review_skipped",
                    target="",
                    status="applied",
                    summary="reviewer found nothing worth learning",
                    reviewer_model=self.reviewer_model,
                )
                return

            # Apply memory operations
            for op in result.memory_operations:
                await self._apply_memory_operation(op, job)

            # Apply skill operations (Phase 2)
            for candidate in result.skill_candidates:
                await self._apply_skill_operation(candidate, job)

            # Persist the result
            self.store.update_learning_job(
                job.id,
                state="completed",
                result={
                    "worth_learning": True,
                    "summary": result.summary,
                    "memory_count": len(result.memory_operations),
                    "skill_count": len(result.skill_candidates),
                },
            )
            LOGGER.info(
                "learning job %s completed: %d memory ops, summary=%s",
                job.id, len(result.memory_operations), result.summary,
            )
        except Exception as exc:
            LOGGER.exception("learning job %s failed", job.id)
            attempts = job.attempts + 1
            if attempts >= self.max_retries:
                self.store.update_learning_job(
                    job.id, state="failed", attempts=attempts,
                )
            else:
                self.store.update_learning_job(
                    job.id, state="pending", attempts=attempts,
                )

    async def _call_reviewer(self, context: dict[str, Any]) -> LearningResult | None:
        """Call the auxiliary reviewer model.

        Uses a lightweight HTTP call to the configured provider.
        In Phase 1, this uses a subprocess call to `agy` or a direct
        HTTP request. The implementation is pluggable.
        """
        user_prompt = context.get("user_prompt", "")
        assistant_summary = context.get("assistant_summary", "")
        tool_summary = context.get("tool_summary", "")
        signals = ", ".join(context.get("signals", []))

        reviewer_input = _REVIEWER_USER_TEMPLATE.format(
            user_prompt=user_prompt[:self.max_input_tokens * 3],
            assistant_summary=assistant_summary[:2000],
            tool_summary=tool_summary[:1000],
            signals=signals,
        )

        # Use the built-in review path: call the model via subprocess
        # This is the Phase 1 implementation using agy CLI.
        # Future phases may use direct HTTP.
        import subprocess

        payload = json.dumps({
            "messages": [
                {"role": "system", "content": _REVIEWER_SYSTEM_PROMPT},
                {"role": "user", "content": reviewer_input},
            ],
            "model": self.reviewer_model,
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_output_tokens,
        })

        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["agy", "chat", "--json", "--stdin"],
                input=payload,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                LOGGER.warning(
                    "reviewer subprocess failed (exit %d): %s",
                    proc.returncode, proc.stderr[:500],
                )
                return None

            raw = json.loads(proc.stdout)
            return self._parse_reviewer_response(raw)
        except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
            LOGGER.warning("reviewer call failed: %s", exc)
            return None

    def _parse_reviewer_response(self, raw: dict[str, Any]) -> LearningResult | None:
        """Parse structured JSON from the reviewer into typed objects."""
        try:
            # Handle both direct response and wrapped response formats
            if "choices" in raw:
                # OpenAI-style wrapper
                content = raw["choices"][0]["message"]["content"]
                data = json.loads(content) if isinstance(content, str) else content
            elif "worth_learning" in raw:
                data = raw
            else:
                # Try parsing stdout as raw JSON
                data = raw

            if not isinstance(data, dict):
                return None

            worth = bool(data.get("worth_learning", False))
            if not worth:
                return LearningResult(worth_learning=False, reviewer_model=self.reviewer_model)

            memory_ops = []
            for item in data.get("memory", []):
                if not isinstance(item, dict):
                    continue
                namespace = str(item.get("namespace", "agent"))
                if namespace not in ("agent", "user"):
                    namespace = "agent"
                memory_ops.append(MemoryOperation(
                    namespace=namespace,
                    operation=str(item.get("operation", "upsert")),
                    key=str(item.get("key", "")),
                    content=str(item.get("content", "")),
                    confidence=float(item.get("confidence", 0.9)),
                    reason=str(item.get("reason", "")),
                ))

            return LearningResult(
                worth_learning=True,
                memory_operations=tuple(memory_ops),
                skill_candidates=tuple(self._parse_skill_candidates(data)),
                summary=str(data.get("summary", "")),
                reviewer_model=self.reviewer_model,
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            LOGGER.warning("failed to parse reviewer response: %s", exc)
            return None

    async def _apply_memory_operation(
        self,
        op: MemoryOperation,
        job: LearningJob,
    ) -> None:
        """Apply a single memory operation to the appropriate identity file."""
        if self.memory_approval_required:
            # In approval-required mode, just record the event as pending
            self.store.record_learning_event(
                run_id=job.run_id,
                conversation_id=job.conversation_id,
                event_type=f"memory_{op.operation}",
                target=f"{op.namespace}:{op.key}",
                status="pending_approval",
                summary=op.content[:200],
                reviewer_model=self.reviewer_model,
            )
            return

        target_file = "MEMORY.md" if op.namespace == "agent" else "USER.md"
        try:
            if op.operation == "upsert":
                # Read current content, append or update the knowledge
                doc = self.identity.load((target_file,))[0]
                updated = self._merge_knowledge(doc.content, op)
                self.identity.update(target_file, updated)

                # Also record as episodic memory for FTS retrieval
                self.memory.record_episode(
                    op.content,
                    source=f"learning:{job.run_id[:8]}",
                    conversation_id=job.conversation_id,
                    confidence=op.confidence,
                )
            elif op.operation == "remove":
                doc = self.identity.load((target_file,))[0]
                updated = self._remove_knowledge(doc.content, op.key)
                if updated != doc.content:
                    self.identity.update(target_file, updated)

            self.store.record_learning_event(
                run_id=job.run_id,
                conversation_id=job.conversation_id,
                event_type=f"memory_{op.operation}",
                target=f"{op.namespace}:{op.key}",
                status="applied",
                summary=op.content[:200],
                reviewer_model=self.reviewer_model,
            )
            LOGGER.info(
                "learning: applied %s to %s (key=%s, confidence=%.2f)",
                op.operation, target_file, op.key, op.confidence,
            )
        except Exception as exc:
            LOGGER.warning(
                "learning: failed to apply %s to %s: %s",
                op.operation, target_file, exc,
            )
            self.store.record_learning_event(
                run_id=job.run_id,
                conversation_id=job.conversation_id,
                event_type=f"memory_{op.operation}",
                target=f"{op.namespace}:{op.key}",
                status="failed",
                summary=f"error: {exc}",
                reviewer_model=self.reviewer_model,
            )

    @staticmethod
    def _parse_skill_candidates(data: dict[str, Any]) -> list[SkillCandidate]:
        """Parse skill candidates from the reviewer response."""
        candidates = []
        for item in data.get("skills", []):
            if not isinstance(item, dict):
                continue
            operation = str(item.get("operation", "create"))
            if operation not in ("create", "patch", "archive"):
                operation = "create"
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            candidates.append(SkillCandidate(
                operation=operation,
                name=name,
                description=str(item.get("description", "")),
                content=str(item.get("content", "")),
                reason=str(item.get("reason", "")),
                confidence=float(item.get("confidence", 0.8)),
                skill_id=item.get("skill_id"),
            ))
        return candidates

    async def _apply_skill_operation(
        self,
        candidate: SkillCandidate,
        job: LearningJob,
    ) -> None:
        """Dispatch a skill candidate to the SkillService."""
        if self.skill_service is None:
            # Phase 2 not wired — just record the event
            self.store.record_learning_event(
                run_id=job.run_id,
                conversation_id=job.conversation_id,
                event_type="skill_candidate",
                target=candidate.name,
                status="pending_approval",
                summary=f"{candidate.operation}: {candidate.description}"[:200],
                reviewer_model=self.reviewer_model,
            )
            return

        try:
            from .skills.models import SkillOperation

            op = SkillOperation(
                operation=candidate.operation,
                name=candidate.name,
                description=candidate.description,
                content=candidate.content,
                reason=candidate.reason,
                confidence=candidate.confidence,
                skill_id=candidate.skill_id,
            )
            result_id = self.skill_service.process_skill_operation(
                op,
                source_run_id=job.run_id,
                review_model=self.reviewer_model,
            )

            status = "pending_approval" if result_id else "failed"
            self.store.record_learning_event(
                run_id=job.run_id,
                conversation_id=job.conversation_id,
                event_type="skill_candidate",
                target=candidate.name,
                status=status,
                summary=f"{candidate.operation}: {candidate.description}"[:200],
                reviewer_model=self.reviewer_model,
            )
            LOGGER.info(
                "learning: skill %s '%s' → %s (id=%s)",
                candidate.operation, candidate.name, status, result_id,
            )
        except Exception as exc:
            LOGGER.warning(
                "learning: failed to process skill operation %s '%s': %s",
                candidate.operation, candidate.name, exc,
            )
            self.store.record_learning_event(
                run_id=job.run_id,
                conversation_id=job.conversation_id,
                event_type="skill_candidate",
                target=candidate.name,
                status="failed",
                summary=f"error: {exc}"[:200],
                reviewer_model=self.reviewer_model,
            )

    @staticmethod
    def _merge_knowledge(content: str, op: MemoryOperation) -> str:
        """Merge a knowledge item into MEMORY.md or USER.md.

        Strategy: append under a '## Learned' section if not already present.
        If the key already exists in that section, replace the line.
        """
        section_header = "## Learned\n"
        key_marker = f"<!-- learn:{op.key} -->"
        new_line = f"- {key_marker} {op.content}\n"

        if key_marker in content:
            # Replace existing entry
            lines = content.splitlines(keepends=True)
            result = []
            for line in lines:
                if key_marker in line:
                    result.append(new_line)
                else:
                    result.append(line)
            return "".join(result)

        # Append under ## Learned section
        if section_header in content:
            # Insert after the header
            idx = content.index(section_header) + len(section_header)
            return content[:idx] + new_line + content[idx:]
        else:
            # Create the section at the end
            if not content.endswith("\n"):
                content += "\n"
            return content + f"\n{section_header}{new_line}"

    @staticmethod
    def _remove_knowledge(content: str, key: str) -> str:
        """Remove a learned fact by its key marker."""
        key_marker = f"<!-- learn:{key} -->"
        if key_marker not in content:
            return content
        lines = content.splitlines(keepends=True)
        return "".join(line for line in lines if key_marker not in line)

    def _get_run_events(self, run: RunRecord) -> list[dict[str, Any]]:
        """Load events for a run as plain dicts for gate evaluation."""
        events = self.store.list_events(run.id)
        return [
            {
                "event_type": event.event_type,
                "payload": event.payload if isinstance(event.payload, dict) else {},
            }
            for event in events
        ]

    def _build_review_context(
        self,
        run: RunRecord,
        events: list[dict[str, Any]],
        gate_result: GateResult,
    ) -> dict[str, Any]:
        """Build the context dict passed to the reviewer model."""
        request = run.request if isinstance(run.request, dict) else {}
        user_prompt = str(request.get("prompt", ""))

        # Extract assistant response from events
        assistant_summary = ""
        for event in reversed(events):
            if event.get("event_type") == "agent.completed":
                payload = event.get("payload", {})
                assistant_summary = str(payload.get("response", ""))[:2000]
                break

        # Summarize tool usage
        tool_calls: list[str] = []
        for event in events:
            if event.get("event_type") == "tool.executed":
                payload = event.get("payload", {})
                name = payload.get("tool_name") or payload.get("name", "unknown")
                tool_calls.append(name)

        context = {
            "user_prompt": user_prompt[:self.max_input_tokens * 3],
            "assistant_summary": assistant_summary,
            "tool_summary": ", ".join(tool_calls[:20]) or "none",
            "signals": list(gate_result.fired_signals),
            "run_id": run.id,
            "conversation_id": run.conversation_id,
        }

        # Phase 3: Enrich with causal attribution if skill_service is wired
        if self.skill_service is not None:
            try:
                from .skills.attribution import build_attribution, enrich_reviewer_context

                # Load RunSkillContext for this run
                run_ctx_json = self.skill_service.get_run_context(run.id)
                if run_ctx_json is not None:
                    run_ctx_dict = run_ctx_json.to_dict()

                    # Get telemetry events for skills in this run
                    loaded_skills = run_ctx_dict.get("loaded", [])
                    skill_telemetry = []
                    for ls in loaded_skills:
                        sid = ls.get("skill_id", "")
                        if sid:
                            stats = self.skill_service.telemetry.stats(sid)
                            # Build event list from recent events for this run
                            skill_telemetry.extend([
                                {"skill_id": sid, "event": evt}
                                for evt in ("loaded", "executed", "successful", "failed", "corrected")
                                if stats.get(evt, 0) > 0
                            ])

                    attribution = build_attribution(run.id, run_ctx_dict, skill_telemetry)
                    context = enrich_reviewer_context(context, attribution)
            except Exception as exc:
                LOGGER.debug("attribution enrichment failed: %s", exc)

        return context

    async def close(self) -> None:
        """Cancel pending background tasks on shutdown."""
        for task in self._background_tasks:
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
