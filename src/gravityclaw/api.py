"""FastAPI control plane for GravityClaw Core."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .channel_store import ChannelStore
from .channel_runtime import ChannelRuntime
from .capabilities import CapabilityManager
from .context import ContextBuilder, RunContextCompiler
from .event_bus import EventBus
from .execution import (
    AgyContainerSpecFactory,
    FakeContainerSpecFactory,
    PodmanExecutionBackend,
)
from .identity import IdentityStore
from .manager import RunManager
from .memory import MemoryService
from .scheduler import Scheduler
from .store import PersistedEvent, RunRecord, Store, TERMINAL_RUN_STATUSES
from .telegram import TelegramAdapter


@dataclass(frozen=True, slots=True)
class Settings:
    home: Path
    mode: str = "agy"
    worker_image: str | None = None
    poll_interval: float = 0.2
    telegram_token: str | None = field(default=None, repr=False)
    telegram_user_id: str | None = None
    telegram_default_workspace: str | None = None
    telegram_api_root: str = "https://api.telegram.org"
    scheduler_poll_interval: float = 1.0
    secret_dir: Path | None = field(default=None, repr=False)

    @property
    def database(self) -> Path:
        return self.home / "gravityclaw.db"

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            home=Path(
                os.environ.get(
                    "GRAVITYCLAW_HOME", str(Path.home() / ".gravityclaw")
                )
            ).resolve(),
            mode=os.environ.get("GRAVITYCLAW_MODE", "agy"),
            worker_image=os.environ.get("GRAVITYCLAW_WORKER_IMAGE"),
            poll_interval=float(os.environ.get("GRAVITYCLAW_POLL_INTERVAL", "0.2")),
            telegram_token=_telegram_token_from_environment(),
            telegram_user_id=os.environ.get("GRAVITYCLAW_TELEGRAM_USER_ID"),
            telegram_default_workspace=os.environ.get(
                "GRAVITYCLAW_TELEGRAM_DEFAULT_WORKSPACE"
            ),
            telegram_api_root=os.environ.get(
                "GRAVITYCLAW_TELEGRAM_API_ROOT", "https://api.telegram.org"
            ),
            scheduler_poll_interval=float(
                os.environ.get("GRAVITYCLAW_SCHEDULER_POLL_INTERVAL", "1.0")
            ),
            secret_dir=(
                Path(os.environ["GRAVITYCLAW_SECRET_DIR"]).resolve()
                if os.environ.get("GRAVITYCLAW_SECRET_DIR") else None
            ),
        )


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    path: str


class ConversationCreate(BaseModel):
    workspace_id: str
    channel: str = "web"
    channel_key: str | None = None
    title: str | None = None


class RunCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=12_000)
    scenario: str | None = None
    delay: float | None = None
    forbidden_path: str | None = None
    print_timeout: str = "15m"
    allow_all: bool = False
    context_profile: str = Field(default="chat", pattern="^(chat|coding|heartbeat|scheduled)$")


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    source: str = Field(min_length=1, max_length=500)
    conversation_id: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class ArtifactCreate(BaseModel):
    kind: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=5_000_000)
    summary: str = Field(default="", max_length=10_000)


class WorkspaceAliasCreate(BaseModel):
    alias: str = Field(min_length=1, max_length=64)
    workspace_id: str


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    trigger_type: str = Field(pattern="^(one_shot|interval|cron|heartbeat)$")
    expression: str = Field(min_length=1, max_length=200)
    timezone: str = "UTC"
    start_at: str | None = None
    prompt: str = Field(min_length=1, max_length=12_000)
    context_profile: str | None = None
    workspace_id: str
    conversation_policy: str = Field(default="new", pattern="^(new|resume)$")
    concurrency_policy: str = Field(default="QUEUE", pattern="^(SKIP|QUEUE|REPLACE)$")
    misfire_policy: str = Field(
        default="MISFIRE_RUN_ONCE",
        pattern="^(MISFIRE_SKIP|MISFIRE_RUN_ONCE|MISFIRE_CATCH_UP)$",
    )
    misfire_grace_seconds: int = Field(default=3600, ge=0)
    notification_policy: str = Field(default="silent", pattern="^(silent|actionable)$")
    notification_channel: str | None = None
    notification_chat_id: str | None = None


class SkillCreate(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    path: str
    workspace_id: str | None = None
    profiles: list[str] = Field(default_factory=list)
    version: str = "unversioned"


class MCPCreate(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    transport: str = Field(pattern="^(stdio|sse|http)$")
    command: str | None = None
    url: str | None = None
    args: list[str] = Field(default_factory=list)
    env_refs: dict[str, str] = Field(default_factory=dict)
    workspace_id: str | None = None


class CapabilityBindingCreate(BaseModel):
    workspace_id: str
    capability_type: str = Field(pattern="^(skill|mcp)$")
    capability_id: str
    profile: str = "*"


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_environment()
    configured.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    configured.home.chmod(0o700)
    store = Store(configured.database)
    identity = IdentityStore(configured.home)
    identity.bootstrap()
    # initialize before constructing services used by dispatch/retrieval
    store.initialize()
    memory = MemoryService(configured.home, store)
    channel_store = ChannelStore(store)
    capabilities = CapabilityManager(
        configured.home, store, secret_dir=configured.secret_dir
    )
    context_compiler = RunContextCompiler(
        store, identity, memory, ContextBuilder()
    )
    bus = EventBus()
    backend = PodmanExecutionBackend()
    if configured.mode == "fake":
        factory = FakeContainerSpecFactory(
            configured.worker_image or "localhost/gravityclaw-test-worker:latest"
        )
    elif configured.mode == "agy":
        factory = AgyContainerSpecFactory(
            configured.worker_image or "localhost/gravityclaw-agy:1.1.13"
        )
    else:
        raise ValueError(f"unsupported GravityClaw mode: {configured.mode}")
    manager = RunManager(
        store,
        backend,
        factory,
        bus,
        poll_interval=configured.poll_interval,
        context_compiler=context_compiler,
        capability_manager=capabilities,
    )
    scheduler = Scheduler(
        store, manager, channel_store,
        poll_interval=configured.scheduler_poll_interval,
    )
    if bool(configured.telegram_token) != bool(configured.telegram_user_id):
        raise ValueError(
            "Telegram requires both GRAVITYCLAW_TELEGRAM_BOT_TOKEN and "
            "GRAVITYCLAW_TELEGRAM_USER_ID"
        )
    channel_runtime: ChannelRuntime | None = None
    if configured.telegram_token and configured.telegram_user_id:
        channel_runtime = ChannelRuntime(
            manager,
            channel_store,
            TelegramAdapter(
                configured.telegram_token, api_root=configured.telegram_api_root
            ),
            authorized_sender_id=configured.telegram_user_id,
            default_workspace_alias=configured.telegram_default_workspace,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        report = await manager.start()
        app.state.reconciliation = asdict(report)
        scheduler_report = await scheduler.start()
        app.state.scheduler_reconciliation = asdict(scheduler_report)
        if channel_runtime is not None:
            await channel_runtime.start()
        try:
            yield
        finally:
            if channel_runtime is not None:
                await channel_runtime.close()
            await scheduler.close()
            await manager.close()

    app = FastAPI(title="GravityClaw Core", version="0.7.0", lifespan=lifespan)
    app.state.settings = configured
    app.state.store = store
    app.state.event_bus = bus
    app.state.manager = manager
    app.state.identity = identity
    app.state.memory = memory
    app.state.channel_store = channel_store
    app.state.channel_runtime = channel_runtime
    app.state.scheduler = scheduler
    app.state.capabilities = capabilities
    app.state.scheduler_reconciliation = {}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": configured.mode,
            "telegram": {"enabled": channel_runtime is not None},
            "reconciliation": app.state.reconciliation,
            "scheduler": app.state.scheduler_reconciliation,
        }

    @app.post("/schedules", status_code=201)
    async def create_schedule(body: ScheduleCreate) -> dict[str, Any]:
        values = body.model_dump()
        if values.get("context_profile") is None:
            values["context_profile"] = (
                "heartbeat" if body.trigger_type == "heartbeat" else "scheduled"
            )
        try:
            return asdict(scheduler.create_schedule(**values))
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/schedules")
    async def list_schedules(include_deleted: bool = False) -> list[dict[str, Any]]:
        return [asdict(item) for item in store.list_schedules(include_deleted=include_deleted)]

    @app.get("/schedules/{schedule_id}/triggers")
    async def list_schedule_triggers(schedule_id: str) -> list[dict[str, Any]]:
        try:
            store.get_schedule(schedule_id, include_deleted=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [asdict(item) for item in store.list_triggers(schedule_id=schedule_id)]

    @app.post("/schedules/{schedule_id}/enable")
    async def enable_schedule(schedule_id: str) -> dict[str, Any]:
        try:
            return asdict(store.set_schedule_enabled(schedule_id, True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/schedules/{schedule_id}/disable")
    async def disable_schedule(schedule_id: str) -> dict[str, Any]:
        try:
            return asdict(store.set_schedule_enabled(schedule_id, False))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> dict[str, Any]:
        try:
            return asdict(store.delete_schedule(schedule_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/capabilities/skills", status_code=201)
    async def register_skill(body: SkillCreate) -> dict[str, Any]:
        try:
            return _skill_json(capabilities.register_skill(
                skill_id=body.id, name=body.name, path=Path(body.path),
                workspace_id=body.workspace_id, profiles=body.profiles,
                version=body.version,
            ))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/capabilities/skills")
    async def list_skills(workspace_id: str | None = None) -> list[dict[str, Any]]:
        return [_skill_json(item) for item in capabilities.list_skills(workspace_id)]

    @app.post("/capabilities/skills/{skill_id}/enable")
    async def enable_skill(skill_id: str) -> dict[str, Any]:
        try:
            return _skill_json(capabilities.set_skill_enabled(skill_id, True))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/capabilities/skills/{skill_id}/disable")
    async def disable_skill(skill_id: str) -> dict[str, Any]:
        try:
            return _skill_json(capabilities.set_skill_enabled(skill_id, False))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/capabilities/mcp", status_code=201)
    async def register_mcp(body: MCPCreate) -> dict[str, Any]:
        try:
            return _mcp_json(capabilities.register_mcp(**body.model_dump()))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/capabilities/mcp")
    async def list_mcp(workspace_id: str | None = None) -> list[dict[str, Any]]:
        return [_mcp_json(item) for item in capabilities.list_mcp(workspace_id)]

    @app.post("/capabilities/mcp/{server_id}/health")
    async def health_mcp(server_id: str) -> dict[str, Any]:
        try:
            return _mcp_json(capabilities.health_check(server_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/capabilities/bind", status_code=204)
    async def bind_capability(body: CapabilityBindingCreate) -> None:
        try:
            capabilities.bind(**body.model_dump())
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/workspaces", status_code=201)
    async def create_workspace(body: WorkspaceCreate) -> dict[str, Any]:
        try:
            record = store.create_workspace(body.name, Path(body.path))
            return {**asdict(record), "path": str(record.path)}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/conversations", status_code=201)
    async def create_conversation(body: ConversationCreate) -> dict[str, Any]:
        try:
            return asdict(
                store.create_conversation(
                    body.workspace_id,
                    channel=body.channel,
                    channel_key=body.channel_key,
                    title=body.title,
                )
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/workspace-aliases", status_code=201)
    async def set_workspace_alias(body: WorkspaceAliasCreate) -> dict[str, str]:
        try:
            channel_store.set_workspace_alias(body.alias, body.workspace_id)
            return {"alias": body.alias.lower(), "workspace_id": body.workspace_id}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/workspace-aliases")
    async def list_workspace_aliases() -> list[dict[str, str]]:
        return channel_store.list_workspace_aliases()

    @app.post("/conversations/{conversation_id}/runs", status_code=202)
    async def submit_run(conversation_id: str, body: RunCreate) -> dict[str, Any]:
        request = body.model_dump(exclude_none=True)
        try:
            return _run_json(await manager.submit(conversation_id, request))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        try:
            return _run_json(store.get_run(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/conversations/{conversation_id}/runs")
    async def list_conversation_runs(conversation_id: str) -> list[dict[str, Any]]:
        return [
            _run_json(run)
            for run in store.list_runs(conversation_id=conversation_id)
        ]

    @app.get("/runs/{run_id}/events")
    async def list_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> list[dict[str, Any]]:
        try:
            store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return [
            _event_json(event)
            for event in store.list_events(run_id, after_sequence=after, limit=limit)
        ]

    @app.get("/runs/{run_id}/context")
    async def inspect_run_context(run_id: str) -> dict[str, Any]:
        try:
            return store.get_context_manifest(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/capabilities")
    async def inspect_run_capabilities(run_id: str) -> dict[str, Any]:
        try:
            return store.get_capability_manifest(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/conversations/{conversation_id}/context-watermark")
    async def inspect_context_watermark(conversation_id: str) -> dict[str, Any]:
        try:
            store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        watermark = store.get_context_watermark(conversation_id)
        return asdict(watermark) if watermark is not None else {}

    @app.get("/conversations/{conversation_id}/summaries")
    async def list_context_summaries(conversation_id: str) -> list[dict[str, Any]]:
        try:
            store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return store.list_conversation_summaries(conversation_id)

    @app.post("/runs/{run_id}/artifacts", status_code=201)
    async def create_artifact(run_id: str, body: ArtifactCreate) -> dict[str, str]:
        try:
            artifact_id = store.add_artifact(
                run_id, kind=body.kind, content=body.content, summary=body.summary
            )
            return {"id": artifact_id}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        try:
            return _run_json(await manager.cancel(run_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/identity")
    async def list_identity() -> list[dict[str, Any]]:
        return [
            {
                "name": document.name,
                "content": document.content,
                "sha256": document.sha256,
            }
            for document in identity.load()
        ]

    @app.post("/memories", status_code=201)
    async def record_memory(body: MemoryCreate) -> dict[str, str]:
        try:
            memory_id = memory.record_episode(
                body.content,
                source=body.source,
                conversation_id=body.conversation_id,
                confidence=body.confidence,
            )
            return {"id": memory_id}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/memories/search")
    async def search_memories(
        q: str = Query(min_length=1, max_length=10_000),
        limit: int = Query(default=8, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        return memory.retrieve(q, limit=limit)

    @app.websocket("/ws/runs/{run_id}")
    async def run_stream(
        websocket: WebSocket, run_id: str, after: int = Query(default=0, ge=0)
    ) -> None:
        await websocket.accept()
        try:
            run = store.get_run(run_id)
        except KeyError:
            await websocket.send_json({"type": "error", "detail": "run not found"})
            await websocket.close(code=4404)
            return
        await websocket.send_json({"type": "run.snapshot", "run": _run_json(run)})
        sequence = after
        version = bus.version(run_id)
        try:
            while True:
                events = store.list_events(run_id, after_sequence=sequence)
                for event in events:
                    await websocket.send_json(
                        {"type": "run.event", "event": _event_json(event)}
                    )
                    sequence = event.sequence
                run = store.get_run(run_id)
                if run.status in TERMINAL_RUN_STATUSES:
                    await websocket.send_json(
                        {"type": "run.terminal", "run": _run_json(run)}
                    )
                    await websocket.close(code=1000)
                    return
                version = await bus.wait(run_id, version)
        except WebSocketDisconnect:
            return

    return app


def _run_json(run: RunRecord) -> dict[str, Any]:
    return asdict(run)


def _event_json(event: PersistedEvent) -> dict[str, Any]:
    return asdict(event)


def _skill_json(skill: Any) -> dict[str, Any]:
    value = asdict(skill)
    value["path"] = str(skill.path)
    return value


def _mcp_json(server: Any) -> dict[str, Any]:
    return asdict(server)


def _telegram_token_from_environment() -> str | None:
    direct = os.environ.get("GRAVITYCLAW_TELEGRAM_BOT_TOKEN")
    secret_file = os.environ.get("GRAVITYCLAW_TELEGRAM_BOT_TOKEN_FILE")
    if direct and secret_file:
        raise ValueError(
            "set only one of GRAVITYCLAW_TELEGRAM_BOT_TOKEN or "
            "GRAVITYCLAW_TELEGRAM_BOT_TOKEN_FILE"
        )
    if not secret_file:
        return direct
    data = Path(secret_file).read_bytes()
    if len(data) > 4096:
        raise ValueError("Telegram token file is unexpectedly large")
    token = data.decode("utf-8").strip()
    if not token:
        raise ValueError("Telegram token file is empty")
    return token
