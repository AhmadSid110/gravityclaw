import { useEffect, useRef, useState } from "react";
import { getAgyQuota, getConversationEffort, getConversationModel, getModels, setConversationEffort, setConversationModel } from "./api";
import type { AgyQuota, ConversationEffort, ConversationModel, ModelCatalog } from "./types";

const EFFORT_LABELS: Record<string, string> = { low: "Low", medium: "Medium", high: "High" };
const EFFORT_ICONS: Record<string, string> = { low: "\u26A1", medium: "\u2696\uFE0F", high: "\uD83E\uDDE0" };

export function ModelSelector({ conversationId, onChanged }: { conversationId: string; onChanged?: (model: ConversationModel) => void }) {
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null);
  const [policy, setPolicy] = useState<ConversationModel | null>(null);
  const [effort, setEffort] = useState<ConversationEffort | null>(null);
  const [quota, setQuota] = useState<AgyQuota | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});

  useEffect(() => {
    let cancelled = false;
    setError(null);
    void Promise.all([
      getModels(),
      getConversationModel(conversationId),
      getConversationEffort(conversationId),
      getAgyQuota(),
    ]).then(([models, current, currentEffort, quotaData]) => {
      if (cancelled) return;
      setCatalog(models);
      setPolicy(current);
      setEffort(currentEffort);
      setQuota(quotaData);
    }).catch((reason) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "Unable to load models");
    });
    return () => { cancelled = true; };
  }, [conversationId]);

  async function changeModel(value: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const next = await setConversationModel(conversationId, value || null);
      setPolicy(next);
      setNotice("Applies to the next run");
      onChanged?.(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to change model");
    } finally {
      setBusy(false);
    }
  }

  async function changeEffort(value: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const next = await setConversationEffort(conversationId, value || null);
      setEffort(next);
      setNotice("Effort updated");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to change effort");
    } finally {
      setBusy(false);
    }
  }

  // Derive the active model display
  const selected = policy?.model_policy === "explicit" ? policy.requested_model || "" : "";
  const activeLabel = selected
    ? _findLabel(selected, quota, catalog)
    : "AGY Default";
  const effortLevels = catalog?.effort_levels ?? ["low", "medium", "high"];
  const currentEffort = effort?.effort || "medium";

  // Build model options from quota pools (grouped by provider)
  const modelGroups = _buildModelGroups(quota, catalog);

  const triggerRef = useRef<HTMLButtonElement>(null);

  function updatePosition() {
    const btn = triggerRef.current;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    const panelWidth = Math.min(290, window.innerWidth - 24);

    // Anchor to the right edge of trigger button so it opens inward into the conversation pane
    let left = rect.right - panelWidth;

    // Constrain within conversation-main pane if present to never overlap the sidebar/side panels
    const mainPane = btn.closest(".conversation-main");
    if (mainPane) {
      const mainRect = mainPane.getBoundingClientRect();
      if (left < mainRect.left + 8) {
        left = mainRect.left + 8;
      }
      if (left + panelWidth > mainRect.right - 8) {
        left = mainRect.right - panelWidth - 8;
      }
    }

    // Safety viewport clamping
    if (left + panelWidth > window.innerWidth - 12) {
      left = window.innerWidth - panelWidth - 12;
    }
    if (left < 12) left = 12;

    const top = rect.bottom + 6;
    setDropdownStyle({ top: `${top}px`, left: `${left}px` });
  }

  function togglePanel() {
    if (!expanded) {
      updatePosition();
    }
    setExpanded(!expanded);
  }

  useEffect(() => {
    if (!expanded) return;
    function handleResizeOrScroll() {
      updatePosition();
    }
    window.addEventListener("resize", handleResizeOrScroll);
    window.addEventListener("scroll", handleResizeOrScroll, true);
    return () => {
      window.removeEventListener("resize", handleResizeOrScroll);
      window.removeEventListener("scroll", handleResizeOrScroll, true);
    };
  }, [expanded]);

  return <div className="model-panel">
    {/* Compact trigger pill */}
    <button
      ref={triggerRef}
      className="model-panel-trigger"
      onClick={togglePanel}
      title={error ?? `Model: ${activeLabel} (${EFFORT_LABELS[currentEffort]} reasoning)`}
      aria-expanded={expanded}
    >
      <span className="model-panel-icon">{"\u25C6"}</span>
      <span className="model-panel-model">{activeLabel}</span>
      <span className="model-panel-chevron">{expanded ? "\u25B4" : "\u25BE"}</span>
    </button>

    {/* Expanded dropdown panel */}
    {expanded && <div className="model-panel-dropdown" style={dropdownStyle}>
      {/* Model Selection */}
      <div className="model-panel-section">
        <div className="model-panel-section-label">ANTIGRAVITY MODEL</div>
        <select
          className="model-panel-select"
          value={selected}
          onChange={(e) => void changeModel(e.target.value)}
          disabled={busy || !catalog}
        >
          <option value="">AGY Default</option>
          {modelGroups.map((group) => (
            <optgroup key={group.provider} label={group.provider}>
              {group.models.map((m) => (
                <option key={m.id} value={m.id}>{m.label}</option>
              ))}
            </optgroup>
          ))}
          {/* Show the current selection if it's not in the pool list */}
          {selected && !modelGroups.some((g) => g.models.some((m) => m.id === selected)) && (
            <option value={selected}>{_findLabel(selected, quota, catalog)}</option>
          )}
        </select>
      </div>

      {/* Reasoning Effort */}
      <div className="model-panel-section">
        <div className="model-panel-section-label">REASONING EFFORT</div>
        <div className="model-panel-effort-row">
          {effortLevels.map((level) => (
            <button
              key={level}
              className={`effort-chip ${currentEffort === level ? "effort-active" : ""}`}
              onClick={() => void changeEffort(level)}
              disabled={busy}
              title={`${EFFORT_LABELS[level]} reasoning effort`}
            >
              <span className="effort-chip-icon">{EFFORT_ICONS[level]}</span>
              <span className="effort-chip-label">{EFFORT_LABELS[level]}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Quota Section */}
      <div className="model-panel-section model-panel-quota">
        <div className="model-panel-section-label">
          QUOTA
          {quota?.plan && <span className="quota-plan-badge">{quota.plan.name}</span>}
        </div>
        {quota && quota.available ? <>
          {quota.pools && quota.pools.map((pool) => (
            <div key={pool.pool} className="quota-pool">
              <div className="quota-pool-header">
                <span className="quota-pool-name">{pool.pool}</span>
                <span className={`quota-pool-percent ${_quotaClass(pool.remaining_percent)}`}>
                  {pool.remaining_percent !== null ? `${pool.remaining_percent}%` : "\u2014"}
                </span>
              </div>
              {pool.remaining_percent !== null && <div className="quota-bar-container">
                <div
                  className={`quota-bar-fill ${_quotaClass(pool.remaining_percent)}`}
                  style={{ width: `${pool.remaining_percent}%` }}
                />
              </div>}
              {pool.reset_time && <div className="quota-reset-time">
                Resets {_formatResetTime(pool.reset_time)}
              </div>}
            </div>
          ))}
          {quota.plan && (quota.plan.available_prompt_credits != null || quota.plan.available_flow_credits != null) && (
            <div className="quota-credits">
              {quota.plan.available_prompt_credits != null && (
                <span className="quota-credit-item">
                  <strong>{quota.plan.available_prompt_credits.toLocaleString()}</strong> prompt credits
                </span>
              )}
              {quota.plan.available_flow_credits != null && (
                <span className="quota-credit-item">
                  <strong>{quota.plan.available_flow_credits.toLocaleString()}</strong> flow credits
                </span>
              )}
            </div>
          )}
        </> : quota && !quota.available ? (
          <div className="quota-unavailable">{quota.error || "Quota data unavailable"}</div>
        ) : (
          <div className="quota-loading">Loading quota...</div>
        )}
      </div>

      {notice && !busy && <div className="model-panel-notice">{notice}</div>}
      {busy && <div className="model-panel-notice">Saving...</div>}
      {error && <div className="model-panel-error">{error}</div>}
    </div>}

    {/* Click-away backdrop */}
    {expanded && <div className="model-panel-backdrop" onClick={() => setExpanded(false)} />}
  </div>;
}

interface ModelGroup {
  provider: string;
  models: Array<{ id: string; label: string }>;
}

function _buildModelGroups(quota: AgyQuota | null, catalog: ModelCatalog | null): ModelGroup[] {
  // Prefer models from quota (live AGY models)
  if (quota?.available && quota.pools && quota.pools.length > 0) {
    return quota.pools.map((pool) => ({
      provider: pool.pool,
      models: pool.models,
    }));
  }

  // Fallback to catalog models (without grouping)
  if (catalog?.models && catalog.models.length > 0) {
    return [{ provider: "Models", models: catalog.models.map((m) => ({ id: m.id, label: m.label })) }];
  }

  return [];
}

function _findLabel(modelId: string, quota: AgyQuota | null, catalog: ModelCatalog | null): string {
  // Check quota models first
  if (quota?.models) {
    const found = quota.models.find((m) => m.id === modelId);
    if (found) return found.label;
  }
  // Check catalog
  if (catalog?.models) {
    const found = catalog.models.find((m) => m.id === modelId);
    if (found) return found.label;
  }
  return modelId;
}



function _quotaClass(percent: number | null): string {
  if (percent === null) return "";
  if (percent <= 20) return "quota-critical";
  if (percent <= 50) return "quota-warning";
  return "quota-healthy";
}

function _formatResetTime(iso: string): string {
  const reset = new Date(iso);
  const now = new Date();
  const diffMs = reset.getTime() - now.getTime();

  if (diffMs <= 0) return "soon";

  const hours = Math.floor(diffMs / 3_600_000);
  const minutes = Math.floor((diffMs % 3_600_000) / 60_000);

  if (hours > 0) return `in ${hours}h ${minutes}m`;
  return `in ${minutes}m`;
}
