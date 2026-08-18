# Console Information Architecture (v2)

Adopted: 2026-08-18. Supersedes the flat "Control Center" navigation in `web/src/App.tsx`.

## Principle

One primary activity: **conversation/session management**. Everything else is administration and goes under MORE. The sidebar stops competing for attention with 10 top-level items.

## Three-layer model

| Layer | Answers | Contains |
|---|---|---|
| Sidebar | *where I am* | sessions, workspace, administration (collapsed) |
| Chat header | *how this session runs* | model picker, context status, run/trace info |
| Composer | *what I want the agent to do* | message, attachments, slash commands |

## Sidebar (collapsed state)

```
GravityClaw
⌁ Overview

────────────────────

＋ New session

SESSIONS  Main ▾  ≡

Current Project      📌 ⋯
GravityClaw UI Review 📌 ⋯
Modal Deployment      ⋯
Hermes Research       ⋯
Model Testing         ⋯

All sessions ›

────────────────────

● Connected  ⚙ ◫ ◧
```

Rules:
- Pinned sessions render above recent sessions; always visible, never paginated away.
- Each session row has a subtle ⋯ menu (rename, pin/unpin, delete, details).
- Internal IDs (`agent:main:test-longrun...`) are never shown in the row; human-readable titles (generated, renameable) replace them. Raw ID stays available under the details/⋯ menu.
- Session count dominates the visible area — it is the primary activity.
- Bottom line: connection status + quick toggles (theme, density, settings).

## MORE (expanded inline)

MORE expands/collapses **inline in the sidebar** — no separate page or full-screen menu:

```
MORE ⌄
⚡ Automations
🧠 Memory
◇ Skills
✦ Learning
⌘ Capabilities
🔌 Integrations
◫ Workspaces
◎ Models
◉ Channels
▤ Logs / Audit
⚙ Settings
```

Mapping from current flat nav (App.tsx `navItems`):
- `home` → Overview (top, always visible)
- `conversations` → the session list itself (no longer a nav item)
- `runs` → Chat header / trace layer + under MORE (Logs / Audit)
- `memory`, `learning`, `workspaces`, `automations`, `capabilities`, `channels` → MORE
- `context` → chat header (context status) — not a top-level nav
- `usage` → MORE (Logs / Audit or Models)
- `settings` → MORE
- MCP/Integrations and Models & Providers get explicit entries once wired up.

## Implementation phases

1. **Shell restructure** — split `App.tsx` (1880 lines) into `Shell.tsx` (sidebar + layout), `Sidebar.tsx`, `SessionList.tsx`, `MoreMenu.tsx`; wire hash routing to the new IA. MORE is inline expand/collapse with a persisted expanded state.
2. **Session list** — pin/rename API surface (backend if missing), pinned-first grouping, ⋯ per-row menu, generated titles with raw ID under details.
3. **Chat header layer** — model picker (existing `ModelSelector`), context status, and trace/run info consolidated into the header; remove `context` and `runs` from top-level nav.
4. **Polish** — mobile drawer behavior, keyboard nav, connection line, icon pass.

## Non-goals

- No multi-column master-detail on mobile (drawer pattern only).
- No settings deep-linking changes; routes stay hash-based.
