# Milestone 8B — Foundation

This checkpoint contains M8B.1 and M8B.2 plus the first operational surfaces.

## Delivered

- Vite + React + TypeScript console under `web/`
- Responsive desktop/tablet/mobile shell
- First-class dark/light themes
- Sidebar, top bar, status badges, panels, tables, activity cards, and run inspector primitives
- Browser session bridge using an HttpOnly signed cookie
- No production control token in localStorage, the URL, or the frontend bundle
- One centralized control replay client for snapshot hydration, cursor tracking,
  event deduplication, reconnect, and offline state
- Home and Runs surfaces backed by M8A read models
- Initial run inspector with Run, Context, Capabilities, and Events tabs

## Run locally

```bash
cd web
npm install
npm run dev
```

Vite proxies `/api`, `/auth`, `/health`, and `/ws` to the local GravityClaw
server at `127.0.0.1:8787`. The production bundle is built with `npm run build`.

## Boundary

The browser is a projection, never the source of truth. Refreshing or losing the
WebSocket causes the client to fetch a fresh snapshot and resume persisted events
from its cursor. The session token is exchanged once and held by an HttpOnly
cookie; the React state only holds current UI data.

## Verification

- Frontend `npm run build`: passed
- Backend: 71 tests passed
- M8A control-plane acceptance: passed
- Vite dev-server smoke request: passed

Remaining M8B work is the conversation workspace, context/memory studio,
automation and capability explorers, command palette, accessibility/performance
hardening, and the full reconnect/visual acceptance gate.
