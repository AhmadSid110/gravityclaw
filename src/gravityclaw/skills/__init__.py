"""GravityClaw Skills — Phase 2/2.5/3 procedural learning, runtime, and autonomy.

Provides skill discovery, registry, proposals, approval transactions,
revision history, usage telemetry, runtime (skill_view, skill_manage,
two-stage discovery, prompt integration, RunSkillContext), trust/autonomy
engine, deterministic curator, /learn ingestion, and causal attribution.
"""

from .models import (
    SkillOwner,
    SkillRecord,
    SkillRevisionRecord,
    SkillProposal,
    SkillState,
    SkillTrust,
    SkillUsageEvent,
)
from .runtime import (
    LoadedSkill,
    PromptIntegration,
    RunSkillContext,
    SkillCandidate,
    SkillDiscovery,
    SkillManageError,
    SkillManageResult,
    SkillViewError,
    SkillViewResult,
    skill_manage,
    skill_view,
)
from .service import SkillService
from .trust import (
    OperationContext,
    OperationKind,
    PolicyResult,
    TrustDecision,
    TrustMode,
    TrustPolicy,
)
from .curator import (
    Curator,
    CuratorConfig,
    CuratorReport,
    SkillUtility,
    compute_utility,
)
from .ingestion import (
    ContentChunk,
    DeduplicationCheck,
    DeduplicationResult,
    IngestionEngine,
    LearnResult,
    SkillReference,
    SourceClassification,
    SourceType,
    chunk_content,
    classify_source,
    check_deduplication,
)
from .attribution import (
    AttributionReport,
    SkillOutcome,
    build_attribution,
    enrich_reviewer_context,
)

__all__ = [
    # Phase 2 core
    "LoadedSkill",
    "PromptIntegration",
    "RunSkillContext",
    "SkillCandidate",
    "SkillDiscovery",
    "SkillManageError",
    "SkillManageResult",
    "SkillOwner",
    "SkillRecord",
    "SkillRevisionRecord",
    "SkillProposal",
    "SkillService",
    "SkillState",
    "SkillTrust",
    "SkillUsageEvent",
    "SkillViewError",
    "SkillViewResult",
    "skill_manage",
    "skill_view",
    # Phase 3: Trust/Autonomy
    "OperationContext",
    "OperationKind",
    "PolicyResult",
    "TrustDecision",
    "TrustMode",
    "TrustPolicy",
    # Phase 3: Curator
    "Curator",
    "CuratorConfig",
    "CuratorReport",
    "SkillUtility",
    "compute_utility",
    # Phase 3: Ingestion
    "ContentChunk",
    "DeduplicationCheck",
    "DeduplicationResult",
    "IngestionEngine",
    "LearnResult",
    "SkillReference",
    "SourceClassification",
    "SourceType",
    "chunk_content",
    "classify_source",
    "check_deduplication",
    # Phase 3: Attribution
    "AttributionReport",
    "SkillOutcome",
    "build_attribution",
    "enrich_reviewer_context",
]
