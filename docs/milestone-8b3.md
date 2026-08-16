# Milestone 8B.3 — Conversation Workspace

M8B.3 turns the control-center shell into a real Web conversation surface while
keeping GravityClaw Core authoritative.

## Delivered

- Conversation navigator backed by `GET /api/v1/conversations`
- Conversation hydration backed by the durable conversation detail endpoint
- Telegram-originated conversations can be opened and continued from Web
- Web messages submit through the existing queued-run path
- Follow-ups remain queueable while a conversation run is active
- Multiline composer with Enter-to-send and Shift+Enter newline behavior
- Durable message timeline with channel/source metadata
- Normalized presentation reducer for deltas, tools, subagents, and run state
- Reconnect-safe activity merging and cursor deduplication
- Run stop action uses the existing version-checked cancellation endpoint
- Focus and Inspect modes
- Responsive conversation navigator, composer, activity block, and inspector

## State boundary

The UI never becomes a conversation engine. It fetches persisted conversation
state, renders it, and submits ordinary GravityClaw runs. Streaming activity is
derived from normalized persisted events; it is not treated as durable message
history. A refresh or WebSocket loss therefore cannot create a second response
or a second run.

## Verification

- Frontend `npm run build`: passed
- Backend: 72 tests passed
- M8A control-plane acceptance: passed
- Web/Telegram shared-conversation API contract: passed

Deferred to later M8B slices: rich context/memory panels, artifact and raw-event
views, full subagent tree, attachments, command palette, global search, and the
full visual/reconnect acceptance gate.
