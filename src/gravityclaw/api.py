"""FastAPI control plane for GravityClaw Core."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

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
from .store import PersistedEvent, RunRecord, Store, TERMINAL_RUN_STATUSES


@dataclass(frozen=True, slots=True)
class Settings:
    home: Path
    mode: str = "agy"
    worker_image: str | None = None
    poll_interval: float = 0.2

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


class MemoryCreate(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)
    source: str = Field(min_length=1, max_length=500)
    conversation_id: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


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
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        report = await manager.start()
        app.state.reconciliation = asdict(report)
        try:
            yield
        finally:
            await manager.close()

    app = FastAPI(title="GravityClaw Core", version="0.3.0", lifespan=lifespan)
    app.state.settings = configured
    app.state.store = store
    app.state.event_bus = bus
    app.state.manager = manager
    app.state.identity = identity
    app.state.memory = memory

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": configured.mode,
            "reconciliation": app.state.reconciliation,
        }

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
