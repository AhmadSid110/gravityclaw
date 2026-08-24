import React, { useEffect, useMemo, useState, useRef, Component } from "react";
import {
  archiveConversation,
  attachmentDownloadUrl,
  cancelRun,
  createConversation,
  deleteConversationPermanent,
  getArchivedConversations,
  getConversation,
  getRuns,
  getSession,
  getTimeline,
  getWorkspaces,
  login,
  logout,
  restoreConversation,
  searchConversations,
  submitRun,
  updateConversation,
  uploadAttachment,
} from "./api";
import { ContextCircle, ContextInspector } from "./ContextInspector";
import { ContextStatus } from "./ContextStatus";
import { ModelSelector } from "./ModelSelector";
import { presentationForRun, useControlReplay } from "./replay";
import { MarkdownMessage } from "./MarkdownMessage";
import { RichRunInspector } from "./RunInspector";
import { MemoryStudio } from "./MemoryStudio";
import { ContextStudio } from "./ContextStudio";
import { AutomationsStudio } from "./AutomationsStudio";
import { TaskFlowStudio } from "./TaskFlowStudio";
import { TaskFlowHomeCard } from "./TaskFlowHomeCard";
import { CapabilitiesStudio } from "./CapabilitiesStudio";
import { LearningStudio } from "./LearningStudio";
import { SettingsPage } from "./SettingsPage";
import { useAppearance } from "./theme";
import { SlashCommandMenu, SlashCommand, SLASH_COMMANDS } from "./SlashCommandMenu";
import type { AttachmentRecord, Conversation, ConversationDetail, ConversationSearchResult, ControlState, Message, NormalizedActivity, PersistedEvent, PresentationState, RunRecord } from "./types";

const navItems = [
  ["home", "⌂", "Home"],
  ["conversations", "◌", "Conversations"],
  ["runs", "ϟ", "Runs"],
  ["memory", "✦", "Memory"],
  ["context", "◎", "Context"],
  ["learning", "◈", "Learning"],
  ["workspaces", "◇", "Workspaces"],
  ["automations", "◷", "Automations"],
  ["capabilities", "⊙", "Capabilities"],
  ["channels", "◉", "Channels"],
] as const;

type View = typeof navItems[number][0] | "taskflow" | "usage" | "settings";

function App() {
  const [authenticated, setAuthenticated] = useState<boolean | null>(null);
  const [authError, setAuthError] = useState<string | null>(null);
  useEffect(() => {
    void getSession().then((result) => setAuthenticated(result.authenticated)).catch(() => setAuthenticated(false));
  }, []);

  if (authenticated === null) return <div className="boot-screen"><div className="brand-mark">✦</div><span>Connecting to GravityClaw…</span></div>;
  if (!authenticated) return <Login onSuccess={() => setAuthenticated(true)} error={authError} setError={setAuthError} />;
  return <Console onLogout={async () => { await logout(); setAuthenticated(false); }} />;
}

function Login({ onSuccess, error, setError }: { onSuccess: () => void; error: string | null; setError: (value: string | null) => void }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try { await login(token); onSuccess(); } catch (reason) { setError(reason instanceof Error ? reason.message : "Authentication failed"); }
    finally { setBusy(false); }
  }
  return <main className="login-screen">
    <div className="login-card">
      <div className="brand-lockup"><div className="brand-mark">✦</div><div><strong>GravityClaw</strong><span>Control Center</span></div></div>
      <div className="eyebrow">PRIVATE CONTROL PLANE</div>
      <h1>Welcome back.</h1>
      <p className="muted">Enter the control token to create a secure browser session. It will never be stored in local browser storage.</p>
      <form onSubmit={submit} className="stack">
        <label className="field-label" htmlFor="control-token">Control token</label>
        <input id="control-token" className="text-input" type="password" autoComplete="off" value={token} onChange={(event) => setToken(event.target.value)} placeholder="Paste token" autoFocus />
        {error && <div className="inline-error" role="alert">{error}</div>}
        <button className="primary-button" disabled={busy || !token}>{busy ? "Authenticating…" : "Open control center"}<span>↗</span></button>
      </form>
      <div className="secure-note"><span>⌁</span> Session secured with an HttpOnly cookie</div>
    </div>
  </main>;
}

function formatSessionTitle(title?: string | null, id?: string): string {
  if (title && title.trim()) {
    let t = title.trim();
    if (t.startsWith("agent:main:") || t.startsWith("agent:") || t.startsWith("conv_")) {
      t = t
        .replace(/^agent:[^:]+:/, "")
        .replace(/^agent:/, "")
        .replace(/^conv_/, "")
        .replace(/[-_]/g, " ");
      return t.charAt(0).toUpperCase() + t.slice(1);
    }
    return t;
  }
  if (id) {
    if (id.includes("agent:") || id.startsWith("conv_")) {
      const t = id
        .replace(/^agent:[^:]+:/, "")
        .replace(/^conv_/, "")
        .replace(/[-_]/g, " ");
      return t.charAt(0).toUpperCase() + t.slice(1);
    }
    return `Session ${id.slice(0, 8)}`;
  }
  return "New session";
}

function AppSidebar({
  view,
  setView,
  conversations,
  selectedConvId,
  onSelectConv,
  onNewSession,
  onOpenAllSessions,
  pinnedIds,
  onTogglePin,
  activeRuns,
  connection,
  mobileOpen,
  onCloseMobile,
  onRenameSession,
  onArchiveSession,
  archivedCount,
  onOpenArchivedChats,
}: {
  view: View;
  setView: (view: View) => void;
  conversations: Conversation[];
  selectedConvId: string | null;
  onSelectConv: (id: string) => void;
  onNewSession: () => void;
  onOpenAllSessions: () => void;
  pinnedIds: string[];
  onTogglePin: (id: string, e?: React.MouseEvent) => void;
  activeRuns: RunRecord[];
  connection: string;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onRenameSession: (convId: string, title: string) => Promise<void>;
  onArchiveSession: (convId: string) => Promise<void>;
  archivedCount: number;
  onOpenArchivedChats: () => void;
}) {
  const [moreOpen, setMoreOpen] = useState(false);
  const [activeMenuId, setActiveMenuId] = useState<string | null>(null);
  const [renamingSessionId, setRenamingSessionId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  // Close menus when mobile drawer closes
  useEffect(() => {
    if (!mobileOpen) {
      setActiveMenuId(null);
      setMoreOpen(false);
      setRenamingSessionId(null);
    }
  }, [mobileOpen]);

  const pinned = conversations.filter((c) => pinnedIds.includes(c.id));
  const recent = conversations.filter((c) => !pinnedIds.includes(c.id));

  const handleSelect = (id: string) => {
    onSelectConv(id);
    setView("conversations");
    onCloseMobile();
  };

  const handleStartRename = (conv: Conversation, e?: React.MouseEvent) => {
    if (e) e.stopPropagation();
    setActiveMenuId(null);
    setRenamingSessionId(conv.id);
    setRenameDraft(conv.title || "");
  };

  const handleSaveRenameSubmit = async (convId: string, e: React.FormEvent) => {
    e.preventDefault();
    const title = renameDraft.trim();
    if (!title) return;
    try {
      await onRenameSession(convId, title);
      setRenamingSessionId(null);
    } catch (err) {
      console.error("Failed to rename session:", err);
    }
  };

  const renderSessionRow = (conv: Conversation, isPinned: boolean) => {
    const isSelected = view === "conversations" && selectedConvId === conv.id;
    const isRunning = activeRuns.some((r) => r.conversation_id === conv.id && r.status === "running");
    const menuOpen = activeMenuId === conv.id;
    const isRenaming = renamingSessionId === conv.id;

    if (isRenaming) {
      return (
        <div key={conv.id} className="sidebar-session-item is-renaming">
          <form
            onSubmit={(e) => void handleSaveRenameSubmit(conv.id, e)}
            className="sidebar-inline-rename-form"
            onClick={(e) => e.stopPropagation()}
          >
            <input
              type="text"
              autoFocus
              className="sidebar-rename-input"
              value={renameDraft}
              onChange={(e) => setRenameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Escape") setRenamingSessionId(null);
              }}
            />
            <button type="submit" className="sidebar-rename-submit-btn" title="Save title">✓</button>
            <button type="button" className="sidebar-rename-cancel-btn" onClick={() => setRenamingSessionId(null)} title="Cancel">✕</button>
          </form>
        </div>
      );
    }

    return (
      <div key={conv.id} className={`sidebar-session-item ${isSelected ? "active" : ""}`}>
        <button
          type="button"
          className="sidebar-session-btn"
          onClick={() => handleSelect(conv.id)}
          title={conv.title || conv.id}
        >
          <span className="sidebar-session-title">
            {formatSessionTitle(conv.title, conv.id)}
          </span>
          {isRunning && <span className="status-dot blue pulsing" />}
        </button>

        <div className="sidebar-session-actions">
          {isPinned && <span className="sidebar-pin-icon" title="Pinned session">📌</span>}
          <button
            type="button"
            className="sidebar-dots-btn"
            onClick={(e) => {
              e.stopPropagation();
              setActiveMenuId(menuOpen ? null : conv.id);
            }}
            title="Session options"
            aria-label="Session options"
          >
            ⋯
          </button>

          {menuOpen && (
            <div className="sidebar-popover-menu" onClick={(e) => e.stopPropagation()}>
              <button
                type="button"
                className="sidebar-popover-item"
                onClick={(e) => {
                  onTogglePin(conv.id, e);
                  setActiveMenuId(null);
                }}
              >
                <span>📌</span> {isPinned ? "Unpin session" : "Pin session"}
              </button>
              <button
                type="button"
                className="sidebar-popover-item"
                onClick={(e) => handleStartRename(conv, e)}
              >
                <span>✎</span> Rename
              </button>
              <button
                type="button"
                className="sidebar-popover-item"
                onClick={() => {
                  setActiveMenuId(null);
                  void onArchiveSession(conv.id);
                }}
              >
                <span>📦</span> Archive
              </button>
              <button
                type="button"
                className="sidebar-popover-item"
                onClick={() => {
                  setActiveMenuId(null);
                  void navigator.clipboard.writeText(conv.id);
                }}
              >
                <span>📋</span> Copy ID
              </button>
            </div>
          )}
        </div>
      </div>
    );
  };

  return (
    <aside className={`sidebar ${mobileOpen ? "open" : ""}`}>
      {/* ═══ ZONE 1: FIXED TOP NAVIGATION ═══ */}
      <div className="sidebar-zone-top">
        {/* Brand */}
        <div className="sidebar-brand-row">
          <div className="brand-mark">✦</div>
          <strong>GravityClaw</strong>
        </div>

        {/* Primary Navigation Entries */}
        <button
          type="button"
          className={`sidebar-nav-entry ${view === "home" ? "active" : ""}`}
          onClick={() => { setView("home"); onCloseMobile(); }}
        >
          <span className="sidebar-entry-icon">⌁</span>
          <span>Overview</span>
        </button>

        <button
          type="button"
          className={`sidebar-nav-entry ${view === "taskflow" ? "active" : ""}`}
          onClick={() => { setView("taskflow"); onCloseMobile(); }}
        >
          <span className="sidebar-entry-icon">⚡</span>
          <span>TaskFlow</span>
        </button>

        {/* Prominent New Session Action */}
        <button
          type="button"
          className="sidebar-new-session-action"
          onClick={() => {
            onNewSession();
            onCloseMobile();
          }}
        >
          <span className="plus">＋</span>
          <span>New session</span>
        </button>
      </div>

      <div className="sidebar-divider" />

      {/* ═══ ZONE 2: SCROLLABLE SESSIONS LIST ═══ */}
      <div className="sidebar-sessions-container">
        <div className="sidebar-sessions-bar">
          <span className="sessions-heading">SESSIONS</span>
          <div className="sessions-bar-right">
            <span className="workspace-pill">Main ▾</span>
            <button
              type="button"
              className="sessions-icon-btn"
              onClick={() => {
                onOpenAllSessions();
                onCloseMobile();
              }}
              title="All sessions"
              aria-label="All sessions"
            >
              ≡
            </button>
          </div>
        </div>

        <div className="sidebar-sessions-scroll">
          {pinned.length > 0 && (
            <div className="sidebar-session-subgroup">
              {pinned.map((conv) => renderSessionRow(conv, true))}
            </div>
          )}

          <div className="sidebar-session-subgroup">
            {recent.map((conv) => renderSessionRow(conv, false))}
          </div>

          {conversations.length === 0 && (
            <div className="sidebar-empty-sessions">No active sessions</div>
          )}
        </div>

        {/* Action Footers: All sessions + Archived sessions */}
        <div className="sidebar-sessions-footer-actions">
          <button
            type="button"
            className="sidebar-all-sessions-footer-btn"
            onClick={() => {
              onOpenAllSessions();
              onCloseMobile();
            }}
          >
            <span>All sessions</span>
            <span className="chevron-right">›</span>
          </button>

          <button
            type="button"
            className="sidebar-archived-footer-btn"
            onClick={() => {
              onOpenArchivedChats();
              onCloseMobile();
            }}
            title="View and restore archived sessions"
          >
            <span className="arch-left">
              <span className="archive-icon">📦</span>
              <span>Archived ({archivedCount})</span>
            </span>
            <span className="chevron-right">›</span>
          </button>
        </div>
      </div>

      <div className="sidebar-divider" />

      {/* ═══ ZONE 3: FIXED BOTTOM NAVIGATION & TOOLS ═══ */}
      <div className="sidebar-zone-footer">
        <div className="sidebar-more-section">
          <button
            type="button"
            className={`sidebar-more-toggle ${moreOpen ? "open" : ""}`}
            onClick={() => setMoreOpen(!moreOpen)}
            aria-expanded={moreOpen}
          >
            <span>MORE</span>
            <span className="more-arrow">{moreOpen ? "⌄" : "›"}</span>
          </button>

          {moreOpen && (
            <div className="sidebar-more-list fade-in">
              <button type="button" className={`sidebar-sub-entry ${view === "automations" ? "active" : ""}`} onClick={() => { setView("automations"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">⚡</span>
                <span>Automations</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "memory" ? "active" : ""}`} onClick={() => { setView("memory"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">🧠</span>
                <span>Memory</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "capabilities" ? "active" : ""}`} onClick={() => { setView("capabilities"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">◇</span>
                <span>Skills</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "learning" ? "active" : ""}`} onClick={() => { setView("learning"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">✦</span>
                <span>Learning</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "capabilities" ? "active" : ""}`} onClick={() => { setView("capabilities"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">⌘</span>
                <span>Capabilities</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "context" ? "active" : ""}`} onClick={() => { setView("context"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">🔌</span>
                <span>Integrations</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "workspaces" ? "active" : ""}`} onClick={() => { setView("workspaces"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">◫</span>
                <span>Workspaces</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "runs" ? "active" : ""}`} onClick={() => { setView("runs"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">◎</span>
                <span>Models</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "channels" ? "active" : ""}`} onClick={() => { setView("channels"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">◉</span>
                <span>Channels</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "runs" ? "active" : ""}`} onClick={() => { setView("runs"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">▤</span>
                <span>Logs</span>
              </button>
              <button type="button" className={`sidebar-sub-entry ${view === "settings" ? "active" : ""}`} onClick={() => { setView("settings"); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">⚙</span>
                <span>Settings</span>
              </button>
              <button type="button" className="sidebar-sub-entry" onClick={() => { onOpenArchivedChats(); onCloseMobile(); }}>
                <span className="sidebar-entry-icon">📦</span>
                <span>Archived ({archivedCount})</span>
              </button>
            </div>
          )}
        </div>

        {/* Minimal Connection Dot */}
        <div className="sidebar-conn-footer">
          <div className="sidebar-conn-dot-only" title={connection === "connected" ? "Connected" : "Disconnected / Error"}>
            <span className={`status-dot ${connection === "connected" ? "green" : "red"}`} />
          </div>
        </div>
      </div>
    </aside>
  );
}

function Console({ onLogout: _onLogout }: { onLogout: () => Promise<void> }) {
  const [view, setView] = useState<View>(() => {
    try {
      const rawHash = window.location.hash.replace(/^#\/?/, "").split("?")[0];
      const baseHash = rawHash.split("/")[0];
      if (baseHash && (navItems.some(([id]) => id === baseHash) || baseHash === "taskflow" || baseHash === "settings" || baseHash === "usage")) {
        return baseHash as View;
      }
      const saved = localStorage.getItem("gravityclaw:active-view");
      if (saved && (navItems.some(([id]) => id === saved) || saved === "taskflow" || saved === "settings" || saved === "usage")) {
        return saved as View;
      }
    } catch {}
    return "home";
  });

  const [selectedConvId, setSelectedConvId] = useState<string | null>(() => {
    try {
      const rawHash = window.location.hash.replace(/^#\/?/, "").split("?")[0];
      const parts = rawHash.split("/");
      if ((parts[0] === "conversations" || parts[0] === "chat") && parts[1]) {
        return parts[1];
      }
      const saved = localStorage.getItem("gravityclaw:active-conversation-id");
      return saved || null;
    } catch {
      return null;
    }
  });

  // Sync view and selected conversation to URL hash and localStorage
  useEffect(() => {
    try {
      localStorage.setItem("gravityclaw:active-view", view);
      if (selectedConvId) {
        localStorage.setItem("gravityclaw:active-conversation-id", selectedConvId);
      }
      const rawHash = window.location.hash.replace(/^#\/?/, "").split("?")[0];
      const expectedHash = view === "conversations" && selectedConvId ? `conversations/${selectedConvId}` : view;
      if (rawHash !== expectedHash && rawHash !== view) {
        window.history.replaceState(null, "", `#${expectedHash}`);
      }
    } catch {}
  }, [view, selectedConvId]);

  // Listen to browser forward/back button navigation (hashchange)
  useEffect(() => {
    const handleHashChange = () => {
      const raw = window.location.hash.replace(/^#\/?/, "").split("?")[0];
      const [targetView, targetConvId] = raw.split("/");
      if (targetView && (navItems.some(([id]) => id === targetView) || targetView === "taskflow" || targetView === "settings" || targetView === "usage")) {
        setView(targetView as View);
        if (targetConvId) {
          setSelectedConvId(targetConvId);
          try {
            localStorage.setItem("gravityclaw:active-conversation-id", targetConvId);
          } catch {}
          window.dispatchEvent(new CustomEvent("gravityclaw:select-chat", { detail: { id: targetConvId } }));
        }
      }
    };
    window.addEventListener("hashchange", handleHashChange);
    return () => window.removeEventListener("hashchange", handleHashChange);
  }, []);

  const [mobileNav, setMobileNav] = useState(false);
  const [selectedRun, setSelectedRun] = useState<RunRecord | null>(null);
  const [contextInspectorRunId, setContextInspectorRunId] = useState<string | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [archivedList, setArchivedList] = useState<Conversation[]>([]);
  const [archivedModalOpen, setArchivedModalOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [pinnedIds, setPinnedIds] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem("gravityclaw:pinned-sessions");
      return raw ? JSON.parse(raw) : [];
    } catch {
      return [];
    }
  });

  const state = useControlReplay();
  useAppearance(); // Reactively syncs theme, font, and density with localStorage and data attributes
  const title = navItems.find(([id]) => id === view)?.[2] ?? (view === "usage" ? "Usage" : view === "settings" ? "Settings" : "Home");

  const showToast = (message: string) => {
    setToast(message);
    setTimeout(() => setToast((curr) => (curr === message ? null : curr)), 3200);
  };

  const syncConversations = async () => {
    try {
      const all = await getArchivedConversations();
      const active = all.filter((c) => !c.archived_at);
      const archived = all.filter((c) => !!c.archived_at);
      setConversations(active);
      setArchivedList(archived);

      const fromUrl = (() => {
        try {
          const parts = window.location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
          if ((parts[0] === "conversations" || parts[0] === "chat") && parts[1]) return parts[1];
        } catch {}
        return null;
      })();
      const savedId = localStorage.getItem("gravityclaw:active-conversation-id");

      const targetId = (fromUrl && active.some((c) => c.id === fromUrl) ? fromUrl : null)
        || (selectedConvId && active.some((c) => c.id === selectedConvId) ? selectedConvId : null)
        || (savedId && active.some((c) => c.id === savedId) ? savedId : null)
        || active[0]?.id
        || null;

      if (targetId) {
        setSelectedConvId(targetId);
        try {
          localStorage.setItem("gravityclaw:active-conversation-id", targetId);
        } catch {}
      }
      return { active, archived };
    } catch {
      return { active: [], archived: [] };
    }
  };

  useEffect(() => {
    void syncConversations();
  }, []);

  useEffect(() => {
    const handleConversationsUpdated = (e: any) => {
      if (e.detail && Array.isArray(e.detail)) {
        setConversations(e.detail);
      }
    };
    window.addEventListener("gravityclaw:conversations-updated" as any, handleConversationsUpdated);
    return () => window.removeEventListener("gravityclaw:conversations-updated" as any, handleConversationsUpdated);
  }, []);

  const togglePin = (convId: string, e?: React.MouseEvent) => {
    if (e) e.stopPropagation();
    setPinnedIds((prev) => {
      const next = prev.includes(convId) ? prev.filter((id) => id !== convId) : [...prev, convId];
      try {
        localStorage.setItem("gravityclaw:pinned-sessions", JSON.stringify(next));
      } catch {}
      return next;
    });
  };

  const handleGlobalRenameSession = async (convId: string, newTitle: string) => {
    try {
      const updated = await updateConversation(convId, { title: newTitle });
      setConversations((prev) => prev.map((c) => (c.id === convId ? { ...c, title: updated.title } : c)));
      const cached = conversationDetailCache.get(convId);
      if (cached) {
        cached.conversation.title = updated.title;
        conversationDetailCache.set(convId, { ...cached });
      }
      showToast("✓ Session renamed");
      window.dispatchEvent(new CustomEvent("gravityclaw:conversations-updated", {
        detail: conversations.map((c) => (c.id === convId ? { ...c, title: updated.title } : c))
      }));
    } catch (err) {
      showToast("✕ Failed to rename session");
      throw err;
    }
  };

  const handleGlobalArchiveSession = async (convId: string) => {
    try {
      await archiveConversation(convId);
      const { active } = await syncConversations();
      showToast("✓ Session moved to Archived chats");
      if (selectedConvId === convId) {
        const nextId = active[0]?.id ?? null;
        setSelectedConvId(nextId);
        if (nextId) {
          window.dispatchEvent(new CustomEvent("gravityclaw:select-chat", { detail: { id: nextId } }));
        }
      }
      window.dispatchEvent(new CustomEvent("gravityclaw:conversations-updated", { detail: active }));
    } catch {
      showToast("✕ Failed to archive session");
    }
  };

  const handleRestoreFromArchived = async (convId: string) => {
    try {
      await restoreConversation(convId);
      await syncConversations();
      setArchivedModalOpen(false);
      setSelectedConvId(convId);
      setView("conversations");
      showToast("✓ Session restored");
      window.dispatchEvent(new CustomEvent("gravityclaw:select-chat", { detail: { id: convId } }));
    } catch {
      showToast("✕ Failed to restore session");
    }
  };

  const handleDeletePermanent = async (conv: Conversation) => {
    try {
      await deleteConversationPermanent(conv.id);
      await syncConversations();
      showToast("✓ Session permanently deleted");
    } catch {
      showToast("✕ Failed to delete session");
    }
  };

  const handleOpenAllSessions = () => {
    setView("conversations");
    window.dispatchEvent(new CustomEvent("gravityclaw:search-chats"));
  };

  const handleNewSession = async () => {
    try {
      const workspaces = await getWorkspaces();
      if (!workspaces[0]) {
        showToast("✕ No workspace found");
        return;
      }
      const countNormal = conversations.filter((c) => c.kind !== "main").length;
      const title = countNormal > 0 ? `New chat ${countNormal + 1}` : "New chat";
      const conversation = await createConversation(workspaces[0].id, title, "normal");
      setSelectedConvId(conversation.id);
      setView("conversations");
      try {
        localStorage.setItem("gravityclaw:active-conversation-id", conversation.id);
      } catch {}
      await syncConversations();
      showToast("✓ New chat created");
      window.dispatchEvent(new CustomEvent("gravityclaw:select-chat", { detail: { id: conversation.id } }));
    } catch (err) {
      console.error("Failed to create conversation:", err);
      showToast("✕ Failed to create new session");
    }
  };

  // Compute the latest active run for the context circle
  const latestActiveRun = state.activeRuns.find((r) => r.status === "running") ?? state.activeRuns[0] ?? null;
  const contextRatio = latestActiveRun ? 0.42 : 0;

  return <div className="app">
    {toast && <div className="global-toast" role="status">{toast}</div>}
    <AppSidebar
      view={view}
      setView={setView}
      conversations={conversations}
      selectedConvId={selectedConvId}
      onSelectConv={(id) => {
        setSelectedConvId(id);
        window.dispatchEvent(new CustomEvent("gravityclaw:select-chat", { detail: { id } }));
      }}
      onNewSession={handleNewSession}
      onOpenAllSessions={handleOpenAllSessions}
      pinnedIds={pinnedIds}
      onTogglePin={togglePin}
      activeRuns={state.activeRuns}
      connection={state.connection}
      mobileOpen={mobileNav}
      onCloseMobile={() => setMobileNav(false)}
      onRenameSession={handleGlobalRenameSession}
      onArchiveSession={handleGlobalArchiveSession}
      archivedCount={archivedList.length}
      onOpenArchivedChats={() => setArchivedModalOpen(true)}
    />
    {mobileNav && <button className="scrim" aria-label="Close navigation" onClick={() => setMobileNav(false)} />}
    <main className="main-shell">
      {view !== "conversations" && (
        <header className="topbar">
          <button className="mobile-menu" onClick={() => setMobileNav(true)} aria-label="Open navigation">☰</button>
          <div className="breadcrumb"><span>GravityClaw</span><span className="crumb-separator">/</span><strong>{title}</strong></div>
          <div className="topbar-actions">
            {latestActiveRun && <ContextCircle ratio={contextRatio} running={latestActiveRun.status === "running"} onClick={() => setContextInspectorRunId(latestActiveRun.id)} />}
            <span className="system-health" role="status">
              <span className={`status-dot ${state.connection === "connected" ? "green" : "amber"}`} />
              {state.connection === "connected" ? "Connected" : state.connection}
            </span>
            <button
              className={`icon-button ${view === "settings" ? "active" : ""}`}
              onClick={() => setView("settings")}
              title="Settings & Appearance"
              aria-label="Open settings"
            >
              ⚙
            </button>
          </div>
        </header>
      )}
      {state.connection !== "connected" && <div className="connection-banner" role="status" aria-live="polite">{state.connection === "reconnecting" ? "Connection interrupted. GravityClaw is still running on the server. Reconnecting…" : state.connection === "offline" ? "Control plane unavailable. Retrying…" : "Connecting to GravityClaw…"}</div>}
      <div className={`content-shell ${view === "conversations" ? "chat-fullscreen" : ""}`}>
        {view === "home" && <Home state={state} onRunSelect={setSelectedRun} onOpenRuns={() => setView("runs")} onOpenTaskFlow={() => setView("taskflow")} />}
        {view === "taskflow" && <TaskFlowStudio onOpenContextInspector={setContextInspectorRunId} />}
        {view === "conversations" && <ConversationWorkspace state={state} onRunSelect={setSelectedRun} onOpenMobileNav={() => setMobileNav(true)} selectedConvId={selectedConvId} onSelectConv={setSelectedConvId} />}
        {view === "runs" && <Runs state={state} onRunSelect={setSelectedRun} />}
        {view === "memory" && <MemoryStudio />}
        {view === "context" && <ContextStudio />}
        {view === "automations" && <AutomationsStudio />}
        {view === "capabilities" && <CapabilitiesStudio />}
        {view === "learning" && <LearningStudio />}
        {view === "settings" && <SettingsPage state={state} />}
        {view !== "home" && view !== "runs" && view !== "conversations" && view !== "memory" && view !== "context" && view !== "automations" && view !== "capabilities" && view !== "learning" && view !== "settings" && view !== "taskflow" && <ComingSoon title={title} />}
      </div>
      {selectedRun && <RichRunInspector run={selectedRun} state={state} onClose={() => setSelectedRun(null)} />}
      {contextInspectorRunId && <ContextInspector runId={contextInspectorRunId} runStatus={latestActiveRun?.status} onClose={() => setContextInspectorRunId(null)} onOpenSkill={(_skillId) => { setContextInspectorRunId(null); setView("learning"); }} onOpenMemory={(_memoryId) => { setContextInspectorRunId(null); setView("learning"); }} onOpenJourney={(_skillId) => { setContextInspectorRunId(null); setView("learning"); }} />}
      {archivedModalOpen && (
        <ArchivedChatsModal
          archived={archivedList}
          onRestore={(id) => void handleRestoreFromArchived(id)}
          onDeletePermanent={(conv) => void handleDeletePermanent(conv)}
          onClose={() => setArchivedModalOpen(false)}
        />
      )}
    </main>
  </div>;
}

function Home({ state, onRunSelect, onOpenRuns, onOpenTaskFlow }: { state: ControlState; onRunSelect: (run: RunRecord) => void; onOpenRuns: () => void; onOpenTaskFlow: () => void }) {
  const snapshot = state.snapshot;
  const active = state.activeRuns.filter((run) => run.status === "running");
  const queued = state.activeRuns.filter((run) => run.status === "queued");

  const now = new Date();
  const dateStr = now.toLocaleDateString(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  }).toUpperCase();
  const hour = now.getHours();
  const greeting = hour < 12 ? "Good morning" : hour < 18 ? "Good afternoon" : "Good evening";

  return (
    <div className="page fade-in home-page">
      <div className="page-heading">
        <div>
          <div className="eyebrow">{dateStr}</div>
          <h1>{greeting}, Ahmad <span className="wave">✦</span></h1>
          <p className="muted">GravityClaw is active and keeping watch.</p>
        </div>
        <div className="live-pill">
          <span className={`status-dot ${state.connection === "connected" ? "green" : "amber"}`} />
          {state.connection === "connected" ? "Live" : "Reconnecting"}
        </div>
      </div>

      <section className="stat-grid">
        <StatCard
          label="Active runs"
          value={String(active.length)}
          detail={active.length ? `${active.length} task${active.length > 1 ? "s" : ""} in progress` : "Nothing running"}
          accent="blue"
        />
        <StatCard
          label="Queued"
          value={String(queued.length)}
          detail={queued.length ? "Waiting for conversation lock" : "Queue is clear"}
          accent="violet"
        />
        <StatCard
          label="Next heartbeat"
          value={nextHeartbeat(snapshot)}
          detail="Autonomous check"
          accent="amber"
        />
      </section>

      <TaskFlowHomeCard onOpenTaskFlow={onOpenTaskFlow} />

      <div className="content-grid">
        <section className="panel active-panel">
          <PanelHeader title="Active now" link={active.length ? "View all runs" : undefined} onLink={onOpenRuns} />
          {active.length === 0 && (
            <EmptyState
              icon="◌"
              title="GravityClaw is quiet"
              detail="Start a conversation or trigger a TaskFlow when you have work to execute."
            />
          )}
          {active.map((run) => <RunRow key={run.id} run={run} onClick={() => onRunSelect(run)} />)}
        </section>

        <section className="panel">
          <PanelHeader title="System Status" />
          <div className="schedule-row">
            <span className="schedule-icon">◷</span>
            <div>
              <strong>Autonomous Heartbeat</strong>
              <span>Evaluates memory curator, dream consolidation, and triggers</span>
            </div>
            <span className="schedule-state">Active</span>
          </div>
          <div className="schedule-row">
            <span className="schedule-icon subdued">◌</span>
            <div>
              <strong>Control Plane</strong>
              <span>FastAPI · Tailscale Secure Mesh</span>
            </div>
            <span className="schedule-state">{state.connection === "connected" ? "Online" : state.connection}</span>
          </div>
        </section>
      </div>

      <section className="panel activity-panel">
        <PanelHeader title="Recent activity" link="Open runs" onLink={onOpenRuns} />
        <div className="activity-list">
          {state.activity.length === 0 ? (
            <div className="sidebar-empty-sessions">No recent activity</div>
          ) : (
            state.activity.slice(-8).reverse().map((event) => <ActivityRow key={event.id} event={event} />)
          )}
        </div>
      </section>
    </div>
  );
}

function Runs({ state, onRunSelect }: { state: ControlState; onRunSelect: (run: RunRecord) => void }) {
  const [runs, setRuns] = useState<RunRecord[]>(state.activeRuns);
  useEffect(() => {
    let cancelled = false;
    void getRuns().then((items) => { if (!cancelled) setRuns(items); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, []);
  useEffect(() => {
    setRuns((current) => {
      const byId = new Map(current.map((run) => [run.id, run]));
      for (const active of state.activeRuns) byId.set(active.id, active);
      return [...byId.values()].sort((left, right) => right.created_at.localeCompare(left.created_at));
    });
  }, [state.activeRuns]);
  return <div className="page fade-in"><div className="page-heading"><div><div className="eyebrow">OPERATIONS</div><h1>Runs</h1><p className="muted">Every execution, one durable timeline.</p></div><span className="history-count" role="status">{runs.length} {runs.length === 1 ? "run" : "runs"}</span></div><div className="filter-bar"><button className="filter active">All <span>⌄</span></button><button className="filter">Workspace <span>⌄</span></button><button className="filter">State <span>⌄</span></button><span className="filter-count">{runs.length} visible</span></div><section className="panel runs-table" aria-label="Execution history"><div className="table-head"><span>STATE</span><span>TASK</span><span>WORKSPACE</span><span>VERSION</span><span>TIME</span></div>{runs.length === 0 && <EmptyState icon="ϟ" title="No runs yet" detail="Your execution history will appear here." />}{runs.map((run) => <button className="table-row" key={run.id} onClick={() => onRunSelect(run)}><span><StatusBadge status={run.status} /></span><span className="task-cell"><strong>{String(run.request.prompt ?? "Untitled run")}</strong><small>{run.id.slice(0, 8)} · {run.request.context_profile ?? "chat"}</small></span><span className="workspace-cell">gravityclaw</span><span className="mono">v{run.version}</span><span className="muted-text">{formatTime(run.created_at)}</span></button>)}</section></div>;
}





// In-memory cache for instant conversation switching & restoration without loading flicker
const conversationDetailCache = new Map<string, ConversationDetail>();
let cachedConversationsList: Conversation[] = [];

function ConversationWorkspace({
  state,
  onRunSelect,
  onOpenMobileNav,
  selectedConvId,
  onSelectConv,
}: {
  state: ControlState;
  onRunSelect: (run: RunRecord) => void;
  onOpenMobileNav?: () => void;
  selectedConvId?: string | null;
  onSelectConv?: (id: string) => void;
}) {
  const [conversations, setConversations] = useState<Conversation[]>(() => cachedConversationsList);
  const [archivedList, setArchivedList] = useState<Conversation[]>([]);
  const [selectedId, setSelectedIdState] = useState<string | null>(() => {
    const fromUrl = (() => {
      try {
        const parts = window.location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
        if ((parts[0] === "conversations" || parts[0] === "chat") && parts[1]) return parts[1];
      } catch {}
      return null;
    })();
    return selectedConvId || fromUrl || localStorage.getItem("gravityclaw:active-conversation-id") || null;
  });

  const setSelectedId = (id: string | null) => {
    setSelectedIdState(id);
    if (id) {
      try {
        localStorage.setItem("gravityclaw:active-conversation-id", id);
      } catch {}
      if (onSelectConv) onSelectConv(id);
    }
  };

  useEffect(() => {
    if (selectedConvId && selectedConvId !== selectedId) {
      setSelectedIdState(selectedConvId);
    }
  }, [selectedConvId]);

  useEffect(() => {
    const handleSelectChat = (e: any) => {
      if (e.detail?.id) {
        setSelectedIdState(e.detail.id);
        try {
          localStorage.setItem("gravityclaw:active-conversation-id", e.detail.id);
        } catch {}
      }
    };
    window.addEventListener("gravityclaw:select-chat" as any, handleSelectChat);
    return () => window.removeEventListener("gravityclaw:select-chat" as any, handleSelectChat);
  }, []);

  useEffect(() => {
    const handleNewChat = () => {
      void newConversation();
    };
    window.addEventListener("gravityclaw:new-chat" as any, handleNewChat);
    return () => window.removeEventListener("gravityclaw:new-chat" as any, handleNewChat);
  }, [conversations]);
  const [detail, setDetail] = useState<ConversationDetail | null>(() => {
    const savedId = localStorage.getItem("gravityclaw:active-conversation-id");
    return savedId ? conversationDetailCache.get(savedId) || null : null;
  });
  const [timeline, setTimeline] = useState<PersistedEvent[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState<boolean>(() => {
    const savedId = localStorage.getItem("gravityclaw:active-conversation-id");
    return savedId ? !conversationDetailCache.has(savedId) : false;
  });
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingFiles, setPendingFiles] = useState<Array<{ file: File; id?: string; state: "uploading" | "ready" | "failed" }>>([]);

  // Multi-chat & Modals state
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const [archivedModalOpen, setArchivedModalOpen] = useState(false);
  const [deleteConfirmConv, setDeleteConfirmConv] = useState<Conversation | null>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const [headerMenuOpen, setHeaderMenuOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);

  // Slash Commands state
  const [slashMenuOpen, setSlashMenuOpen] = useState(false);
  const [slashSelectedIndex, setSlashSelectedIndex] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      const scrollH = textareaRef.current.scrollHeight;
      textareaRef.current.style.height = `${Math.min(Math.max(scrollH, 32), 240)}px`;
    }
  }, [draft]);

  const handleExecuteSlashCommand = (cmd: SlashCommand) => {
    if (cmd.actionType === "insert" && cmd.insertText) {
      setDraft(cmd.insertText);
      setSlashMenuOpen(false);
      setTimeout(() => {
        if (textareaRef.current) {
          textareaRef.current.focus();
          textareaRef.current.selectionStart = textareaRef.current.selectionEnd = cmd.insertText?.length ?? 0;
        }
      }, 50);
    } else if (cmd.actionType === "execute") {
      setDraft("");
      setSlashMenuOpen(false);
      if (cmd.id === "search") {
        setSearchOpen(true);
      } else if (cmd.id === "inspect") {
        setInspectorOpen((prev) => !prev);
      } else if (cmd.id === "new") {
        void newConversation();
      } else if (cmd.id === "archive" && detail) {
        void handleArchive(detail.conversation.id);
      } else if (cmd.id === "clear") {
        setDraft("");
      }
      setTimeout(() => textareaRef.current?.focus(), 50);
    }
  };

  const showToast = (message: string) => {
    setToast(message);
    setTimeout(() => setToast((curr) => (curr === message ? null : curr)), 3200);
  };

  const loadConversations = async () => {
    try {
      const all = await getArchivedConversations();
      const active = all.filter((c) => !c.archived_at);
      const archived = all.filter((c) => !!c.archived_at);
      cachedConversationsList = active;
      setConversations(active);
      setArchivedList(archived);
      window.dispatchEvent(new CustomEvent("gravityclaw:conversations-updated", { detail: active }));
      return active;
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to load conversations");
      return [];
    }
  };

  useEffect(() => {
    let cancelled = false;
    void loadConversations().then((active) => {
      if (cancelled) return;
      const fromUrl = (() => {
        try {
          const parts = window.location.hash.replace(/^#\/?/, "").split("?")[0].split("/");
          if ((parts[0] === "conversations" || parts[0] === "chat") && parts[1]) return parts[1];
        } catch {}
        return null;
      })();
      const savedId = localStorage.getItem("gravityclaw:active-conversation-id");
      const matched = (fromUrl && active.some((c) => c.id === fromUrl) ? fromUrl : null)
        || (selectedId && active.some((c) => c.id === selectedId) ? selectedId : null)
        || (selectedConvId && active.some((c) => c.id === selectedConvId) ? selectedConvId : null)
        || (savedId && active.some((c) => c.id === savedId) ? savedId : null);
      const targetId = matched ?? active[0]?.id ?? null;
      if (targetId && targetId !== selectedId) {
        setSelectedId(targetId);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(null);
      setTimeline([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    const cached = conversationDetailCache.get(selectedId);
    if (cached) {
      setDetail(cached);
      setLoading(false);
    } else {
      setLoading(true);
    }
    void getConversation(selectedId)
      .then((value) => {
        if (!cancelled) {
          conversationDetailCache.set(selectedId, value);
          setDetail(value);
          setLoading(false);
        }
      })
      .catch((reason) => {
        if (!cancelled) {
          setError(reason instanceof Error ? reason.message : "Unable to open conversation");
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const currentRuns = useMemo(() => {
    if (!detail) return [];
    const activeMap = new Map(state.activeRuns.map((r) => [r.id, r]));
    return detail.runs.map((r) => activeMap.get(r.id) ?? r);
  }, [detail, state.activeRuns]);

  const activeRun = useMemo(
    () => currentRuns.slice().reverse().find((item) => item.status === "running" || item.status === "queued") ?? null,
    [currentRuns]
  );
  const latestRun = useMemo(() => currentRuns.at(-1) ?? null, [currentRuns]);
  const run = activeRun ?? latestRun;

  const hasAuthoritativeAssistantMessage = useMemo(() => {
    if (!latestRun || !detail) return false;
    return detail.messages.some((m) => m.source_run_id === latestRun.id && m.role === "assistant");
  }, [detail?.messages, latestRun?.id]);

  useEffect(() => {
    if (!run) {
      setTimeline([]);
      return;
    }
    let cancelled = false;
    void getTimeline(run.id)
      .then((value) => {
        if (!cancelled) setTimeline(value.events);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [run?.id]);

  const selectedIdRef = useRef(selectedId);
  selectedIdRef.current = selectedId;

  useEffect(() => {
    if (!detail || !selectedId) return;
    const currentConvId = detail.conversation.id;
    const relevant = state.activity.some(
      (event) =>
        currentRuns.some((item) => item.id === event.run_id) &&
        ["run.completed", "run.failed", "run.cancelled", "run.interrupted", "agent.completed"].includes(event.event_type)
    );
    if (!relevant) return;
    const timer = window.setTimeout(() => {
      if (selectedIdRef.current !== currentConvId) return;
      void getConversation(currentConvId)
        .then((fresh) => {
          if (selectedIdRef.current === currentConvId) {
            setDetail(fresh);
          }
        })
        .catch(() => undefined);
    }, 80);
    return () => window.clearTimeout(timer);
  }, [state.activity, currentRuns]);

  // Close menus when clicking outside
  useEffect(() => {
    const handleGlobalClick = () => {
      setHeaderMenuOpen(false);
    };
    window.addEventListener("click", handleGlobalClick);
    return () => window.removeEventListener("click", handleGlobalClick);
  }, []);

  async function newConversation() {
    setError(null);
    try {
      const workspaces = await getWorkspaces();
      if (!workspaces[0]) throw new Error("Create a workspace before starting a conversation.");
      const countNormal = conversations.filter((c) => c.kind !== "main").length;
      const title = countNormal > 0 ? `New chat ${countNormal + 1}` : "New chat";
      const conversation = await createConversation(workspaces[0].id, title, "normal");
      setDetail(null);
      setTimeline([]);
      setDraft("");
      setSelectedId(conversation.id);
      await loadConversations();
      const fresh = await getConversation(conversation.id);
      setDetail(fresh);
      showToast("✓ New chat created");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to create conversation");
    }
  }

  async function handleArchive(conversationId: string, event?: React.MouseEvent) {
    if (event) event.stopPropagation();
    try {
      await archiveConversation(conversationId);
      const active = await loadConversations();
      if (selectedId === conversationId) {
        setSelectedId(active[0]?.id ?? null);
      }
      showToast("✓ Chat moved to archive");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to archive conversation");
    }
  }

  async function handleRestore(conversationId: string) {
    try {
      const restored = await restoreConversation(conversationId);
      await loadConversations();
      setSelectedId(restored.id);
      setArchivedModalOpen(false);
      showToast("✓ Chat restored to active");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to restore conversation");
    }
  }

  async function handleDeletePermanent(conversationId: string) {
    try {
      await deleteConversationPermanent(conversationId);
      const active = await loadConversations();
      if (selectedId === conversationId) {
        setSelectedId(active[0]?.id ?? null);
      }
      setDeleteConfirmConv(null);
      showToast("✓ Chat deleted permanently");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to delete conversation");
    }
  }

  async function handleSaveRename(conversationId: string, event: React.FormEvent) {
    event.preventDefault();
    const title = renameDraft.trim();
    if (!title) return;
    try {
      const updated = await updateConversation(conversationId, { title });
      const nextList = conversations.map((c) => (c.id === conversationId ? { ...c, title: updated.title } : c));
      setConversations(nextList);
      if (detail && detail.conversation.id === conversationId) {
        setDetail({ ...detail, conversation: { ...detail.conversation, title: updated.title } });
      }
      const cached = conversationDetailCache.get(conversationId);
      if (cached) {
        cached.conversation.title = updated.title;
        conversationDetailCache.set(conversationId, { ...cached });
      }
      setRenamingId(null);
      showToast("✓ Chat title updated");
      window.dispatchEvent(new CustomEvent("gravityclaw:conversations-updated", { detail: nextList }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to update title");
    }
  }

  function startRename(conv: Conversation, e?: React.MouseEvent) {
    if (e) e.stopPropagation();
    setRenamingId(conv.id);
    setRenameDraft(conv.title || "");
    setHeaderMenuOpen(false);
  }

  function copyChatId(convId: string, e?: React.MouseEvent) {
    if (e) e.stopPropagation();
    void navigator.clipboard.writeText(convId);
    showToast("✓ Copied Chat ID to clipboard");
    setHeaderMenuOpen(false);
  }

  async function send(customPrompt?: string) {
    const prompt = (customPrompt !== undefined ? customPrompt : draft).trim();
    if ((!prompt && pendingFiles.length === 0) || !detail || sending) return;
    if (pendingFiles.some((f) => f.state === "uploading")) return;
    setSending(true);
    setError(null);
    try {
      const readyIds = pendingFiles.filter((f) => f.state === "ready" && f.id).map((f) => f.id!);
      const effectivePrompt =
        prompt || (pendingFiles.length > 0 ? `[${pendingFiles.map((f) => f.file.name).join(", ")}]` : "");
      const submitted = await submitRun(detail.conversation.id, effectivePrompt, readyIds.length > 0 ? readyIds : undefined);
      const optimistic: Message = {
        id: `local:${submitted.id}`,
        conversation_id: detail.conversation.id,
        role: "user",
        content: effectivePrompt,
        created_at: new Date().toISOString(),
        source_run_id: submitted.id,
      };
      setDetail((current) =>
        current ? { ...current, messages: [...current.messages, optimistic], runs: [...current.runs, submitted] } : current
      );
      setDraft("");
      setPendingFiles([]);
      if (
        detail.conversation.kind === "normal" &&
        ((detail.conversation.title && detail.conversation.title.startsWith("New chat")) || !detail.conversation.title)
      ) {
        const autoTitle = effectivePrompt.slice(0, 36);
        setConversations((items) =>
          items.map((c) => (c.id === detail.conversation.id ? { ...c, title: autoTitle } : c))
        );
        void updateConversation(detail.conversation.id, { title: autoTitle }).catch(() => undefined);
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to send message");
    } finally {
      setSending(false);
    }
  }

  const [cancelingRunId, setCancelingRunId] = useState<string | null>(null);

  async function handleCancelRun(targetRun: RunRecord) {
    if (cancelingRunId) return;
    setCancelingRunId(targetRun.id);
    try {
      await cancelRun(targetRun);
      showToast("⏹ Run cancelled");
    } catch (err) {
      showToast(err instanceof Error ? `Failed to cancel: ${err.message}` : "Failed to cancel run");
    } finally {
      setCancelingRunId(null);
    }
  }

  async function handleFileSelect(files: FileList | null) {
    if (!files || files.length === 0 || !detail) return;
    const newFiles = Array.from(files).slice(0, 10 - pendingFiles.length);
    for (const file of newFiles) {
      const entry = { file, id: undefined as string | undefined, state: "uploading" as const };
      setPendingFiles((prev) => [...prev, entry]);
      try {
        const record = await uploadAttachment(detail.conversation.id, file);
        setPendingFiles((prev) =>
          prev.map((f) => (f.file === file ? { ...f, id: record.id, state: "ready" as const } : f))
        );
      } catch {
        setPendingFiles((prev) => (prev.map((f) => (f.file === file ? { ...f, state: "failed" as const } : f))));
      }
    }
  }

  function removeFile(file: File) {
    setPendingFiles((prev) => prev.filter((f) => f.file !== file));
  }

  const liveEvents = useMemo(
    () =>
      [...timeline, ...state.activity.filter((event) => event.run_id === run?.id)].filter(
        (event, index, all) => all.findIndex((candidate) => candidate.id === event.id) === index
      ),
    [timeline, state.activity, run?.id]
  );
  const presentation = useMemo(() => (run ? presentationForRun(run, liveEvents) : null), [run, liveEvents]);

  const showLiveBlock = Boolean(
    latestRun &&
    presentation &&
    !hasAuthoritativeAssistantMessage &&
    (
      activeRun !== null ||
      presentation.assistantText.trim().length > 0 ||
      presentation.currentTool !== null ||
      presentation.completedTools.length > 0 ||
      presentation.subagents.length > 0 ||
      latestRun.status === "queued" ||
      latestRun.status === "running"
    )
  );

  const isQueued = activeRun?.status === "queued";
  const isRunning = activeRun?.status === "running";
  const hasDraft = Boolean(draft.trim() || pendingFiles.length > 0);



  const messagesEndRef = useRef<HTMLDivElement>(null);
  const messageScrollRef = useRef<HTMLDivElement>(null);
  const isUserScrolledUp = useRef(false);

  const handleScroll = () => {
    if (!messageScrollRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = messageScrollRef.current;
    isUserScrolledUp.current = scrollHeight - scrollTop - clientHeight > 120;
  };

  const scrollToBottom = (behavior: ScrollBehavior = "smooth") => {
    if (isUserScrolledUp.current) return;
    if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior, block: "end" });
    } else if (messageScrollRef.current) {
      messageScrollRef.current.scrollTop = messageScrollRef.current.scrollHeight;
    }
  };

  useEffect(() => {
    scrollToBottom("smooth");
  }, [
    detail?.messages.length,
    presentation?.assistantText,
    presentation?.currentActivity?.id,
    presentation?.completedActivities.length,
    latestRun?.status,
  ]);

  useEffect(() => {
    isUserScrolledUp.current = false;
    scrollToBottom("auto");
  }, [selectedId]);

  const knownConv = conversations.find((c) => c.id === selectedId);
  const activeTitle =
    detail?.conversation.title ||
    knownConv?.title ||
    detail?.messages.find((item) => item.role === "user")?.content.slice(0, 34) ||
    (selectedId ? (selectedId.startsWith("agent:") ? "Main Agent" : "Chat") : "New chat");

  return (
    <div className="conversation-workspace">
      {/* TOAST NOTIFICATION */}
      {toast && <div className="toast-notification fade-in">{toast}</div>}

      {/* DELETE CONFIRMATION MODAL */}
      {deleteConfirmConv && (
        <div className="modal-backdrop" onClick={() => setDeleteConfirmConv(null)}>
          <div className="modal-dialog delete-dialog" onClick={(e) => e.stopPropagation()}>
            <div className="modal-header">
              <h3>Delete conversation?</h3>
              <button className="modal-close" onClick={() => setDeleteConfirmConv(null)}>✕</button>
            </div>
            <div className="modal-body">
              <p>
                Are you sure you want to permanently delete <strong>&quot;{deleteConfirmConv.title}&quot;</strong>?
              </p>
              <p className="muted-text">
                This will delete all messages, runs, and artifacts associated with this session. This action cannot be undone.
              </p>
            </div>
            <div className="modal-footer">
              <button className="secondary-button" onClick={() => setDeleteConfirmConv(null)}>
                Cancel
              </button>
              <button
                className="danger-button"
                onClick={() => void handleDeletePermanent(deleteConfirmConv.id)}
              >
                Delete permanently
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ARCHIVED CHATS MODAL */}
      {archivedModalOpen && (
        <ArchivedChatsModal
          archived={archivedList}
          onRestore={(id) => void handleRestore(id)}
          onDeletePermanent={(conv) => {
            setArchivedModalOpen(false);
            setDeleteConfirmConv(conv);
          }}
          onClose={() => setArchivedModalOpen(false)}
        />
      )}

      {/* CHAT PANE */}
      <section className="conversation-main" aria-busy={loading}>
        {/* COMPRESSED CONVERSATION HEADER */}
        <div className="conversation-header">
          {/* ROW 1: Toggle drawer + Title + Active Run chip + Menu ⋮ */}
          <div className="conversation-header-row header-primary-row">
            <div className="header-left">
              <button
                className="drawer-toggle-button"
                onClick={() => {
                  if (onOpenMobileNav) {
                    onOpenMobileNav();
                  }
                }}
                aria-label="Open sessions and navigation"
                title="Sessions & navigation"
              >
                ☰
              </button>
              <div className="title-lockup">
                {renamingId === detail?.conversation.id ? (
                  <form
                    onSubmit={(e) => void handleSaveRename(detail.conversation.id, e)}
                    className="inline-rename-form"
                  >
                    <input
                      type="text"
                      autoFocus
                      className="text-input rename-input"
                      value={renameDraft}
                      onChange={(e) => setRenameDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Escape") setRenamingId(null);
                      }}
                    />
                    <button type="submit" className="rename-submit-btn" title="Save title">✓</button>
                    <button type="button" className="rename-cancel-btn" onClick={() => setRenamingId(null)} title="Cancel">✕</button>
                  </form>
                ) : (
                  <div className="title-heading-row">
                    <h1
                      onClick={() => {
                        if (detail) startRename(detail.conversation);
                      }}
                      title="Click to rename"
                      className="editable-title"
                    >
                      {activeTitle}
                    </h1>
                    {detail && (
                      <button
                        type="button"
                        className="title-edit-trigger"
                        onClick={() => startRename(detail.conversation)}
                        title="Rename conversation"
                        aria-label="Rename conversation"
                      >
                        ✎
                      </button>
                    )}
                    
                  </div>
                )}
              </div>
            </div>

            <div className="header-right">
              {/* ⋮ MENU */}
              <div className="header-menu-container">
                <button
                  className="icon-button header-menu-trigger"
                  title="Chat options & tools"
                  onClick={(e) => {
                    e.stopPropagation();
                    setHeaderMenuOpen(!headerMenuOpen);
                  }}
                >
                  ⋮
                </button>
                {headerMenuOpen && (
                  <div className="header-dropdown-menu" onClick={(e) => e.stopPropagation()}>
                    <button onClick={() => { setHeaderMenuOpen(false); void newConversation(); }}>
                      <span>＋</span> New chat
                    </button>
                    <button onClick={() => { setHeaderMenuOpen(false); setInspectorOpen(!inspectorOpen); }}>
                      <span>◎</span> {inspectorOpen ? "Close Inspector" : "Inspect Run"}
                    </button>
                    {run && (
                      <button onClick={() => { setHeaderMenuOpen(false); onRunSelect(run); }}>
                        <span>⚡</span> Full Trace
                      </button>
                    )}
                    {detail && (
                      <>
                        <button onClick={(e) => startRename(detail.conversation, e)}>
                          <span>✎</span> Rename conversation
                        </button>
                        <button onClick={(e) => void handleArchive(detail.conversation.id, e)}>
                          <span>📦</span> Archive conversation
                        </button>
                        <button
                          className="danger-item"
                          onClick={(e) => {
                            e.stopPropagation();
                            setHeaderMenuOpen(false);
                            setDeleteConfirmConv(detail.conversation);
                          }}
                        >
                          <span>🗑</span> Delete permanently
                        </button>
                        <button onClick={(e) => copyChatId(detail.conversation.id, e)}>
                          <span>📋</span> Copy Chat ID
                        </button>
                      </>
                    )}
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>

        {error && <div className="inline-error workspace-error" role="alert">{error}</div>}

        {/* State 1: No conversation selected */}
        {!selectedId && !loading && conversations.length > 0 && (
          <EmptyState icon="◌" title="Choose a conversation" detail="Select a chat from the sidebar or create a new one." />
        )}

        {/* State 2: Welcome empty state when no chats exist */}
        {!selectedId && !loading && conversations.length === 0 && (
          <EmptyState icon="✦" title="Welcome to GravityClaw" detail="Start a conversation with the New chat button." />
        )}

        {/* State 3: Skeleton Loader while existing conversation hydrates */}
        {selectedId && loading && !detail && (
          <div className="message-scroll">
            <div className="message-list message-skeleton-list">
              <div className="message-skeleton user-skeleton">
                <div className="skeleton-line full" />
                <div className="skeleton-line short" />
              </div>
              <div className="message-skeleton assistant-skeleton">
                <div className="skeleton-line full" />
                <div className="skeleton-line mid" />
                <div className="skeleton-line short" />
              </div>
              <div className="message-skeleton assistant-skeleton">
                <div className="skeleton-line full" />
                <div className="skeleton-line mid" />
              </div>
            </div>
          </div>
        )}

        {detail && (
          <div className="message-scroll" ref={messageScrollRef} onScroll={handleScroll}>
            {detail.messages.length === 0 && !showLiveBlock && (
              <StarterPrompts onSelect={(prompt) => void send(prompt)} />
            )}

            <div className="message-list">
              {detail.messages.map((message) => (
                <MessageCard key={message.id} message={message} />
              ))}
              {showLiveBlock && latestRun && presentation && (
                <LiveRunBlock
                  run={latestRun}
                  presentation={presentation}
                  onCancel={handleCancelRun}
                  canceling={cancelingRunId === latestRun.id}
                />
              )}
              {!showLiveBlock && latestRun && latestRun.status === "failed" && !hasAuthoritativeAssistantMessage && (
                <div className="run-failed-banner">
                  <div className="run-failed-content">
                    <span className="run-failed-icon">⚠️</span>
                    <div className="run-failed-text">
                      <strong>Last execution failed</strong>
                      <span>{latestRun.error || "Execution was interrupted or failed in background worker."}</span>
                    </div>
                  </div>
                  <button
                    className="secondary-button retry-btn"
                    onClick={() => {
                      const lastPrompt = String(latestRun.request.prompt || "");
                      if (lastPrompt) void send(lastPrompt);
                    }}
                  >
                    ↺ Retry
                  </button>
                </div>
              )}
              <div ref={messagesEndRef} className="scroll-bottom-anchor" />
            </div>
          </div>
        )}

        {/* COMPACT COMPOSER */}
        <div
          className="composer-wrap"
          onDragOver={(e) => { e.preventDefault(); e.currentTarget.classList.add("drag-over"); }}
          onDragLeave={(e) => { e.currentTarget.classList.remove("drag-over"); }}
          onDrop={(e) => { e.preventDefault(); e.currentTarget.classList.remove("drag-over"); void handleFileSelect(e.dataTransfer.files); }}
        >
          {slashMenuOpen && (
            <SlashCommandMenu
              filter={draft}
              selectedIndex={slashSelectedIndex}
              onSelect={handleExecuteSlashCommand}
              onClose={() => setSlashMenuOpen(false)}
            />
          )}

          {pendingFiles.length > 0 && (
            <div className="composer-attachments">
              {pendingFiles.map((entry, index) => (
                <div key={index} className={`attachment-chip ${entry.state}`}>
                  <span className="attachment-chip-icon">{entry.file.type.startsWith("image/") ? "🖼" : "📄"}</span>
                  <span className="attachment-chip-name">{entry.file.name}</span>
                  {entry.state === "uploading" && <span className="attachment-chip-state">…</span>}
                  {entry.state === "failed" && <span className="attachment-chip-state">✕</span>}
                  <button className="attachment-chip-remove" onClick={() => removeFile(entry.file)} aria-label={`Remove ${entry.file.name}`}>×</button>
                </div>
              ))}
            </div>
          )}

          <div className="composer-card">
            <textarea
              ref={textareaRef}
              aria-label="Message GravityClaw"
              value={draft}
              onInput={(event) => {
                const target = event.currentTarget;
                target.style.height = "auto";
                target.style.height = `${Math.min(Math.max(target.scrollHeight, 32), 240)}px`;
              }}
              onChange={(event) => {
                const val = event.target.value;
                setDraft(val);
                if (val.startsWith("/") && !val.includes("\n") && !val.slice(1).includes(" ")) {
                  setSlashMenuOpen(true);
                  setSlashSelectedIndex(0);
                } else {
                  setSlashMenuOpen(false);
                }
              }}
              onKeyDown={(event) => {
                if (slashMenuOpen) {
                  const normalizedFilter = draft.toLowerCase().replace(/^\//, "").trim();
                  const currentFiltered = SLASH_COMMANDS.filter((cmd) => {
                    if (!normalizedFilter) return true;
                    return cmd.name.toLowerCase().includes(normalizedFilter) ||
                           cmd.label.toLowerCase().includes(normalizedFilter) ||
                           cmd.description.toLowerCase().includes(normalizedFilter);
                  });

                  if (event.key === "ArrowDown") {
                    event.preventDefault();
                    setSlashSelectedIndex((prev) => (prev + 1) % (currentFiltered.length || 1));
                    return;
                  } else if (event.key === "ArrowUp") {
                    event.preventDefault();
                    setSlashSelectedIndex((prev) => (prev - 1 + currentFiltered.length) % (currentFiltered.length || 1));
                    return;
                  } else if (event.key === "Enter" || event.key === "Tab") {
                    if (currentFiltered.length > 0) {
                      event.preventDefault();
                      const chosen = currentFiltered[(slashSelectedIndex % currentFiltered.length + currentFiltered.length) % currentFiltered.length];
                      handleExecuteSlashCommand(chosen);
                      return;
                    }
                  } else if (event.key === "Escape") {
                    event.preventDefault();
                    setSlashMenuOpen(false);
                    return;
                  }
                }

                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void send();
                }
              }}
              placeholder={activeRun ? "Type a follow-up to queue…" : "Ask GravityClaw…"}
              rows={1}
              disabled={!detail || sending}
              onPaste={(event) => {
                const items = event.clipboardData?.files;
                if (items && items.length > 0) {
                  event.preventDefault();
                  void handleFileSelect(items);
                }
              }}
            />
            <div className="composer-bottom-bar compact-composer-bar">
              <div className="composer-tools-left">
                <label className="composer-tool-btn" aria-label="Attach file" title="Attach file or image">
                  <input type="file" multiple hidden onChange={(event) => void handleFileSelect(event.target.files)} />
                  <span>＋</span>
                </label>
                {detail && (
                  <ModelSelector
                    conversationId={detail.conversation.id}
                    variant="compact"
                    placement="up"
                  />
                )}
              </div>

              <div className="composer-actions-right">
                {detail && (
                  <ContextStatus
                    conversationId={detail.conversation.id}
                    refreshKey={`${run?.id ?? "none"}:${run?.status ?? "none"}:${state.activity.length}`}
                  />
                )}
                <button
                  type="button"
                  className={`composer-send-btn ${hasDraft ? "has-content" : ""} ${sending ? "is-sending" : isRunning || isQueued ? "is-queuing" : ""}`}
                  onClick={() => void send()}
                  disabled={!hasDraft || !detail || sending || pendingFiles.some((f) => f.state === "uploading")}
                  aria-label={sending ? "Sending message" : hasDraft && activeRun ? "Queue message" : "Send message"}
                  title={sending ? "Sending…" : hasDraft && activeRun ? "Queue follow-up (↑)" : "Send (Enter)"}
                >
                  {sending ? (
                    <span className="composer-spinner" />
                  ) : (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                      <line x1="12" y1="19" x2="12" y2="5" />
                      <polyline points="5 12 12 5 19 12" />
                    </svg>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* SLIDE-OVER RUN INSPECTOR DRAWER */}
      {inspectorOpen && (
        <>
          <div className="inspector-drawer-backdrop" onClick={() => setInspectorOpen(false)} aria-hidden="true" />
          <aside className="workspace-inspector-drawer">
            <div className="inspector-drawer-header">
              <div className="brand-lockup">
                <strong>Run Inspector</strong>
                <span className="muted-text">Execution & Context</span>
              </div>
              <button className="icon-button" onClick={() => setInspectorOpen(false)}>✕</button>
            </div>
            <div className="inspector-drawer-body">
              {run ? (
                <>
                  <StatusBadge status={presentation?.status ?? run.status} large />
                  <h2 className="inspector-run-title">{String(run.request.prompt ?? activeTitle)}</h2>
                  <Detail label="Workspace" value="gravityclaw" />
                  <Detail label="Context" value={String(run.request.context_profile ?? "chat")} />
                  <Detail label="Run version" value={`v${run.version}`} />
                  <button
                    className="primary-button"
                    style={{ marginTop: 20, width: "100%" }}
                    onClick={() => {
                      onRunSelect(run);
                      setInspectorOpen(false);
                    }}
                  >
                    Open Full Trace Inspector →
                  </button>
                </>
              ) : (
                <EmptyState icon="◎" title="No active run" detail="Run details will appear here while GravityClaw executes tasks." />
              )}
            </div>
          </aside>
        </>
      )}

      {/* SEARCH MODAL */}
      {searchOpen && (
        <SearchModal
          onSelect={(convId) => {
            setSelectedId(convId);
            setSearchOpen(false);
          }}
          onClose={() => setSearchOpen(false)}
        />
      )}
    </div>
  );
}

function StarterPrompts({ onSelect }: { onSelect: (prompt: string) => void }) {
  const starters = [
    {
      icon: "💻",
      title: "Build or debug code",
      prompt: "Help me review the codebase structure and implement improvements.",
    },
    {
      icon: "🔍",
      title: "Inspect system architecture",
      prompt: "Explain how GravityClaw manages memory, agents, and background tasks.",
    },
    {
      icon: "⚡",
      title: "Check automations & health",
      prompt: "List all active schedules, cron triggers, and recent run history.",
    },
    {
      icon: "🧠",
      title: "Explore learned skills",
      prompt: "What learned skills and long-term memories are currently indexed?",
    },
  ];

  return (
    <div className="starter-prompts-container fade-in">
      <div className="starter-header">
        <div className="starter-mark">✦</div>
        <h2>What would you like to do?</h2>
        <p>Choose a starter action or type your prompt below.</p>
      </div>
      <div className="starter-grid">
        {starters.map((item, index) => (
          <button key={index} className="starter-card" onClick={() => onSelect(item.prompt)}>
            <span className="starter-icon">{item.icon}</span>
            <div className="starter-text">
              <strong>{item.title}</strong>
              <small>{item.prompt}</small>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function ArchivedChatsModal({
  archived,
  onRestore,
  onDeletePermanent,
  onClose,
}: {
  archived: Conversation[];
  onRestore: (id: string) => void;
  onDeletePermanent: (conv: Conversation) => void;
  onClose: () => void;
}) {
  const [filter, setFilter] = useState("");
  const filtered = archived.filter((c) => (c.title || "").toLowerCase().includes(filter.toLowerCase()));

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal-dialog archived-modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <div className="modal-title-lockup">
            <span className="archive-badge">📦</span>
            <h3>Archived Conversations</h3>
            <span className="count-pill">{archived.length}</span>
          </div>
          <button className="modal-close" onClick={onClose}>✕</button>
        </div>

        <div className="archived-search-bar">
          <input
            type="text"
            className="text-input archived-search-input"
            placeholder="Filter archived chats…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>

        <div className="archived-list">
          {filtered.length === 0 ? (
            <div className="archived-empty">No archived conversations found.</div>
          ) : (
            filtered.map((conv) => (
              <div key={conv.id} className="archived-item-row">
                <div className="archived-item-info">
                  <strong>{conv.title || "Untitled chat"}</strong>
                  <small>Archived {conv.archived_at ? formatTime(conv.archived_at) : "recently"}</small>
                </div>
                <div className="archived-item-actions">
                  <button
                    className="secondary-button restore-btn"
                    onClick={() => onRestore(conv.id)}
                    title="Restore to active chats"
                  >
                    <span>↺</span> Restore
                  </button>
                  <button
                    className="danger-button delete-perm-btn"
                    onClick={() => onDeletePermanent(conv)}
                    title="Permanently delete"
                  >
                    <span>🗑</span> Delete
                  </button>
                </div>
              </div>
            ))
          )}
        </div>

        <div className="modal-footer">
          <button className="secondary-button" onClick={onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

function SearchModal({ onSelect, onClose }: { onSelect: (convId: string) => void; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<ConversationSearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  useEffect(() => {
    const trimmed = query.trim();
    if (!trimmed) {
      setResults([]);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    const timer = setTimeout(() => {
      searchConversations(trimmed)
        .then((res) => {
          if (!cancelled) {
            setResults(res);
            setLoading(false);
          }
        })
        .catch(() => {
          if (!cancelled) setLoading(false);
        });
    }, 200);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query]);

  return (
    <div className="search-modal-backdrop" onClick={onClose}>
      <div className="search-modal-card" onClick={(e) => e.stopPropagation()}>
        <div className="search-modal-header">
          <span className="search-modal-icon">🔍</span>
          <input
            ref={inputRef}
            type="text"
            className="search-modal-input"
            placeholder="Search all chats & messages…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="search-modal-close" onClick={onClose}>✕</button>
        </div>
        <div className="search-modal-body">
          {loading && <div className="search-loading">Searching…</div>}
          {!loading && query && results.length === 0 && (
            <div className="search-empty">No matching messages or chats found.</div>
          )}
          {!query && (
            <div className="search-hint">Type keywords to search across all your conversations.</div>
          )}
          {results.map((res) => (
            <button
              key={`${res.conversation_id}:${res.message_id}`}
              className="search-result-item"
              onClick={() => onSelect(res.conversation_id)}
            >
              <div className="search-result-header">
                <strong>{res.kind === "main" ? "★ Main" : res.title}</strong>
                <span className="search-result-role">{res.role}</span>
              </div>
              <p className="search-result-snippet">{res.content}</p>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function MessageCard({ message }: { message: Message }) {
  const user = message.role === "user";
  const [copied, setCopied] = useState(false);

  const handleCopy = async (e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      const selection = window.getSelection()?.toString();
      const textToCopy = (selection && selection.trim().length > 0) ? selection : (message.content || "");
      await navigator.clipboard.writeText(textToCopy);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      void navigator.clipboard.writeText(message.content || "");
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  };

  if (user) {
    return (
      <article className="message-group user-group">
        <div className="user-meta-row">
          <span className="msg-meta-label">You · {formatTime(message.created_at)}</span>
          <button
            type="button"
            className={`msg-copy-icon-btn ${copied ? "copied" : ""}`}
            onClick={handleCopy}
            title="Copy message (or selection)"
            aria-label="Copy message"
          >
            {copied ? "✓" : "⧉"}
          </button>
        </div>
        <div className="user-bubble">
          <p className="user-message-text">{message.content}</p>
          {message.attachments && message.attachments.length > 0 && (
            <div className="message-attachments">
              {message.attachments.map((att) => (
                <AttachmentPreview key={att.id} attachment={att} />
              ))}
            </div>
          )}
        </div>
      </article>
    );
  }

  return (
    <article className="message-group assistant-group">
      <div className="assistant-meta-row">
        <div className="assistant-meta-left">
          <span className="agent-meta-name">GravityClaw</span>
          <span className="msg-meta-label">· {formatTime(message.created_at)}</span>
        </div>
        <button
          type="button"
          className={`msg-copy-icon-btn ${copied ? "copied" : ""}`}
          onClick={handleCopy}
          title="Copy response (or selection)"
          aria-label="Copy response"
        >
          {copied ? "✓" : "⧉"}
        </button>
      </div>
      <div className="assistant-body">
        <MarkdownMessage content={message.content} />
        {message.attachments && message.attachments.length > 0 && (
          <div className="message-attachments">
            {message.attachments.map((att) => (
              <AttachmentPreview key={att.id} attachment={att} />
            ))}
          </div>
        )}
      </div>
    </article>
  );
}

function useElapsedTime(startTime?: string | null, active: boolean = false): string {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (!active || !startTime) {
      setElapsed(0);
      return;
    }
    const start = new Date(startTime).getTime();
    const update = () => {
      const diff = Math.max(0, Math.floor((Date.now() - start) / 1000));
      setElapsed(diff);
    };
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [startTime, active]);

  if (!active || !startTime) return "";
  const mins = Math.floor(elapsed / 60);
  const secs = elapsed % 60;
  if (mins < 60) {
    return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  const hrs = Math.floor(mins / 60);
  const remMins = mins % 60;
  return `${hrs}h ${remMins}m ${secs}s`;
}

function ActivityTimelineRow({
  activity,
  active = false,
}: {
  activity: NormalizedActivity;
  active?: boolean;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);

  const durationStr = activity.durationSeconds !== undefined
    ? activity.durationSeconds < 1
      ? `${Math.round(activity.durationSeconds * 1000)}ms`
      : `${activity.durationSeconds.toFixed(1)}s`
    : undefined;

  const statusLabel = active
    ? "running"
    : activity.state === "finished"
    ? (durationStr || "completed")
    : activity.state === "failed"
    ? "failed"
    : "queued";

  const kindLabel = activity.kind === "command"
    ? "Terminal"
    : activity.kind === "file"
    ? "File"
    : activity.kind === "search"
    ? "Search"
    : activity.kind === "edit"
    ? "Edit"
    : activity.tool;

  const hasExpandable = Boolean(
    activity.command || activity.path || activity.query || activity.output || activity.error || (activity.detail && activity.detail !== activity.title)
  );

  return (
    <div className={`activity-timeline-row ${activity.state} ${active ? "is-active" : ""}`}>
      <div className="activity-row-main">
        <span className={`activity-status-dot ${activity.state} ${active ? "is-active" : ""}`}>
          {active ? "●" : activity.state === "finished" ? "✓" : activity.state === "failed" ? "✕" : "○"}
        </span>
        <div className="activity-row-body">
          <div className="activity-row-title-line">
            <strong className="activity-row-title">{activity.title}</strong>
          </div>
          <div className="activity-row-subline">
            <span className="activity-kind-tag">{kindLabel}</span>
            <span className="activity-subline-sep">·</span>
            <span className={`activity-state-tag ${activity.state}`}>{statusLabel}</span>
            {active && durationStr && (
              <>
                <span className="activity-subline-sep">·</span>
                <span className="activity-live-duration">{durationStr}</span>
              </>
            )}
            {hasExpandable && (
              <button
                type="button"
                className="activity-expand-btn"
                onClick={() => setDetailsOpen(!detailsOpen)}
              >
                {detailsOpen ? "▾ Hide command" : "▸ View command"}
              </button>
            )}
          </div>
        </div>
      </div>

      {detailsOpen && (
        <div className="activity-expanded-details">
          {activity.command && (
            <div className="activity-code-box">
              <span className="code-prompt">$</span>
              <code>{activity.command}</code>
            </div>
          )}
          {activity.cwd && (
            <div className="activity-cwd-box">
              <span className="cwd-icon">📁</span>
              <code>{activity.cwd}</code>
            </div>
          )}
          {activity.path && (
            <div className="activity-path-box">
              <code>{activity.path}</code>
              {activity.lines && <span className="lines-tag">{activity.lines}</span>}
            </div>
          )}
          {activity.query && (
            <div className="activity-query-box">
              <span>Query:</span> <code>"{activity.query}"</code>
            </div>
          )}
          {activity.output && (
            <div className="activity-output-box">
              <div className="output-header">Output ({activity.output.trim().split("\n").length} lines)</div>
              <pre>{activity.output.trim()}</pre>
            </div>
          )}
          {activity.error && (
            <div className="activity-error-box">
              ⚠️ {activity.error}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function LiveRunBlock({
  run,
  presentation,
  onCancel,
  canceling = false,
}: {
  run: RunRecord;
  presentation: PresentationState;
  onCancel?: (run: RunRecord) => void;
  canceling?: boolean;
}) {
  const [showAllCompleted, setShowAllCompleted] = useState(false);
  const [terminalOpen, setTerminalOpen] = useState(true);
  const [copiedTerminal, setCopiedTerminal] = useState(false);
  const elapsedStr = useElapsedTime(run.created_at, run.status === "running" || run.status === "queued");

  const currentActivity = presentation.currentActivity;
  const completedActivities = presentation.completedActivities;
  const taskSummary = presentation.currentTaskSummary;
  const isRunning = run.status === "running";
  const isQueued = run.status === "queued";
  const progress = presentation.progress;

  // Extract recent output lines from active activity or progress snapshot
  const rawTail = progress?.recent_output_tail || [];
  const activityOutput = currentActivity?.output ? currentActivity.output.trim().split("\n") : [];
  const terminalLines = rawTail.length > 0 ? rawTail : activityOutput;

  const handleCopyTerminal = async () => {
    try {
      await navigator.clipboard.writeText(terminalLines.join("\n"));
      setCopiedTerminal(true);
      setTimeout(() => setCopiedTerminal(false), 2000);
    } catch {}
  };

  return (
    <article className={`live-run-block ${run.status}`}>
      {/* 1. Header: Status + Elapsed Time + Stop Button */}
      <div className="live-run-header">
        <div className="live-run-header-left">
          <span className={`run-status-indicator ${run.status}`}>
            {isRunning ? "● Running" : isQueued ? "◌ Queued" : run.status === "completed" ? "✓ Completed" : "✕ Failed"}
          </span>
          {elapsedStr && <span className="live-elapsed-time">· {elapsedStr}</span>}
        </div>
        {(isRunning || isQueued) && onCancel && (
          <button
            type="button"
            className="live-cancel-btn"
            onClick={() => void onCancel(run)}
            disabled={canceling}
            title="Stop execution"
          >
            {canceling ? "Stopping…" : "■ Stop"}
          </button>
        )}
      </div>

      {/* 2. Direct Task Objective */}
      {taskSummary && (
        <div className="live-task-objective-text">
          {taskSummary}
        </div>
      )}

      {/* 3. Current Active Telemetry Status */}
      {currentActivity && (
        <div className="live-current-activity-box">
          <div className="current-activity-eyebrow">CURRENT ACTIVITY</div>
          <div className="current-activity-title-row">
            <strong className="current-activity-name">{currentActivity.title}</strong>
            <span className="current-activity-tag">{currentActivity.tool} · active</span>
          </div>
          {currentActivity.command && (
            <div className="current-activity-cmd">
              <span className="prompt-symbol">$</span>
              <code>{currentActivity.command}</code>
            </div>
          )}
          <div className="current-activity-telemetry-meta">
            <span className="telemetry-item">Process: <strong>alive</strong></span>
            {currentActivity.durationSeconds && (
              <span className="telemetry-item">· Elapsed: <strong>{currentActivity.durationSeconds.toFixed(1)}s</strong></span>
            )}
          </div>
        </div>
      )}

      {/* 4. Live Terminal Preview (Streamed real-time output) */}
      {terminalLines.length > 0 && (
        <div className="live-terminal-preview-box">
          <div className="terminal-header">
            <div className="terminal-header-left">
              <span className="terminal-dot green">●</span>
              <span className="terminal-title">Live Terminal Output</span>
              <span className="terminal-lines-count">({terminalLines.length} lines)</span>
            </div>
            <div className="terminal-header-actions">
              <button
                type="button"
                className={`terminal-action-btn ${copiedTerminal ? "copied" : ""}`}
                onClick={handleCopyTerminal}
                title="Copy terminal output"
              >
                {copiedTerminal ? "✓ Copied" : "Copy"}
              </button>
              <button
                type="button"
                className="terminal-action-btn"
                onClick={() => setTerminalOpen(!terminalOpen)}
              >
                {terminalOpen ? "Hide" : "Show"}
              </button>
            </div>
          </div>
          {terminalOpen && (
            <pre className="terminal-output-body">
              <code>{terminalLines.join("\n")}</code>
            </pre>
          )}
        </div>
      )}

      {/* 5. Semantic Progress Steps (if present) */}
      {progress && progress.completed_steps && progress.completed_steps.length > 0 && (
        <div className="semantic-progress-section">
          <div className="progress-section-eyebrow">PROGRESS</div>
          <div className="progress-steps-list">
            {progress.completed_steps.map((step) => (
              <div key={step.key} className="progress-step-row completed">
                <span className="step-icon">✓</span>
                <span className="step-label">{step.label}</span>
              </div>
            ))}
            {progress.active_step && (
              <div className="progress-step-row active">
                <span className="step-icon">●</span>
                <span className="step-label">{progress.active_step.label}</span>
              </div>
            )}
            {progress.pending_steps && progress.pending_steps.map((step) => (
              <div key={step.key} className="progress-step-row pending">
                <span className="step-icon">○</span>
                <span className="step-label">{step.label}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* 6. Activity Timeline */}
      {(completedActivities.length > 0 || (currentActivity && !terminalLines.length)) && (
        <div className="activity-timeline-section">
          <div className="activity-timeline-header">ACTIVITY HISTORY</div>
          <div className="activity-timeline-list">
            {(showAllCompleted ? completedActivities : completedActivities.slice(-3)).map((activity) => (
              <ActivityTimelineRow key={activity.id} activity={activity} />
            ))}
            {!showAllCompleted && completedActivities.length > 3 && (
              <button
                type="button"
                className="show-more-timeline-btn"
                onClick={() => setShowAllCompleted(true)}
              >
                + {completedActivities.length - 3} earlier events…
              </button>
            )}
          </div>
        </div>
      )}

      {/* 7. Subagents */}
      {presentation.subagents.length > 0 && (
        <div className="live-subagents-section">
          {presentation.subagents.map((agent, i) => (
            <div key={i} className="subagent-chip">↳ {agent}</div>
          ))}
        </div>
      )}

      {/* 8. Streaming Assistant Text */}
      {presentation.assistantText && (
        <div className="streaming-response-box">
          <MarkdownMessage content={presentation.assistantText} streaming={isRunning} />
        </div>
      )}

      {/* 9. Error Banner */}
      {run.status === "failed" && run.error && (
        <div className="run-error-banner">
          ✕ {run.error}
        </div>
      )}
    </article>
  );
}

function AttachmentPreview({ attachment }: { attachment: AttachmentRecord }) {
  const url = attachmentDownloadUrl(attachment.id);
  const isImage = attachment.kind === "image";
  const sizeLabel = attachment.size_bytes < 1024 ? `${attachment.size_bytes} B` : attachment.size_bytes < 1048576 ? `${(attachment.size_bytes / 1024).toFixed(1)} KB` : `${(attachment.size_bytes / 1048576).toFixed(1)} MB`;
  if (isImage) {
    return <a href={url} target="_blank" rel="noopener noreferrer" className="attachment-preview image-preview"><img src={url} alt={attachment.filename} loading="lazy" /><span className="attachment-label">{attachment.filename} · {sizeLabel}</span></a>;
  }
  return <a href={url} target="_blank" rel="noopener noreferrer" className="attachment-preview file-preview"><span className="file-icon">{attachment.kind === "document" ? "📄" : attachment.kind === "audio" ? "🎵" : attachment.kind === "video" ? "🎬" : attachment.kind === "archive" ? "📦" : "📎"}</span><span className="attachment-label">{attachment.filename} · {sizeLabel}</span></a>;
}

function StatCard({ label, value, detail, accent }: { label: string; value: string; detail: string; accent: string }) { return <div className={`stat-card ${accent}`}><div className="stat-label">{label}<span className="stat-glyph">✦</span></div><strong>{value}</strong><span>{detail}</span></div>; }
function PanelHeader({ title, link, onLink = () => undefined }: { title: string; link?: string; onLink?: () => void }) { return <div className="panel-header"><h2>{title}</h2>{link && <button className="text-link" onClick={onLink}>{link} →</button>}</div>; }
function RunRow({ run, onClick }: { run: RunRecord; onClick: () => void }) { return <button className="run-row" onClick={onClick}><StatusBadge status={run.status} /><div><strong>{String(run.request.prompt ?? "Untitled run")}</strong><span>{run.request.context_profile ?? "chat"} · {formatTime(run.created_at)}</span></div><span className="row-arrow">→</span></button>; }
function ActivityRow({ event }: { event: PersistedEvent }) { const label = event.event_type.replaceAll(".", " · "); return <div className="activity-row"><span className={`activity-icon ${event.event_type.includes("failed") ? "error" : event.event_type.includes("running") ? "active" : ""}`}>{event.event_type.includes("completed") ? "✓" : event.event_type.includes("failed") ? "!" : event.event_type.includes("tool") ? "⚙" : "•"}</span><div><strong>{label}</strong><span>{event.run_id.slice(0, 8)} · {formatTime(event.created_at)}</span></div><span className="activity-cursor">#{event.id}</span></div>; }
function StatusBadge({ status, large = false }: { status: string; large?: boolean }) { const labels: Record<string, string> = { running: "Running", queued: "Queued", completed: "Completed", failed: "Failed", cancelled: "Cancelled", interrupted: "Interrupted", orphaned: "Orphaned" }; const icons: Record<string, string> = { running: "◉", queued: "◌", completed: "✓", failed: "!", cancelled: "■", interrupted: "!", orphaned: "!" }; const label = labels[status] ?? status; return <span className={`status-badge ${status} ${large ? "large" : ""}`} role="status" aria-label={`Run status: ${label}`}><span className="status-symbol" aria-hidden="true">{icons[status] ?? "•"}</span><span className="status-dot" aria-hidden="true" />{label}</span>; }
function Detail({ label, value }: { label: string; value: string }) { return <div className="detail-row"><span>{label}</span><strong>{value}</strong></div>; }
function EmptyState({ icon, title, detail }: { icon: string; title: string; detail: string }) { return <div className="empty-state" role="status"><span className="empty-icon" aria-hidden="true">{icon}</span><strong>{title}</strong><span>{detail}</span></div>; }
function ComingSoon({ title }: { title: string }) { return <div className="page centered fade-in"><div className="placeholder-icon">✦</div><div className="eyebrow">M8B FOUNDATION</div><h1>{title}</h1><p className="muted">This surface is connected to the control plane and scheduled for the next M8B slice.</p></div>; }
function nextHeartbeat(snapshot: ControlState["snapshot"]): string { const schedule = snapshot?.next_schedules.find((item) => item.trigger_type === "heartbeat"); return schedule ? "18m" : "—"; }
function formatTime(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? "now" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }

class ErrorBoundary extends Component<{ children: React.ReactNode }, { hasError: boolean; error: string | null }> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { hasError: false, error: null };
  }
  static getDerivedStateFromError(error: Error) {
    return { hasError: true, error: error.message || "An unexpected error occurred." };
  }
  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    console.error("React ErrorBoundary caught error:", error, errorInfo);
  }
  render() {
    if (this.state.hasError) {
      return (
        <div style={{ padding: 32, textAlign: "center", color: "#f87171", background: "var(--bg)", minHeight: "100dvh", display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center" }}>
          <h2>Something went wrong</h2>
          <p style={{ color: "var(--text-3)", maxWidth: 500, margin: "8px 0 20px" }}>{this.state.error}</p>
          <button
            style={{ padding: "8px 18px", borderRadius: 8, background: "var(--accent)", color: "#fff", border: "none", cursor: "pointer", fontWeight: 600 }}
            onClick={() => { this.setState({ hasError: false, error: null }); window.location.reload(); }}
          >
            Reload Conversation
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

export default function RootApp() {
  return (
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  );
}
