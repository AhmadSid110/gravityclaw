"""FastAPI control plane for GravityClaw Core."""

from __future__ import annotations

import os
import hmac
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
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
from .store import PersistedEvent, RunRecord, Store, TERMINAL_RUN_STATUSES, VersionConflict
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
    control_token: str | None = field(default=None, repr=False)

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
            control_token=_control_token_from_environment(),
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


class IdentityUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=256_000)
    expected_version: int = Field(ge=1)


class JournalUpdate(BaseModel):
    content: str = Field(max_length=1_000_000)
    expected_sha256: str | None = None


class ContextPreview(BaseModel):
    task: str = Field(min_length=1, max_length=12_000)
    profile: str = Field(default="chat", pattern="^(chat|coding|heartbeat|scheduled)$")
    conversation_id: str | None = None


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


class VersionedMutation(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)


class RunCancel(BaseModel):
    expected_version: int | None = Field(default=None, ge=1)


class SessionLogin(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_environment()
    configured.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    configured.home.chmod(0o700)
    store = Store(configured.database)
    identity = IdentityStore(configured.home)
    identity.bootstrap()
    # initialize before constructing services used by dispatch/retrieval
    store.initialize()
    identity_lock = threading.Lock()
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

    app = FastAPI(title="GravityClaw Control Plane", version="0.8.0", lifespan=lifespan)
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
    app.state.reconciliation = {}
    app.state.scheduler_reconciliation = {}
    app.state.control_auth_enabled = configured.control_token is not None

    @app.middleware("http")
    async def control_auth(request: Request, call_next: Any) -> Any:
        public = request.url.path in {
            "/health", "/docs", "/openapi.json", "/redoc", "/auth/session"
        }
        if configured.control_token is not None and not public:
            provided = _bearer_token(request.headers.get("authorization"))
            session = request.cookies.get("gravityclaw_session")
            if not _credential_authorized(provided, session, configured.control_token):
                return JSONResponse(
                    {"detail": "control-plane authentication required"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.actor = "control-token"
        else:
            request.state.actor = "local"
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": configured.mode,
            "telegram": {"enabled": channel_runtime is not None},
            "reconciliation": app.state.reconciliation,
            "scheduler": app.state.scheduler_reconciliation,
        }

    @app.post("/auth/session")
    async def create_browser_session(body: SessionLogin, response: Response) -> dict[str, Any]:
        if configured.control_token is None:
            raise HTTPException(status_code=503, detail="control authentication is disabled")
        if not hmac.compare_digest(body.token, configured.control_token):
            raise HTTPException(status_code=401, detail="invalid control token")
        response.set_cookie(
            "gravityclaw_session", _session_cookie(configured.control_token),
            max_age=12 * 60 * 60, httponly=True,
            secure=os.environ.get("GRAVITYCLAW_COOKIE_SECURE", "0") == "1",
            samesite="lax", path="/",
        )
        return {"authenticated": True, "expires_in": 12 * 60 * 60}

    @app.get("/auth/session")
    async def browser_session(request: Request) -> dict[str, Any]:
        authenticated = configured.control_token is None or _credential_authorized(
            _bearer_token(request.headers.get("authorization")),
            request.cookies.get("gravityclaw_session"), configured.control_token,
        )
        return {"authenticated": authenticated}

    @app.delete("/auth/session")
    async def delete_browser_session(response: Response) -> dict[str, bool]:
        response.delete_cookie("gravityclaw_session", path="/")
        return {"authenticated": False}

    @app.post("/schedules", status_code=201)
    async def create_schedule(body: ScheduleCreate, request: Request) -> dict[str, Any]:
        values = body.model_dump()
        if values.get("context_profile") is None:
            values["context_profile"] = (
                "heartbeat" if body.trigger_type == "heartbeat" else "scheduled"
            )
        try:
            record = scheduler.create_schedule(**values)
            app.state.store.record_audit(
                actor=request.state.actor, action="schedule.create",
                resource_type="schedule", resource_id=record.id,
                resulting_version=record.version,
                payload={"name": record.name, "trigger_type": record.trigger_type},
            )
            return asdict(record)
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
    async def enable_schedule(
        schedule_id: str, request: Request, body: VersionedMutation = VersionedMutation()
    ) -> dict[str, Any]:
        try:
            record = store.set_schedule_enabled(schedule_id, True, expected_version=body.expected_version)
            store.record_audit(actor=request.state.actor, action="schedule.enable",
                               resource_type="schedule", resource_id=schedule_id,
                               expected_version=body.expected_version,
                               resulting_version=record.version)
            return asdict(record)
        except (KeyError, VersionConflict) as exc:
            if isinstance(exc, VersionConflict):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/schedules/{schedule_id}/disable")
    async def disable_schedule(
        schedule_id: str, request: Request, body: VersionedMutation = VersionedMutation()
    ) -> dict[str, Any]:
        try:
            record = store.set_schedule_enabled(schedule_id, False, expected_version=body.expected_version)
            store.record_audit(actor=request.state.actor, action="schedule.disable",
                               resource_type="schedule", resource_id=schedule_id,
                               expected_version=body.expected_version,
                               resulting_version=record.version)
            return asdict(record)
        except (KeyError, VersionConflict) as exc:
            if isinstance(exc, VersionConflict):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.delete("/schedules/{schedule_id}")
    async def delete_schedule(
        schedule_id: str, request: Request, body: VersionedMutation = VersionedMutation()
    ) -> dict[str, Any]:
        try:
            record = store.delete_schedule(schedule_id, expected_version=body.expected_version)
            store.record_audit(actor=request.state.actor, action="schedule.delete",
                               resource_type="schedule", resource_id=schedule_id,
                               expected_version=body.expected_version,
                               resulting_version=record.version)
            return asdict(record)
        except (KeyError, VersionConflict) as exc:
            if isinstance(exc, VersionConflict):
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/capabilities/skills", status_code=201)
    async def register_skill(body: SkillCreate, request: Request) -> dict[str, Any]:
        try:
            skill = capabilities.register_skill(
                skill_id=body.id, name=body.name, path=Path(body.path),
                workspace_id=body.workspace_id, profiles=body.profiles,
                version=body.version,
            )
            store.record_audit(actor=request.state.actor, action="skill.register",
                               resource_type="skill", resource_id=body.id,
                               payload={"name": body.name, "workspace_id": body.workspace_id})
            return _skill_json(skill)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/capabilities/skills")
    async def list_skills(workspace_id: str | None = None) -> list[dict[str, Any]]:
        return [_skill_json(item) for item in capabilities.list_skills(workspace_id)]

    @app.post("/capabilities/skills/{skill_id}/enable")
    async def enable_skill(skill_id: str, request: Request) -> dict[str, Any]:
        try:
            skill = capabilities.set_skill_enabled(skill_id, True)
            store.record_audit(actor=request.state.actor, action="skill.enable",
                               resource_type="skill", resource_id=skill_id)
            return _skill_json(skill)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/capabilities/skills/{skill_id}/disable")
    async def disable_skill(skill_id: str, request: Request) -> dict[str, Any]:
        try:
            skill = capabilities.set_skill_enabled(skill_id, False)
            store.record_audit(actor=request.state.actor, action="skill.disable",
                               resource_type="skill", resource_id=skill_id)
            return _skill_json(skill)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/capabilities/mcp", status_code=201)
    async def register_mcp(body: MCPCreate, request: Request) -> dict[str, Any]:
        try:
            server = capabilities.register_mcp(**body.model_dump())
            store.record_audit(actor=request.state.actor, action="mcp.register",
                               resource_type="mcp", resource_id=body.id,
                               payload={"name": body.name, "transport": body.transport,
                                        "workspace_id": body.workspace_id,
                                        "env_refs": body.env_refs})
            return _mcp_json(server)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/capabilities/mcp")
    async def list_mcp(workspace_id: str | None = None) -> list[dict[str, Any]]:
        return [_mcp_json(item) for item in capabilities.list_mcp(workspace_id)]

    @app.post("/capabilities/mcp/{server_id}/health")
    async def health_mcp(server_id: str, request: Request) -> dict[str, Any]:
        try:
            server = capabilities.health_check(server_id)
            store.record_audit(actor=request.state.actor, action="mcp.health_check",
                               resource_type="mcp", resource_id=server_id,
                               payload={"health_state": server.health_state})
            return _mcp_json(server)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/capabilities/bind", status_code=204)
    async def bind_capability(body: CapabilityBindingCreate, request: Request) -> None:
        try:
            capabilities.bind(**body.model_dump())
            store.record_audit(actor=request.state.actor, action="capability.bind",
                               resource_type=body.capability_type,
                               resource_id=body.capability_id,
                               payload={"workspace_id": body.workspace_id, "profile": body.profile})
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/workspaces", status_code=201)
    async def create_workspace(body: WorkspaceCreate, request: Request) -> dict[str, Any]:
        try:
            record = store.create_workspace(body.name, Path(body.path))
            store.record_audit(actor=request.state.actor, action="workspace.create",
                               resource_type="workspace", resource_id=record.id,
                               payload={"name": record.name, "path": str(record.path)})
            return {**asdict(record), "path": str(record.path)}
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/conversations", status_code=201)
    async def create_conversation(body: ConversationCreate, request: Request) -> dict[str, Any]:
        try:
            record = store.create_conversation(
                    body.workspace_id,
                    channel=body.channel,
                    channel_key=body.channel_key,
                    title=body.title,
                )
            store.record_audit(actor=request.state.actor, action="conversation.create",
                               resource_type="conversation", resource_id=record.id,
                               payload={"workspace_id": record.workspace_id, "channel": record.channel})
            return asdict(record)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/workspace-aliases", status_code=201)
    async def set_workspace_alias(body: WorkspaceAliasCreate, request: Request) -> dict[str, str]:
        try:
            channel_store.set_workspace_alias(body.alias, body.workspace_id)
            store.record_audit(actor=request.state.actor, action="workspace_alias.set",
                               resource_type="workspace_alias", resource_id=body.alias.lower(),
                               payload={"workspace_id": body.workspace_id})
            return {"alias": body.alias.lower(), "workspace_id": body.workspace_id}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/workspace-aliases")
    async def list_workspace_aliases() -> list[dict[str, str]]:
        return channel_store.list_workspace_aliases()

    @app.post("/conversations/{conversation_id}/runs", status_code=202)
    async def submit_run(conversation_id: str, body: RunCreate, request: Request) -> dict[str, Any]:
        request_data = body.model_dump(exclude_none=True)
        try:
            record = await manager.submit(conversation_id, request_data)
            store.record_audit(actor=request.state.actor, action="run.submit",
                               resource_type="run", resource_id=record.id,
                               resulting_version=record.version,
                               payload={"conversation_id": conversation_id})
            return _run_json(record)
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
    @app.get("/api/v1/runs/{run_id}/context")
    async def inspect_run_context(run_id: str) -> dict[str, Any]:
        try:
            return store.get_context_manifest(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/capabilities")
    @app.get("/api/v1/runs/{run_id}/capabilities")
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
    async def cancel_run(
        run_id: str, request: Request, body: RunCancel = RunCancel()
    ) -> dict[str, Any]:
        try:
            current = store.get_run(run_id)
            if body.expected_version is not None and current.version != body.expected_version:
                raise HTTPException(
                    status_code=409,
                    detail=f"run {run_id} version is {current.version}, expected {body.expected_version}",
                )
            result = await manager.cancel(run_id)
            store.record_audit(actor=request.state.actor, action="run.cancel",
                               resource_type="run", resource_id=run_id,
                               expected_version=body.expected_version,
                               resulting_version=result.version)
            return _run_json(result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/identity")
    async def list_identity() -> list[dict[str, Any]]:
        return [_identity_json(document) for document in identity.load()]

    @app.get("/api/v1/identity")
    async def control_identity() -> list[dict[str, Any]]:
        result = []
        for document in identity.load((
            "SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"
        )):
            result.append({**_identity_json(document), **store.sync_identity_revision(
                document.name, document.sha256, document.content
            )})
        return result

    @app.get("/api/v1/identity/{name}/history")
    async def identity_history(name: str) -> list[dict[str, Any]]:
        if name not in ("SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"):
            raise HTTPException(status_code=404, detail="identity document not found")
        document = identity.load((name,))[0]
        store.sync_identity_revision(name, document.sha256, document.content)
        return store.list_identity_revisions(name)

    @app.put("/api/v1/identity/{name}")
    async def update_identity(name: str, body: IdentityUpdate, request: Request) -> dict[str, Any]:
        if name not in ("SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"):
            raise HTTPException(status_code=404, detail="identity document not found")
        try:
            with identity_lock:
                current = identity.load((name,))[0]
                revision = store.sync_identity_revision(name, current.sha256, current.content)
                if int(revision["version"]) != body.expected_version:
                    raise VersionConflict(
                        f"identity {name} version is {revision['version']}, expected {body.expected_version}"
                    )
                updated = identity.update(name, body.content)
                result = store.append_identity_revision(
                    name, updated.content, updated.sha256, expected_version=body.expected_version
                )
                store.record_audit(actor=request.state.actor, action="identity.update",
                                   resource_type="identity", resource_id=name,
                                   expected_version=body.expected_version,
                                   resulting_version=result["version"])
                return {**_identity_json(updated), **result}
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/memories", status_code=201)
    async def record_memory(body: MemoryCreate, request: Request) -> dict[str, str]:
        try:
            memory_id = memory.record_episode(
                body.content,
                source=body.source,
                conversation_id=body.conversation_id,
                confidence=body.confidence,
            )
            store.record_audit(actor=request.state.actor, action="memory.record",
                               resource_type="memory", resource_id=memory_id,
                               payload={"source": body.source, "conversation_id": body.conversation_id})
            return {"id": memory_id}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/memories/search")
    async def search_memories(
        q: str = Query(min_length=1, max_length=10_000),
        limit: int = Query(default=8, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        return memory.retrieve(q, limit=limit)

    @app.get("/api/v1/memories")
    async def list_memories(kind: str | None = None, limit: int = Query(default=200, ge=1, le=1000)) -> list[dict[str, Any]]:
        return store.list_memories(kind=kind, limit=limit)

    @app.get("/api/v1/memories/search")
    async def control_search_memories(q: str = Query(min_length=1, max_length=10_000), limit: int = Query(default=50, ge=1, le=100)) -> list[dict[str, Any]]:
        return memory.retrieve(q, limit=limit)

    @app.get("/api/v1/memories/{memory_id}")
    async def inspect_memory(memory_id: str) -> dict[str, Any]:
        try:
            return {**store.get_memory(memory_id), "usage": store.memory_usage(memory_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/journals")
    async def list_journals() -> list[dict[str, Any]]:
        return memory.list_journals()

    @app.get("/api/v1/journals/{date}")
    async def get_journal(date: str) -> dict[str, Any]:
        try:
            return memory.read_journal(date)
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.put("/api/v1/journals/{date}")
    async def update_journal(date: str, body: JournalUpdate, request: Request) -> dict[str, Any]:
        try:
            result = memory.update_journal(date, body.content, body.expected_sha256)
            store.record_audit(actor=request.state.actor, action="journal.update",
                               resource_type="journal", resource_id=date)
            return result
        except ValueError as exc:
            status = 409 if "changed" in str(exc) else 422
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @app.post("/api/v1/context/preview")
    async def context_preview(body: ContextPreview) -> dict[str, Any]:
        try:
            compiled = context_compiler.preview(
                task=body.task, profile=body.profile, conversation_id=body.conversation_id
            )
            manifest = compiled.manifest()
            manifest.pop("summary_proposal", None)
            return {"manifest": manifest, "preview": True, "prompt_characters": len(compiled.prompt)}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Versioned control-plane read models. These are deliberately composed
    # from the existing durable stores; the UI never gets a parallel state DB.

    def control_snapshot(*, include_activity: bool = True) -> dict[str, Any]:
        runs = store.list_runs()
        schedules = store.list_schedules()
        events = store.list_events_global(after_id=max(0, store.latest_event_id() - 50), limit=50)
        snapshot = {
            "api_version": "2026-08-16",
            "health": {
                "status": "ok",
                "mode": configured.mode,
                "telegram": {"enabled": channel_runtime is not None},
                "auth": {"enabled": configured.control_token is not None},
            },
            "counts": {
                "runs": len(runs),
                "active_runs": sum(item.status == "running" for item in runs),
                "queued_runs": sum(item.status == "queued" for item in runs),
                "schedules": len(schedules),
            },
            "active_runs": [_run_json(item) for item in runs if item.status in {"running", "queued"}],
            "next_schedules": [asdict(item) for item in schedules if item.enabled][:10],
        }
        if include_activity:
            snapshot["activity"] = [
                {"cursor": item.id, "event": _event_json(item)} for item in events
            ]
        return snapshot

    @app.get("/api/v1/control/home")
    async def control_home() -> dict[str, Any]:
        return control_snapshot()

    @app.get("/api/v1/workspaces")
    async def control_workspaces() -> list[dict[str, Any]]:
        return [{**asdict(item), "path": str(item.path)} for item in store.list_workspaces()]

    @app.get("/api/v1/conversations")
    async def control_conversations(
        workspace_id: str | None = None, channel: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        return [asdict(item) for item in store.list_conversations(
            workspace_id=workspace_id, channel=channel, limit=limit
        )]

    @app.get("/api/v1/conversations/{conversation_id}")
    async def control_conversation(conversation_id: str, limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        try:
            conversation = store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "conversation": asdict(conversation),
            "messages": [asdict(item) for item in store.list_messages(conversation_id, limit=limit)],
            "runs": [_run_json(item) for item in store.list_runs(conversation_id=conversation_id)],
        }

    @app.get("/api/v1/runs")
    async def control_runs(
        status: str | None = None, conversation_id: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        statuses = (status,) if status else None
        return [_run_json(item) for item in store.list_runs(
            conversation_id=conversation_id, statuses=statuses
        )[-limit:]]

    @app.get("/api/v1/runs/{run_id}/timeline")
    async def control_run_timeline(run_id: str, after: int = Query(default=0, ge=0)) -> dict[str, Any]:
        try:
            run = store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"run": _run_json(run), "events": [
            _event_json(item) for item in store.list_events(run_id, after_sequence=after)
        ]}

    @app.get("/api/v1/runs/{run_id}/artifacts")
    async def control_run_artifacts(run_id: str) -> list[dict[str, Any]]:
        try:
            return [
                {**asdict(item), "content": None}
                for item in store.list_artifacts(run_id)
            ]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/artifacts/{artifact_id}")
    async def control_artifact(artifact_id: str) -> dict[str, Any]:
        try:
            return asdict(store.get_artifact(artifact_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/audit")
    async def control_audit(after: int = Query(default=0, ge=0), limit: int = Query(default=200, ge=1, le=1000)) -> list[dict[str, Any]]:
        return [asdict(item) for item in store.list_audit(after_id=after, limit=limit)]

    @app.websocket("/ws/control")
    async def control_stream(websocket: WebSocket, after: int = Query(default=0, ge=0)) -> None:
        if not _websocket_authorized(websocket, configured.control_token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        cursor = after
        await websocket.send_json({
            "type": "control.snapshot",
            "cursor": cursor,
            "state": control_snapshot(include_activity=False),
        })
        version = bus.global_version()
        try:
            while True:
                events = store.list_events_global(after_id=cursor, limit=1000)
                for event in events:
                    await websocket.send_json({
                        "type": "control.event", "cursor": event.id,
                        "event": _event_json(event),
                    })
                    cursor = event.id
                version = await bus.wait_global(version)
        except WebSocketDisconnect:
            return

    @app.websocket("/ws/runs/{run_id}")
    async def run_stream(
        websocket: WebSocket, run_id: str, after: int = Query(default=0, ge=0)
    ) -> None:
        if not _websocket_authorized(websocket, configured.control_token):
            await websocket.close(code=4401)
            return
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


def _identity_json(document: Any) -> dict[str, Any]:
    return {
        "name": document.name,
        "content": document.content,
        "sha256": document.sha256,
    }


def _event_json(event: PersistedEvent) -> dict[str, Any]:
    return _redact_control(asdict(event))


def _redact_control(value: Any, *, key: str = "") -> Any:
    """Keep inspector payloads useful without exposing secret-shaped values."""
    lowered = key.casefold()
    if any(marker in lowered for marker in ("token", "secret", "password", "credential", "api_key")):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(k): _redact_control(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_control(item, key=key) for item in value]
    return value


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


def _bearer_token(header: str | None) -> str | None:
    if not header:
        return None
    scheme, separator, value = header.partition(" ")
    if not separator or scheme.casefold() != "bearer" or not value.strip():
        return None
    return value.strip()


def _websocket_authorized(websocket: WebSocket, expected: str | None) -> bool:
    if expected is None:
        return True
    provided = _bearer_token(websocket.headers.get("authorization"))
    if provided is None:
        # Browser WebSocket clients cannot set Authorization headers. The
        # short-lived token is accepted as a query parameter for this local,
        # single-user control plane; deployments should prefer a same-origin
        # cookie or a reverse proxy that injects the header.
        provided = websocket.query_params.get("access_token")
    return _credential_authorized(
        provided, websocket.cookies.get("gravityclaw_session"), expected
    )


def _credential_authorized(
    bearer: str | None, session: str | None, expected: str | None
) -> bool:
    if expected is None:
        return True
    if bearer is not None and hmac.compare_digest(bearer, expected):
        return True
    if session is None:
        return False
    try:
        issued, expires, signature = session.split(".", 2)
        payload = f"{issued}.{expires}"
        if int(expires) < int(time.time()):
            return False
        expected_signature = hmac.new(
            expected.encode("utf-8"), payload.encode("ascii"), "sha256"
        ).hexdigest()
        return hmac.compare_digest(signature, expected_signature)
    except (ValueError, UnicodeEncodeError):
        return False


def _session_cookie(secret: str, *, now: int | None = None) -> str:
    issued = int(time.time()) if now is None else now
    expires = issued + 12 * 60 * 60
    payload = f"{issued}.{expires}"
    signature = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), "sha256").hexdigest()
    return f"{payload}.{signature}"


def _control_token_from_environment() -> str | None:
    direct = os.environ.get("GRAVITYCLAW_CONTROL_TOKEN")
    secret_file = os.environ.get("GRAVITYCLAW_CONTROL_TOKEN_FILE")
    if direct and secret_file:
        raise ValueError(
            "set only one of GRAVITYCLAW_CONTROL_TOKEN or "
            "GRAVITYCLAW_CONTROL_TOKEN_FILE"
        )
    if not secret_file:
        return direct
    data = Path(secret_file).read_bytes()
    if len(data) > 4096:
        raise ValueError("control token file is unexpectedly large")
    token = data.decode("utf-8").strip()
    if not token:
        raise ValueError("control token file is empty")
    return token
