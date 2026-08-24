"""Reduce verbose run events into a small channel presentation."""

from __future__ import annotations

from dataclasses import dataclass, field

from .store import PersistedEvent, RunRecord


@dataclass(slots=True)
class PresentationState:
    response: str = ""
    current_tool: str | None = None
    completed_tools: list[str] = field(default_factory=list)
    active_subagents: int = 0
    error: str | None = None


class PresentationReducer:
    def __init__(self, max_characters: int = 3900) -> None:
        self.max_characters = max_characters

    def reduce(self, run: RunRecord, events: list[PersistedEvent]) -> tuple[str, int]:
        state = PresentationState()
        sequence = 0
        for event in events:
            sequence = max(sequence, event.sequence)
            if event.event_type in {"message.delta", "model.streaming"}:
                delta = event.payload.get("text_delta") or event.payload.get("content") or ""
                state.response += str(delta)
            elif event.event_type in {"agent.completed", "model.completed"}:
                response = str(event.payload.get("response") or event.payload.get("content") or "")
                if response.strip():
                    state.response = response.strip()
            elif event.event_type in {"tool.started", "process.started", "ssh.command_started"}:
                state.current_tool = _tool_label(event)
            elif event.event_type in {"tool.finished", "process.exited", "ssh.command_exited"}:
                label = _tool_label(event)
                state.current_tool = None
                if not state.completed_tools or state.completed_tools[-1] != label:
                    state.completed_tools.append(label)
            elif event.event_type in {"tool.failed", "process.failed"}:
                label = _tool_label(event)
                state.current_tool = None
                state.error = f"Tool failed: {label}"
            elif event.event_type == "subagent.updated":
                info = event.payload.get("subagent_info")
                subagents = info.get("subagents", []) if isinstance(info, dict) else []
                terminal = event.payload.get("state") in {
                    "DONE",
                    "ERROR",
                    "CANCELED",
                    "INTERRUPTED",
                }
                state.active_subagents = (
                    0 if terminal else len(subagents) if isinstance(subagents, list) else 0
                )
            elif event.event_type in {"agent.failed", "backend.protocol_error", "backend.monitor_error"}:
                state.error = str(
                    event.payload.get("error")
                    or event.payload.get("message")
                    or "Execution failed"
                )

        if run.status in {"failed", "interrupted", "orphaned"} and not state.error:
            state.error = run.error or run.status.title()

        if run.status == "completed":
            if state.response.strip():
                text = state.response.strip()
            elif state.completed_tools:
                lines = ["✓ Completed"]
                for label in state.completed_tools[-3:]:
                    lines.append(f"✓ {label}")
                text = "\n".join(lines)
            else:
                text = "✓ Completed"
        elif run.status == "running":
            lines: list[str] = []
            for label in state.completed_tools[-3:]:
                lines.append(f"✓ {label}")
            if state.current_tool:
                lines.append(f"⚙ {state.current_tool}")
            if state.active_subagents:
                count = state.active_subagents
                lines.append(f"↳ {count} subagent{'s' if count != 1 else ''} active")
            if state.error:
                lines.append(f"⚠ {state.error}")
            if state.response.strip():
                if lines:
                    text = "\n".join(lines) + "\n\n" + state.response.strip() + " ▌"
                else:
                    text = state.response.strip() + " ▌"
            else:
                if not lines:
                    lines.append("◉ Working…")
                text = "\n".join(lines)
        elif run.status in {"failed", "interrupted", "cancelled", "orphaned"}:
            heading = _run_heading(run.status)
            if state.response.strip():
                text = f"{state.response.strip()}\n\n{heading}: {state.error or 'error'}"
            else:
                text = f"{heading}: {state.error or 'error'}"
        else:
            text = _run_heading(run.status)

        if len(text) > self.max_characters:
            text = text[: self.max_characters - 1].rstrip() + "…"
        return text, sequence


def _tool_label(event: PersistedEvent) -> str:
    return str(event.payload.get("tool_name") or event.payload.get("name") or "tool")[:120]


def _run_heading(status: str) -> str:
    return {
        "queued": "◌ Queued…",
        "running": "◉ Working…",
        "completed": "✓ Completed",
        "failed": "✗ Failed",
        "cancelled": "■ Cancelled",
        "interrupted": "⚠ Interrupted",
        "orphaned": "⚠ Orphaned",
    }.get(status, f"• {status.title()}")
