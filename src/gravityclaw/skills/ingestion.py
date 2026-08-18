"""/learn ingestion — source classification, chunking, synthesis, deduplication.

Implements the /learn command abstraction: takes a source of knowledge
(conversation, document, file, repository, etc.), classifies it, chunks
large sources, synthesizes into a skill proposal, and deduplicates against
the registry before creating a SkillProposal.

The critical rule: /learn does NOT bypass the Phase-2 safety machinery.
It ultimately produces the same SkillProposal objects as background learning.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Sequence

from .models import SkillOwner
from .registry import SkillRegistry

LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Source classification
# ─────────────────────────────────────────────────────────────────────────────


class SourceType(StrEnum):
    """Types of knowledge sources for /learn."""
    CONVERSATION = "conversation"
    WEB_PAGE = "web_page"
    LOCAL_FILE = "local_file"
    PDF = "pdf"
    DIRECTORY = "directory"
    REPOSITORY = "repository"
    DOCUMENTATION_TREE = "documentation_tree"
    PLAIN_TEXT = "plain_text"


# Size thresholds for routing
SMALL_SOURCE_CHARS = 12_000    # Sources below this go through normal processing
LARGE_SOURCE_CHARS = 12_000    # Above this → chunked ingestion planner
MAX_CHUNK_CHARS = 8_000        # Individual chunk target size
MAX_CHUNKS = 20                # Maximum chunks per source


@dataclass(frozen=True, slots=True)
class SourceClassification:
    """Result of classifying a /learn source."""
    source_type: SourceType
    source_identity: str           # URL, path, or identifier
    estimated_chars: int
    is_large: bool                 # Whether chunked processing is needed
    content_hash: str              # SHA-256 of raw content for dedup
    metadata: dict[str, Any] = field(default_factory=dict)


def classify_source(
    source: str,
    *,
    content: str | None = None,
) -> SourceClassification:
    """Classify the source type and estimate scope.

    Args:
        source: The source identifier (URL, path, or raw text).
        content: Pre-fetched content if available.

    Returns:
        SourceClassification with type and size routing info.
    """
    estimated_chars = len(content) if content else 0
    content_hash = hashlib.sha256(
        (content or source).encode("utf-8", errors="replace")
    ).hexdigest()
    metadata: dict[str, Any] = {}

    # URL detection
    if re.match(r"https?://", source, re.IGNORECASE):
        source_type = SourceType.WEB_PAGE
        metadata["url"] = source
    # File path detection
    elif os.path.exists(source):
        path = Path(source)
        if path.is_dir():
            # Check if it's a git repo
            if (path / ".git").exists():
                source_type = SourceType.REPOSITORY
            elif _looks_like_docs_tree(path):
                source_type = SourceType.DOCUMENTATION_TREE
            else:
                source_type = SourceType.DIRECTORY
            # Estimate content from directory
            if content is None:
                estimated_chars = _estimate_directory_size(path)
        elif path.suffix.lower() == ".pdf":
            source_type = SourceType.PDF
            if content is None:
                estimated_chars = path.stat().st_size * 2  # Rough char estimate
        else:
            source_type = SourceType.LOCAL_FILE
            if content is None:
                try:
                    estimated_chars = path.stat().st_size
                except OSError:
                    pass
        metadata["path"] = str(path.resolve())
    # Conversation context (multi-line with role markers)
    elif _looks_like_conversation(source):
        source_type = SourceType.CONVERSATION
        estimated_chars = len(source)
    # Plain text
    else:
        source_type = SourceType.PLAIN_TEXT
        estimated_chars = estimated_chars or len(source)

    return SourceClassification(
        source_type=source_type,
        source_identity=source[:500],
        estimated_chars=estimated_chars,
        is_large=estimated_chars > LARGE_SOURCE_CHARS,
        content_hash=content_hash,
        metadata=metadata,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Chunking
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ContentChunk:
    """A single chunk of source content for processing."""
    index: int
    total: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


def chunk_content(
    content: str,
    *,
    chunk_size: int = MAX_CHUNK_CHARS,
    max_chunks: int = MAX_CHUNKS,
) -> list[ContentChunk]:
    """Split content into processable chunks.

    Strategy:
    - Split on paragraph boundaries (double newlines) first.
    - If paragraphs are too large, split on single newlines.
    - Preserves logical boundaries where possible.
    """
    if not content or len(content) <= chunk_size:
        return [ContentChunk(index=0, total=1, content=content)]

    # Split on paragraph boundaries
    paragraphs = re.split(r"\n\n+", content)
    chunks: list[ContentChunk] = []
    current_chunk = ""

    for para in paragraphs:
        # If a single paragraph exceeds chunk size, split it further
        if len(para) > chunk_size:
            # Flush current chunk
            if current_chunk.strip():
                chunks.append(ContentChunk(
                    index=len(chunks), total=0,  # Will update later
                    content=current_chunk.strip(),
                ))
                current_chunk = ""

            # Split large paragraph on line boundaries
            lines = para.split("\n")
            for line in lines:
                if len(current_chunk) + len(line) + 1 > chunk_size:
                    if current_chunk.strip():
                        chunks.append(ContentChunk(
                            index=len(chunks), total=0,
                            content=current_chunk.strip(),
                        ))
                    current_chunk = line + "\n"
                else:
                    current_chunk += line + "\n"
        elif len(current_chunk) + len(para) + 2 > chunk_size:
            # Flush and start new chunk
            if current_chunk.strip():
                chunks.append(ContentChunk(
                    index=len(chunks), total=0,
                    content=current_chunk.strip(),
                ))
            current_chunk = para + "\n\n"
        else:
            current_chunk += para + "\n\n"

    # Don't forget the last chunk
    if current_chunk.strip():
        chunks.append(ContentChunk(
            index=len(chunks), total=0,
            content=current_chunk.strip(),
        ))

    # Enforce max chunks limit
    if len(chunks) > max_chunks:
        # Merge trailing chunks into the last slot
        merged_content = "\n\n".join(c.content for c in chunks[max_chunks - 1:])
        chunks = chunks[: max_chunks - 1]
        chunks.append(ContentChunk(
            index=len(chunks), total=0,
            content=merged_content[:chunk_size * 2],  # Allow some overflow for final
        ))

    # Fix total counts
    total = len(chunks)
    chunks = [
        ContentChunk(index=c.index, total=total, content=c.content, metadata=c.metadata)
        for c in chunks
    ]

    return chunks


# ─────────────────────────────────────────────────────────────────────────────
# Deduplication
# ─────────────────────────────────────────────────────────────────────────────


class DeduplicationResult(StrEnum):
    """Outcome of deduplication check."""
    NO_MATCH = "no_match"            # No existing skill matches → create proposal
    EXACT_MATCH = "exact_match"      # Content hash matches → skip
    IMPROVEMENT = "improvement"       # Similar exists but new content is better → patch proposal


@dataclass(frozen=True, slots=True)
class DeduplicationCheck:
    """Result of checking a learned candidate against the registry."""
    result: DeduplicationResult
    existing_skill_id: str | None = None
    existing_skill_name: str | None = None
    similarity_score: float = 0.0
    reason: str = ""


def check_deduplication(
    registry: SkillRegistry,
    store: Any,
    *,
    proposed_name: str,
    proposed_description: str,
    proposed_content: str,
    content_hash: str | None = None,
) -> DeduplicationCheck:
    """Check if a proposed skill duplicates or improves an existing one.

    Strategy:
    1. Exact name match → check content hash for identity
    2. FTS search → find similar skills
    3. Compare scope overlap

    Returns a DeduplicationCheck indicating whether to skip, create, or patch.
    """
    # 1. Exact name match
    existing = registry.get_skill_by_name(proposed_name)
    if existing:
        return DeduplicationCheck(
            result=DeduplicationResult.IMPROVEMENT,
            existing_skill_id=existing.skill_id,
            existing_skill_name=existing.name,
            similarity_score=1.0,
            reason=f"skill '{proposed_name}' already exists — treating as improvement",
        )

    # 2. FTS search for similar descriptions/names
    try:
        similar_results = _fts_search_similar(store, proposed_name, proposed_description)
    except Exception:
        similar_results = []

    if not similar_results:
        return DeduplicationCheck(
            result=DeduplicationResult.NO_MATCH,
            reason="no similar skills found in registry",
        )

    # 3. Check similarity of top result
    best = similar_results[0]
    # Use a simple token overlap heuristic
    overlap = _token_overlap(proposed_description, best.get("description", ""))

    if overlap > 0.8:
        return DeduplicationCheck(
            result=DeduplicationResult.IMPROVEMENT,
            existing_skill_id=best.get("id"),
            existing_skill_name=best.get("name"),
            similarity_score=overlap,
            reason=f"high overlap ({overlap:.2f}) with existing skill '{best.get('name')}'",
        )
    elif overlap > 0.5:
        # Borderline — still create new, but note the relationship
        return DeduplicationCheck(
            result=DeduplicationResult.NO_MATCH,
            existing_skill_name=best.get("name"),
            similarity_score=overlap,
            reason=f"moderate overlap ({overlap:.2f}) with '{best.get('name')}' — creating separate skill",
        )
    else:
        return DeduplicationCheck(
            result=DeduplicationResult.NO_MATCH,
            reason="no significant overlap with existing skills",
        )


# ─────────────────────────────────────────────────────────────────────────────
# LearnResult — the output of /learn processing
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SkillReference:
    """A reference document within a multi-file skill."""
    filename: str        # e.g., "networking.md"
    title: str
    content: str
    provenance: str = ""  # Source section/page that generated this reference


@dataclass(slots=True)
class LearnResult:
    """Complete result of /learn ingestion."""
    source_type: SourceType
    source_identity: str
    title: str
    summary: str

    # Skill output
    proposed_skill_name: str
    proposed_skill_content: str
    proposed_description: str
    references: list[SkillReference] = field(default_factory=list)

    # Deduplication outcome
    dedup_result: DeduplicationResult = DeduplicationResult.NO_MATCH
    existing_skill_id: str | None = None

    # Processing stats
    chunks_processed: int = 0
    content_hash: str = ""
    warnings: list[str] = field(default_factory=list)

    # Set after proposal creation
    proposal_id: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion Engine
# ─────────────────────────────────────────────────────────────────────────────


class IngestionEngine:
    """Processes /learn commands into SkillProposal objects.

    Architecture:
    1. Classify source
    2. Load/fetch content
    3. Chunk if large
    4. Synthesize into skill format
    5. Deduplicate
    6. Create proposal via SkillService

    The reviewer/synthesis step is a function parameter (injectable) so tests
    can supply a deterministic synthesizer.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        store: Any,
        home: Path,
        *,
        synthesizer: Any | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._home = home
        self._synthesizer = synthesizer

    def ingest(
        self,
        source: str,
        *,
        content: str | None = None,
        source_run_id: str | None = None,
        title_hint: str | None = None,
    ) -> LearnResult:
        """Process a /learn command.

        Args:
            source: The source identifier (URL, path, raw text, etc.)
            content: Pre-loaded content (if already available).
            source_run_id: The run ID that triggered this learn.
            title_hint: Optional title suggestion from the user.

        Returns:
            LearnResult with the ingestion outcome (may include proposal_id).
        """
        # Step 1: Classify
        classification = classify_source(source, content=content)
        actual_content = content or self._load_content(source, classification)

        if not actual_content:
            result = LearnResult(
                source_type=classification.source_type,
                source_identity=classification.source_identity,
                title="",
                summary="",
                proposed_skill_name="",
                proposed_skill_content="",
                proposed_description="",
                content_hash=classification.content_hash,
            )
            result.warnings.append("could not load content from source")
            return result

        # Update hash with actual content
        content_hash = hashlib.sha256(
            actual_content.encode("utf-8", errors="replace")
        ).hexdigest()

        # Step 2: Chunk if large
        if classification.is_large:
            chunks = chunk_content(actual_content)
        else:
            chunks = [ContentChunk(index=0, total=1, content=actual_content)]

        # Step 3: Synthesize (use injected synthesizer or default)
        synth = self._synthesizer or _default_synthesizer
        synthesis = synth(
            chunks=chunks,
            source_type=classification.source_type,
            title_hint=title_hint,
            source_identity=classification.source_identity,
        )

        proposed_name = synthesis.get("name", "")
        proposed_description = synthesis.get("description", "")
        proposed_content = synthesis.get("content", "")
        references = [
            SkillReference(
                filename=ref.get("filename", ""),
                title=ref.get("title", ""),
                content=ref.get("content", ""),
                provenance=ref.get("provenance", ""),
            )
            for ref in synthesis.get("references", [])
        ]

        if not proposed_name or not proposed_content:
            result = LearnResult(
                source_type=classification.source_type,
                source_identity=classification.source_identity,
                title=synthesis.get("title", ""),
                summary=synthesis.get("summary", ""),
                proposed_skill_name=proposed_name,
                proposed_skill_content=proposed_content,
                proposed_description=proposed_description,
                content_hash=content_hash,
                chunks_processed=len(chunks),
            )
            result.warnings.append("synthesis produced empty skill name or content")
            return result

        # Step 4: Deduplication
        dedup = check_deduplication(
            self._registry,
            self._store,
            proposed_name=proposed_name,
            proposed_description=proposed_description,
            proposed_content=proposed_content,
            content_hash=content_hash,
        )

        result = LearnResult(
            source_type=classification.source_type,
            source_identity=classification.source_identity,
            title=synthesis.get("title", proposed_name),
            summary=synthesis.get("summary", proposed_description),
            proposed_skill_name=proposed_name,
            proposed_skill_content=proposed_content,
            proposed_description=proposed_description,
            references=references,
            dedup_result=dedup.result,
            existing_skill_id=dedup.existing_skill_id,
            chunks_processed=len(chunks),
            content_hash=content_hash,
        )

        # Step 5: Create proposal (or patch) via registry
        if dedup.result == DeduplicationResult.EXACT_MATCH:
            result.warnings.append("exact duplicate — skipping proposal creation")
            return result

        proposal_id = self._create_proposal(result, dedup, source_run_id=source_run_id)
        result.proposal_id = proposal_id

        # Step 6: Write references if multi-file
        if references and proposal_id:
            self._stage_references(proposed_name, references)

        return result

    def _load_content(
        self, source: str, classification: SourceClassification,
    ) -> str | None:
        """Load content from the source based on its classification."""
        if classification.source_type == SourceType.LOCAL_FILE:
            path = Path(classification.metadata.get("path", source))
            if path.is_file():
                try:
                    return path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
        elif classification.source_type == SourceType.PLAIN_TEXT:
            return source
        elif classification.source_type == SourceType.CONVERSATION:
            return source
        elif classification.source_type in (
            SourceType.DIRECTORY, SourceType.REPOSITORY, SourceType.DOCUMENTATION_TREE,
        ):
            path = Path(classification.metadata.get("path", source))
            return _read_directory_content(path)
        # For WEB_PAGE and PDF, content must be pre-fetched
        return None

    def _create_proposal(
        self,
        result: LearnResult,
        dedup: DeduplicationCheck,
        *,
        source_run_id: str | None = None,
    ) -> str | None:
        """Create a SkillProposal via the registry."""
        try:
            if dedup.result == DeduplicationResult.IMPROVEMENT and dedup.existing_skill_id:
                # Patch proposal for existing skill
                from .discovery import read_skill_content
                current = read_skill_content(self._home, dedup.existing_skill_name or "")
                existing = self._registry.get_skill(dedup.existing_skill_id)

                proposal = self._registry.create_proposal(
                    skill_name=dedup.existing_skill_name or result.proposed_skill_name,
                    operation="patch",
                    description=result.proposed_description,
                    reason=f"/learn improvement from {result.source_type.value}: {result.source_identity[:100]}",
                    content=result.proposed_skill_content,
                    confidence=0.8,
                    skill_id=dedup.existing_skill_id,
                    before=current,
                    base_revision=existing.revision if existing else None,
                    source_run_id=source_run_id,
                    review_model="learn-ingestion",
                )
                return proposal.id
            else:
                # New skill proposal
                proposal = self._registry.create_proposal(
                    skill_name=result.proposed_skill_name,
                    operation="create",
                    description=result.proposed_description,
                    reason=f"/learn from {result.source_type.value}: {result.source_identity[:100]}",
                    content=result.proposed_skill_content,
                    confidence=0.8,
                    source_run_id=source_run_id,
                    review_model="learn-ingestion",
                )
                return proposal.id
        except Exception as exc:
            LOGGER.warning("/learn: failed to create proposal: %s", exc)
            result.warnings.append(f"proposal creation failed: {exc}")
            return None

    def _stage_references(self, skill_name: str, references: list[SkillReference]) -> None:
        """Write reference files to a staging area for the skill.

        These become part of the skill directory upon approval.
        The references are stored alongside the skill even before approval
        so the proposal approval can incorporate them.
        """
        refs_dir = self._home / "skills" / ".staged" / skill_name / "references"
        refs_dir.mkdir(parents=True, exist_ok=True)
        for ref in references:
            if ref.filename and ref.content:
                ref_path = refs_dir / ref.filename
                try:
                    ref_path.write_text(ref.content, encoding="utf-8")
                except OSError as exc:
                    LOGGER.warning("failed to stage reference %s: %s", ref.filename, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Default synthesizer (deterministic, no LLM)
# ─────────────────────────────────────────────────────────────────────────────


def _default_synthesizer(
    chunks: list[ContentChunk],
    source_type: SourceType,
    title_hint: str | None = None,
    source_identity: str = "",
) -> dict[str, Any]:
    """Deterministic synthesizer for testing and simple sources.

    In production, this would be replaced by an LLM-backed synthesizer.
    Extracts structure from the content and formats as SKILL.md.
    """
    full_content = "\n\n".join(c.content for c in chunks)

    # Extract title from first heading or use hint
    title = title_hint or _extract_title(full_content) or _derive_name(source_identity)
    name = _normalize_name(title)
    description = _extract_first_paragraph(full_content)[:200]

    # Build SKILL.md content
    skill_lines = [
        f"# {title}",
        "",
        description,
        "",
    ]

    # For large sources, split into a main summary + references
    references: list[dict[str, Any]] = []
    if len(chunks) > 1:
        skill_lines.append("## Overview")
        skill_lines.append("")
        # Use first chunk as overview
        skill_lines.append(chunks[0].content[:2000])
        skill_lines.append("")
        skill_lines.append("## References")
        skill_lines.append("")

        # Create references from remaining chunks
        for i, chunk in enumerate(chunks[1:], start=1):
            ref_name = f"section-{i:02d}.md"
            skill_lines.append(f"- See `references/{ref_name}`")
            references.append({
                "filename": ref_name,
                "title": f"Section {i}",
                "content": chunk.content,
                "provenance": f"chunk {i} of {len(chunks)}",
            })
        skill_lines.append("")
    else:
        # Single chunk: inline everything
        skill_lines.append("## Procedure")
        skill_lines.append("")
        skill_lines.append(full_content[:8000])
        skill_lines.append("")

    # Source provenance
    skill_lines.append(f"## Source")
    skill_lines.append("")
    skill_lines.append(f"- Type: {source_type.value}")
    skill_lines.append(f"- Identity: {source_identity[:200]}")
    skill_lines.append("")

    return {
        "name": name,
        "title": title,
        "description": description,
        "summary": f"Learned from {source_type.value}: {title}",
        "content": "\n".join(skill_lines),
        "references": references,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────────────────────────────────────


def _looks_like_conversation(text: str) -> bool:
    """Check if text looks like a conversation transcript."""
    role_patterns = (r"^(User|Assistant|Human|AI|System):", r"^\[.*?\]:")
    lines = text.split("\n", 20)
    role_count = sum(
        1 for line in lines
        if any(re.match(pat, line.strip(), re.IGNORECASE) for pat in role_patterns)
    )
    return role_count >= 2


def _looks_like_docs_tree(path: Path) -> bool:
    """Check if a directory looks like a documentation tree."""
    doc_markers = ("docs", "documentation", "wiki", "guide")
    if any(m in path.name.lower() for m in doc_markers):
        return True
    # Check for common doc structure
    md_files = list(path.glob("*.md"))
    return len(md_files) >= 3


def _estimate_directory_size(path: Path) -> int:
    """Estimate total text content size in a directory."""
    total = 0
    text_exts = {".md", ".txt", ".rst", ".py", ".js", ".ts", ".toml", ".yaml", ".yml", ".json"}
    for f in path.rglob("*"):
        if f.is_file() and f.suffix.lower() in text_exts:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        if total > 1_000_000:  # Cap estimation
            break
    return total


def _read_directory_content(path: Path) -> str | None:
    """Read concatenated content from a directory (text files only)."""
    if not path.is_dir():
        return None
    text_exts = {".md", ".txt", ".rst"}
    parts: list[str] = []
    total_chars = 0
    for f in sorted(path.rglob("*")):
        if f.is_file() and f.suffix.lower() in text_exts:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                parts.append(f"# {f.relative_to(path)}\n\n{content}")
                total_chars += len(content)
                if total_chars > MAX_CHUNK_CHARS * MAX_CHUNKS:
                    break
            except OSError:
                pass
    return "\n\n---\n\n".join(parts) if parts else None


def _extract_title(content: str) -> str:
    """Extract title from first markdown heading."""
    for line in content.split("\n", 10):
        match = re.match(r"^#\s+(.+)$", line.strip())
        if match:
            return match.group(1).strip()
    return ""


def _extract_first_paragraph(content: str) -> str:
    """Extract first non-empty, non-heading paragraph."""
    lines = content.split("\n")
    past_heading = False
    para_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            if para_lines:
                break
            past_heading = True
            continue
        if not stripped:
            if para_lines:
                break
            continue
        if past_heading or not stripped.startswith("#"):
            para_lines.append(stripped)
    return " ".join(para_lines)


def _derive_name(source_identity: str) -> str:
    """Derive a skill name from the source identity."""
    # URL: use domain + path
    url_match = re.match(r"https?://([^/]+)(/[^?#]*)?", source_identity)
    if url_match:
        domain = url_match.group(1).split(".")[-2] if "." in url_match.group(1) else url_match.group(1)
        path_part = (url_match.group(2) or "").strip("/").replace("/", "-")[:30]
        return f"{domain}-{path_part}" if path_part else domain

    # File path: use filename without extension
    path = Path(source_identity)
    return path.stem if path.stem else "learned-skill"


def _normalize_name(title: str) -> str:
    """Normalize a title into a kebab-case skill name."""
    # Remove special characters, lowercase, replace spaces/underscores with hyphens
    name = re.sub(r"[^\w\s-]", "", title.lower())
    name = re.sub(r"[\s_]+", "-", name.strip())
    name = re.sub(r"-+", "-", name)  # Collapse multiple hyphens
    name = name.strip("-")
    # Ensure it starts with a letter
    if name and not name[0].isalpha():
        name = "skill-" + name
    return name[:60] or "learned-skill"


def _fts_search_similar(
    store: Any,
    name: str,
    description: str,
) -> list[dict[str, Any]]:
    """Search for similar skills using FTS."""
    import re as _re
    search_text = f"{name} {description}"
    terms = _re.findall(r"[^\W_]+", search_text, flags=_re.UNICODE)
    if not terms:
        return []

    # Build FTS query
    fts_expr = " OR ".join(
        f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms[:10]
    )

    try:
        with store._connect() as conn:
            rows = conn.execute(
                """SELECT s.id, s.name, s.description, bm25(skills_fts) AS rank
                   FROM skills_fts f
                   JOIN learned_skills s ON s.name = f.name
                   WHERE skills_fts MATCH ?
                   AND s.state = 'active'
                   ORDER BY rank
                   LIMIT 5""",
                (fts_expr,),
            ).fetchall()
        return [
            {"id": row["id"], "name": row["name"], "description": row["description"]}
            for row in rows
        ]
    except Exception:
        return []


def _token_overlap(text_a: str, text_b: str) -> float:
    """Compute token overlap ratio between two texts."""
    tokens_a = set(re.findall(r"\w+", text_a.lower()))
    tokens_b = set(re.findall(r"\w+", text_b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union) if union else 0.0
