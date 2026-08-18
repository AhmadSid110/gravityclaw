"""FastAPI control plane for GravityClaw Core."""

from __future__ import annotations

import json
import logging
import os
import hmac
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.staticfiles import StaticFiles

from .channel_store import ChannelStore
from .channel_runtime import ChannelRuntime
from .capabilities import CapabilityManager
from .config import RELEASE_VERSION, RuntimeLayout, load_config, read_secret_file
from .context import ContextBuilder, PROFILES, RunContextCompiler
from .event_bus import EventBus
from .execution import (
    AgyContainerSpecFactory,
    AgyHostSpecFactory,
    FakeContainerSpecFactory,
    FakeHostSpecFactory,
    HostExecutionBackend,
    PodmanExecutionBackend,
)
from .identity import IdentityStore
from .learning import LearningEngine, LearningEligibilityGate
from .manager import RunManager
from .memory import MemoryService
from .models import AgyModelCatalog
from .scheduler import Scheduler
from .store import PersistedEvent, RunRecord, Store, TERMINAL_RUN_STATUSES, VersionConflict
from .attachments import (
    AttachmentRecord,
    AttachmentResolver,
    AttachmentService,
    AttachmentStorage,
    AttachmentStore,
)
from .goals import GoalEvaluator
from .harness import HarnessCompiler
from .telegram import TelegramAdapter
from .learning_config import LearningConfig
from .learn_service import LearnChannel, LearnOptions, LearnRequest, LearnResponse, LearnService, parse_learn_command
from .curator_job import CuratorJob, CURATOR_SCHEDULE_PROMPT, ensure_curator_schedule
from .skills import (
    Curator,
    CuratorConfig,
    IngestionEngine,
    SkillService,
    TrustMode,
    TrustPolicy,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Settings:
    home: Path
    identity_home: Path | None = None
    capability_home: Path | None = None
    memory_home: Path | None = None
    mode: str = "agy"
    target: str = "host"
    sandbox: bool = False
    policy_mode: str = "balanced"
    allow_normal_commands: bool = True
    require_approval_for_elevated: bool = True
    worker_image: str | None = None
    agy_binary: str = "agy"
    agy_models: tuple[str, ...] = ()
    agy_default_model: str | None = None
    poll_interval: float = 0.2
    telegram_token: str | None = field(default=None, repr=False)
    telegram_user_id: str | None = None
    telegram_default_workspace: str | None = None
    telegram_api_root: str = "https://api.telegram.org"
    scheduler_poll_interval: float = 1.0
    secret_dir: Path | None = field(default=None, repr=False)
    control_token: str | None = field(default=None, repr=False)
    cookie_secure: bool = False
    frontend_dir: Path | None = field(default=None, repr=False)
    learning_enabled: bool = True
    learning_toml: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def database(self) -> Path:
        return self.home / "gravityclaw.db"

    @property
    def identity_root(self) -> Path:
        return self.identity_home or self.home

    @property
    def capability_root(self) -> Path:
        return self.capability_home or self.home

    @property
    def memory_root(self) -> Path:
        return self.memory_home or self.home

    @classmethod
    def from_environment(cls) -> "Settings":
        config_path = os.environ.get("GRAVITYCLAW_CONFIG")
        if config_path is None and "GRAVITYCLAW_HOME" not in os.environ:
            candidate = RuntimeLayout.for_user().config_file
            if candidate.exists():
                config_path = str(candidate)
        config: dict[str, Any] = {}
        layout: RuntimeLayout | None = None
        if config_path:
            config = load_config(Path(config_path))
            layout = RuntimeLayout.for_user()
            data_path = Path(config.get("database", {}).get("path", layout.database)).expanduser()
            home = data_path.parent.resolve()
            identity_home = layout.identity_dir
            capability_home = layout.capability_dir
            memory_home = home
            secret_dir = layout.secret_dir
        else:
            home = Path(os.environ.get("GRAVITYCLAW_HOME", str(Path.home() / ".gravityclaw"))).resolve()
            identity_home = capability_home = memory_home = secret_dir = None
        execution = config.get("execution", {})
        server = config.get("server", {})
        control = config.get("control", {})
        telegram = config.get("telegram", {})
        scheduler = config.get("scheduler", {})
        learning = config.get("learning", {})
        control_file = control.get("token_file") if isinstance(control, dict) else None
        telegram_file = telegram.get("token_file") if isinstance(telegram, dict) else None
        control_token = _read_config_secret(control_file, required=False)
        telegram_token = _read_config_secret(telegram_file, required=bool(telegram.get("enabled")))
        policy = execution.get("policy", {}) if isinstance(execution, dict) else {}
        return cls(
            home=home,
            identity_home=identity_home,
            capability_home=capability_home,
            memory_home=memory_home,
            mode=os.environ.get("GRAVITYCLAW_MODE", execution.get("mode", "agy")),
            target=os.environ.get("GRAVITYCLAW_EXECUTION_TARGET", execution.get("target", "host")),
            sandbox=os.environ.get("GRAVITYCLAW_SANDBOX", str(bool(execution.get("sandbox", False)))).lower() in {"1", "true", "yes"},
            policy_mode=os.environ.get("GRAVITYCLAW_POLICY_MODE", policy.get("mode", "balanced")),
            allow_normal_commands=bool(policy.get("allow_normal_commands", True)),
            require_approval_for_elevated=bool(policy.get("require_approval_for_elevated", True)),
            worker_image=os.environ.get("GRAVITYCLAW_WORKER_IMAGE", execution.get("worker_image")),
            agy_binary=os.environ.get("GRAVITYCLAW_AGY_BINARY", str(execution.get("agy_binary", "agy"))),
            agy_models=tuple(_model_list_from_environment(execution.get("models", []))),
            agy_default_model=os.environ.get("GRAVITYCLAW_AGY_DEFAULT_MODEL") or execution.get("default_model"),
            poll_interval=float(os.environ.get("GRAVITYCLAW_POLL_INTERVAL", "0.2")),
            telegram_token=_telegram_token_from_environment() or telegram_token,
            telegram_user_id=os.environ.get("GRAVITYCLAW_TELEGRAM_USER_ID") or telegram.get("allowed_user_id") or None,
            telegram_default_workspace=os.environ.get(
                "GRAVITYCLAW_TELEGRAM_DEFAULT_WORKSPACE", telegram.get("default_workspace") or None
            ),
            telegram_api_root=os.environ.get(
                "GRAVITYCLAW_TELEGRAM_API_ROOT", "https://api.telegram.org"
            ),
            scheduler_poll_interval=float(
                os.environ.get("GRAVITYCLAW_SCHEDULER_POLL_INTERVAL", str(scheduler.get("poll_interval", 1.0)))
            ),
            secret_dir=(Path(os.environ["GRAVITYCLAW_SECRET_DIR"]).resolve()
                        if os.environ.get("GRAVITYCLAW_SECRET_DIR") else secret_dir),
            control_token=_control_token_from_environment() or control_token,
            cookie_secure=os.environ.get("GRAVITYCLAW_COOKIE_SECURE", str(bool(control.get("cookie_secure", False)))).lower() in {"1", "true", "yes"},
            learning_enabled=os.environ.get(
                "GRAVITYCLAW_LEARNING_ENABLED",
                str(bool(learning.get("enabled", True) if isinstance(learning, dict) else True)),
            ).lower() in {"1", "true", "yes"},
            learning_toml=learning if isinstance(learning, dict) else {},
        )


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    path: str


class ConversationCreate(BaseModel):
    workspace_id: str
    channel: str = "web"
    channel_key: str | None = None
    title: str | None = None
    kind: str = Field(default="normal", pattern="^(main|normal)$")
    model_override: str | None = None


class ConversationUpdate(BaseModel):
    title: str | None = None
    model_override: str | None = None


class RunCreate(BaseModel):
    prompt: str = Field(min_length=1, max_length=2_000_000)
    scenario: str | None = None
    delay: float | None = None
    forbidden_path: str | None = None
    print_timeout: str = "120m"
    allow_all: bool = True
    context_profile: str = Field(default="chat", pattern="^(chat|coding|heartbeat|scheduled)$")
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)


class ModelUpdate(BaseModel):
    model: str | None = Field(default=None, max_length=200)


class EffortUpdate(BaseModel):
    effort: str | None = Field(default=None, pattern="^(low|medium|high)$")


class GlobalModelUpdate(BaseModel):
    model: str | None = Field(default=None, max_length=200)

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
    task: str = Field(min_length=1, max_length=2_000_000)
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
    prompt: str = Field(min_length=1, max_length=2_000_000)
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


class ScheduleUpdate(ScheduleCreate):
    expected_version: int | None = Field(default=None, ge=1)


class RunNowRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=200)


class CapabilityMutation(BaseModel):
    expected_updated_at: str | None = None


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


class GoalCreate(BaseModel):
    conversation_id: str
    objective: str = Field(min_length=1, max_length=4000)
    acceptance: list[dict[str, Any]] = Field(default_factory=list)
    max_turns: int = Field(default=12, ge=1, le=100)


class SessionLogin(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class ProposalAction(BaseModel):
    reason: str | None = None


class RollbackBody(BaseModel):
    target_revision: int = Field(ge=1)
    reason: str = ""


class MemoryPatch(BaseModel):
    content: str = Field(min_length=1, max_length=100_000)


class LearningSettingsUpdate(BaseModel):
    enabled: bool | None = None
    trust_mode: str | None = Field(default=None, pattern="^(strict|balanced|autonomous)$")
    min_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    curator_enabled: bool | None = None
    notifications_mode: str | None = Field(default=None, pattern="^(silent|normal|verbose)$")


class SPAStaticFiles(StaticFiles):
    """Serve the production console while preserving API 404 semantics."""

    def __init__(self, directory: Path) -> None:
        super().__init__(directory=str(directory), html=True, check_dir=True)
        self._index_path = directory / "index.html"
        self._directory = directory

    async def get_response(self, path: str, scope: dict[str, Any]) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or scope.get("method") not in {"GET", "HEAD"}:
                raise
            # Support reverse-proxy sub-path deployment: if the path contains
            # an /assets/ segment and the suffix resolves to a real file, serve
            # it directly. This allows /gravityclaw/assets/index-xxx.js to
            # resolve when Tailscale Serve forwards the full path.
            if "/assets/" in path:
                relative = "assets/" + path.rsplit("/assets/", 1)[-1]
                if (self._directory / relative).is_file():
                    return await super().get_response(relative, scope)
            # Similarly, handle favicon.svg and other root-level static files
            # accessed under a sub-path prefix.
            basename = path.rsplit("/", 1)[-1]
            if basename in {"favicon.svg", "manifest.webmanifest"} and (self._directory / basename).is_file():
                return await super().get_response(basename, scope)
            first_segment = path.split("/", 1)[0]
            if first_segment in {"api", "auth", "health"}:
                raise
            if "." in Path(path).name:
                # If this looks like a static file that wasn't found, raise 404
                # unless it could be a prefixed SPA route (no file extension).
                name = Path(path).name
                if any(name.endswith(ext) for ext in (".js", ".css", ".map", ".svg", ".png", ".ico", ".woff2", ".woff", ".ttf")):
                    raise
                # Fallthrough: serve index.html for SPA routes like /gravityclaw/conversations
            return FileResponse(self._index_path)


def _frontend_directory(settings: Settings) -> Path | None:
    candidates = (
        settings.frontend_dir,
        Path(__file__).with_name("web_dist"),
        Path(__file__).resolve().parents[2] / "web" / "dist",
    )
    for candidate in candidates:
        if candidate is not None and (candidate / "index.html").is_file():
            return candidate
    return None


def _is_public_frontend_request(request: Request, frontend_directory: Path | None) -> bool:
    """Allow the browser to load the shell without exposing control APIs."""
    if frontend_directory is None or request.method not in {"GET", "HEAD"}:
        return False
    path = request.url.path.rstrip("/") or "/"
    if path in {"/", "/index.html"} or path.startswith("/assets/"):
        return True
    if path in {"/favicon.svg", "/manifest.webmanifest"}:
        return True
    # Support reverse-proxy sub-path deployment (e.g. /gravityclaw/assets/...)
    # by checking if the path contains /assets/ and the resolved file exists.
    if "/assets/" in path:
        relative = path.rsplit("/assets/", 1)[-1]
        if (frontend_directory / "assets" / relative).is_file():
            return True
    # Allow favicon/manifest accessed under a sub-path prefix.
    basename = path.rsplit("/", 1)[-1]
    if basename in {"favicon.svg", "manifest.webmanifest"} and (frontend_directory / basename).is_file():
        return True
    if path in {
        "/api", "/auth", "/health", "/docs", "/redoc", "/openapi.json",
        "/identity", "/memories", "/journals", "/schedules", "/workspace-aliases",
        "/workspaces", "/capabilities",
    } or path.startswith((
        "/api/", "/auth/", "/health/", "/runs/", "/schedules/", "/identity/",
        "/memories/", "/journals/", "/workspace-aliases/", "/workspaces/",
        "/capabilities/",
    )):
        return False
    if path.startswith("/conversations/") and path.rsplit("/", 1)[-1] in {
        "runs", "context-watermark", "summaries",
    }:
        return False
    return "." not in Path(path).name or (frontend_directory / path.lstrip("/")).is_file()


class PrefixStrippingMiddleware:
    """Strip reverse-proxy subpath prefixes (e.g. /gravityclaw) from ASGI scope paths."""

    def __init__(self, app: Any, prefix: str = "/gravityclaw") -> None:
        self.app = app
        self.prefix = prefix.rstrip("/")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] in {"http", "websocket"}:
            path = scope.get("path", "")
            if path == self.prefix:
                scope["path"] = "/"
                scope["raw_path"] = b"/"
            elif path.startswith(self.prefix + "/"):
                new_path = path[len(self.prefix):] or "/"
                scope["path"] = new_path
                scope["raw_path"] = new_path.encode("latin-1")
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_environment()
    configured.home.mkdir(mode=0o700, parents=True, exist_ok=True)
    configured.home.chmod(0o700)
    store = Store(configured.database)
    identity = IdentityStore(configured.identity_root, runtime_home=configured.home)
    identity.bootstrap()
    # initialize before constructing services used by dispatch/retrieval
    store.initialize()
    catalog = AgyModelCatalog(
        configured.agy_binary, configured.agy_models, configured.agy_default_model
    )
    store.set_model_catalog(catalog.snapshot())
    store.ensure_model_default(catalog.default_model)
    with store._connect() as connection:
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES('agy_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (catalog.version,),
        )
    identity_lock = threading.Lock()
    memory = MemoryService(configured.memory_root, store)
    channel_store = ChannelStore(store)
    capabilities = CapabilityManager(
        configured.capability_root, store, secret_dir=configured.secret_dir
    )
    context_compiler = RunContextCompiler(
        store, identity, memory, ContextBuilder(),
        harness_compiler=HarnessCompiler(),
    )
    bus = EventBus()
    if configured.target == "host" and not configured.sandbox:
        backend = HostExecutionBackend()
        if configured.mode == "fake":
            factory = FakeHostSpecFactory()
        elif configured.mode == "agy":
            factory = AgyHostSpecFactory(binary=configured.agy_binary)
        else:
            raise ValueError(f"unsupported GravityClaw mode: {configured.mode}")
    else:
        backend = PodmanExecutionBackend()
        if configured.mode == "fake":
            factory = FakeContainerSpecFactory(
                configured.worker_image or "localhost/gravityclaw-test-worker:latest"
            )
        elif configured.mode == "agy":
            factory = AgyContainerSpecFactory(
                configured.worker_image or "localhost/gravityclaw-agy:1.1.13",
                binary=configured.agy_binary,
            )
        else:
            raise ValueError(f"unsupported GravityClaw mode: {configured.mode}")
    # Initialize attachment subsystem early so RunManager can use it for mounts
    attachment_storage = AttachmentStorage(configured.home)
    attachment_store = AttachmentStore(store)
    attachment_service = AttachmentService(attachment_store, attachment_storage)
    attachment_resolver = AttachmentResolver()
    manager = RunManager(
        store,
        backend,
        factory,
        bus,
        poll_interval=configured.poll_interval,
        context_compiler=context_compiler,
        capability_manager=capabilities,
        goal_evaluator=GoalEvaluator(store),
        learning_engine=LearningEngine(
            store, identity, memory,
            enabled=configured.learning_enabled,
        ),
        attachment_store=attachment_store,
        attachment_resolver=attachment_resolver,
        attachment_storage=attachment_storage,
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
            attachment_service=attachment_service,
        )

    # ─── Phase 3.1: Learning subsystem wiring ────────────────────────────────
    learning_config = LearningConfig.from_environment(configured.learning_toml)
    trust_policy = TrustPolicy(mode=TrustMode(learning_config.skills.trust_mode))
    skill_service = SkillService(
        store, configured.home,
        create_approval_required=learning_config.skills.create_approval_required,
        modify_approval_required=learning_config.skills.modify_approval_required,
        trust_policy=trust_policy,
    )
    ingestion_engine = IngestionEngine(
        skill_service.registry, store, configured.home,
    )
    learn_service = LearnService(
        ingestion_engine, trust_policy, store, learning_config,
    )
    curator_config = CuratorConfig(
        enabled=learning_config.curator.enabled,
        stale_after_days=learning_config.curator.stale_after_days,
        archive_after_days=learning_config.curator.archive_after_days,
        minimum_invocations=learning_config.curator.minimum_invocations,
        min_idle_hours=learning_config.curator.min_idle_hours,
        utility_stale_threshold=learning_config.curator.utility_stale_threshold,
        utility_archive_threshold=learning_config.curator.utility_archive_threshold,
    )
    curator = Curator(skill_service.registry, store, trust_policy, curator_config)
    curator_job = CuratorJob(curator, store, learning_config.curator)
    # Wire skill_service into the learning engine
    if manager.learning_engine is not None:
        manager.learning_engine.skill_service = skill_service

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        report = await manager.start()
        app.state.reconciliation = asdict(report)
        scheduler_report = await scheduler.start()
        app.state.scheduler_reconciliation = asdict(scheduler_report)
        # Register curator schedule after scheduler is started and workspaces exist
        if learning_config.curator.enabled:
            try:
                workspaces = store.list_workspaces()
                if workspaces:
                    ensure_curator_schedule(store, workspaces[0].id, learning_config.curator)
            except Exception as exc:
                LOGGER.warning("curator schedule registration failed: %s", exc)
        if channel_runtime is not None:
            await channel_runtime.start()
        try:
            yield
        finally:
            if channel_runtime is not None:
                await channel_runtime.close()
            await scheduler.close()
            await manager.close()

    app = FastAPI(title="GravityClaw Control Plane", version=RELEASE_VERSION, lifespan=lifespan)
    app.add_middleware(PrefixStrippingMiddleware, prefix="/gravityclaw")
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
    app.state.learning_config = learning_config
    app.state.learn_service = learn_service
    app.state.skill_service = skill_service
    app.state.curator_job = curator_job
    app.state.trust_policy = trust_policy
    # Attachment service was initialized earlier; just expose on app state
    app.state.attachment_service = attachment_service
    app.state.attachment_resolver = attachment_resolver
    frontend_directory = _frontend_directory(configured)
    app.state.frontend_dir = frontend_directory

    @app.middleware("http")
    async def control_auth(request: Request, call_next: Any) -> Any:
        path = request.url.path.rstrip("/")
        public = path in {"/health", "/auth/session", "/api/v1/auth/session"}
        public_frontend = _is_public_frontend_request(request, frontend_directory)
        if configured.control_token is not None and not public and not public_frontend:
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
    @app.get("/api/v1/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "mode": configured.mode,
            "telegram": {"enabled": channel_runtime is not None},
            "reconciliation": app.state.reconciliation,
            "scheduler": app.state.scheduler_reconciliation,
        }

    @app.post("/auth/session")
    @app.post("/api/v1/auth/session")
    async def create_browser_session(body: SessionLogin, response: Response) -> dict[str, Any]:
        if configured.control_token is None:
            raise HTTPException(status_code=503, detail="control authentication is disabled")
        if not hmac.compare_digest(body.token, configured.control_token):
            raise HTTPException(status_code=401, detail="invalid control token")
        response.set_cookie(
            "gravityclaw_session", _session_cookie(configured.control_token),
            max_age=12 * 60 * 60, httponly=True,
            secure=configured.cookie_secure,
            samesite="lax", path="/",
        )
        return {"authenticated": True, "expires_in": 12 * 60 * 60}

    @app.get("/auth/session")
    @app.get("/api/v1/auth/session")
    async def browser_session(request: Request) -> dict[str, Any]:
        authenticated = configured.control_token is None or _credential_authorized(
            _bearer_token(request.headers.get("authorization")),
            request.cookies.get("gravityclaw_session"), configured.control_token,
        )
        return {"authenticated": authenticated}

    @app.delete("/auth/session")
    @app.delete("/api/v1/auth/session")
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
            values = body.model_dump()
            values["server_id"] = values.pop("id")
            server = capabilities.register_mcp(**values)
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
    @app.post("/api/v1/conversations", status_code=201)
    async def create_conversation(body: ConversationCreate, request: Request) -> dict[str, Any]:
        try:
            if body.kind == "main":
                record = store.ensure_main_conversation(body.workspace_id)
            else:
                record = store.create_conversation(
                    body.workspace_id,
                    channel=body.channel,
                    channel_key=body.channel_key,
                    title=body.title,
                    kind=body.kind,
                    model_override=body.model_override,
                )
            store.record_audit(actor=request.state.actor, action="conversation.create",
                               resource_type="conversation", resource_id=record.id,
                               payload={"workspace_id": record.workspace_id, "channel": record.channel, "kind": record.kind})
            return _conversation_json(record, store.get_model_default())
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/models")
    @app.get("/api/v1/models")
    async def list_models() -> dict[str, Any]:
        from .quota import fetch_agy_quota

        value = store.get_model_catalog()
        value["default_model"] = store.get_model_default()
        value["effort_levels"] = ["low", "medium", "high"]

        # Enrich the models list with live AGY models from the quota RPC.
        # This replaces the static config-only list with the actual available models.
        try:
            quota_data = await fetch_agy_quota()
            if quota_data.get("available") and quota_data.get("models"):
                # Build deduplicated list of AGY models (strip effort suffixes for display)
                agy_models: list[dict[str, str]] = []
                seen_ids: set[str] = set()
                for m in quota_data["models"]:
                    mid = m["id"]
                    if mid not in seen_ids:
                        seen_ids.add(mid)
                        agy_models.append({"id": mid, "label": m["label"]})
                value["models"] = agy_models
        except Exception:
            pass  # Fall back to configured models on error

        return value

    @app.put("/api/models/default")
    @app.put("/api/v1/models/default")
    async def update_global_model(body: GlobalModelUpdate, request: Request) -> dict[str, Any]:
        try:
            model = catalog.validate(body.model)
            if model is not None and model not in {item["id"] for item in store.get_model_catalog().get("models", [])}:
                raise ValueError(f"model is not available from the server: {model}")
            store.set_model_default(model)
            store.record_audit(actor=request.state.actor, action="model.default.update", resource_type="model", payload={"model": model})
            return {"default_model": model, "models": store.get_model_catalog().get("models", []), "agy_version": catalog.version}
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/conversations/{conversation_id}/model")
    @app.get("/api/v1/conversations/{conversation_id}/model")
    async def get_conversation_model(conversation_id: str) -> dict[str, Any]:
        try:
            conversation = store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _conversation_model_json(conversation, store.get_model_default())

    @app.put("/api/conversations/{conversation_id}/model")
    @app.put("/api/v1/conversations/{conversation_id}/model")
    async def update_conversation_model(conversation_id: str, body: ModelUpdate, request: Request) -> dict[str, Any]:
        try:
            model = body.model.strip() if body.model else None
            if model == "":
                model = None
            conversation = store.set_conversation_model(conversation_id, model)
            store.record_audit(actor=request.state.actor, action="conversation.model.update", resource_type="conversation", resource_id=conversation_id, payload={"model": model})
            return _conversation_model_json(conversation, store.get_model_default())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/conversations/{conversation_id}/effort")
    @app.get("/api/v1/conversations/{conversation_id}/effort")
    async def get_conversation_effort(conversation_id: str) -> dict[str, Any]:
        try:
            conversation = store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"conversation_id": conversation.id, "effort": conversation.effort_override}

    @app.put("/api/conversations/{conversation_id}/effort")
    @app.put("/api/v1/conversations/{conversation_id}/effort")
    async def update_conversation_effort(conversation_id: str, body: EffortUpdate, request: Request) -> dict[str, Any]:
        try:
            conversation = store.set_conversation_effort(conversation_id, body.effort)
            store.record_audit(actor=request.state.actor, action="conversation.effort.update", resource_type="conversation", resource_id=conversation_id, payload={"effort": body.effort})
            return {"conversation_id": conversation.id, "effort": conversation.effort_override}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/usage")
    @app.get("/api/v1/usage")
    async def get_usage(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
        return store.get_usage_summary(days=days)

    @app.get("/api/quota")
    @app.get("/api/v1/quota")
    async def get_agy_quota() -> dict[str, Any]:
        """Query the running AGY process's local Connect RPC for live quota data."""
        from .quota import fetch_agy_quota
        return await fetch_agy_quota()

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

    # ─── Attachment API (Phase 7A) ───────────────────────────────────────────

    @app.post("/api/v1/conversations/{conversation_id}/attachments", status_code=201)
    async def upload_attachment(
        conversation_id: str, file: UploadFile, request: Request
    ) -> dict[str, Any]:
        """Upload a file and create an attachment record."""
        try:
            conversation = store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        filename = file.filename or "upload"
        import io as _io
        content = await file.read()
        data = _io.BytesIO(content)
        try:
            record = attachment_service.ingest(
                workspace_id=conversation.workspace_id,
                conversation_id=conversation_id,
                filename=filename,
                data=data,
                source="web",
                mime_type_hint=file.content_type,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.record_audit(
            actor=request.state.actor, action="attachment.upload",
            resource_type="attachment", resource_id=record.id,
            payload={"conversation_id": conversation_id, "filename": record.filename},
        )
        return _attachment_json(record)

    @app.get("/api/v1/conversations/{conversation_id}/attachments")
    async def list_conversation_attachments(conversation_id: str) -> list[dict[str, Any]]:
        try:
            store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        records = attachment_service.list_for_conversation(conversation_id)
        return [_attachment_json(r) for r in records]

    @app.get("/api/v1/attachments/{attachment_id}")
    async def get_attachment(attachment_id: str) -> dict[str, Any]:
        try:
            record = attachment_service.get(attachment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _attachment_json(record)

    @app.get("/api/v1/attachments/{attachment_id}/download")
    async def download_attachment(attachment_id: str) -> FileResponse:
        try:
            record = attachment_service.get(attachment_id)
            path = attachment_service.resolve_path(attachment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Attachment file not found on disk")
        return FileResponse(
            path=str(path),
            media_type=record.mime_type,
            filename=record.filename,
        )

    @app.get("/api/v1/messages/{message_id}/attachments")
    async def list_message_attachments(message_id: str) -> list[dict[str, Any]]:
        records = attachment_service.list_for_message(message_id)
        return [_attachment_json(r) for r in records]

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
    async def inspect_run_context(run_id: str) -> Any:
        try:
            store.get_run(run_id)
            return store.get_context_manifest(run_id)
        except KeyError as exc:
            if "manifest not found" in str(exc):
                return Response(status_code=204)
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/capabilities")
    @app.get("/api/v1/runs/{run_id}/capabilities")
    async def inspect_run_capabilities(run_id: str) -> Any:
        try:
            store.get_run(run_id)
            return store.get_capability_manifest(run_id)
        except KeyError as exc:
            if "manifest not found" in str(exc):
                return Response(status_code=204)
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/context-snapshot")
    @app.get("/api/v1/runs/{run_id}/context-snapshot")
    async def get_run_context_snapshot(run_id: str) -> Any:
        """Phase 4C: Full context transparency snapshot for a run.

        Returns the persisted context snapshot with segment breakdown,
        loaded skills, retrieved memories, and token accounting.
        If no snapshot exists, attempts to synthesize one from the context manifest.
        """
        try:
            run = store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        # Try persisted snapshot first
        snapshot = store.get_context_snapshot(run_id)
        if snapshot is not None:
            # Enrich with computed fields
            context_limit = snapshot["context_limit"]
            input_tokens = snapshot["input_tokens"]
            usage_ratio = round(input_tokens / context_limit, 4) if context_limit > 0 else 0
            remaining = max(0, context_limit - input_tokens)
            return {
                **snapshot,
                "usage_ratio": usage_ratio,
                "remaining_tokens": remaining,
                "run_status": run.status,
                "is_final": snapshot["token_source"] == "provider",
                "is_estimated": snapshot["token_source"] != "provider",
            }

        # Fall back: synthesize from context manifest if available
        try:
            manifest = store.get_context_manifest(run_id)
        except KeyError:
            return Response(status_code=204)

        # Build a synthetic snapshot from the manifest
        budget = int(manifest.get("budget_tokens", 0) or 0)
        used = int(manifest.get("estimated_tokens", manifest.get("used_tokens", 0)) or 0)
        model = run.request.get("resolved_model") or run.request.get("model") or manifest.get("model", "unknown")
        all_sources = [s for s in manifest.get("sources", []) if isinstance(s, dict)]

        # Build segment breakdown
        segment_map: dict[str, int] = {}
        skills_list: list[dict[str, Any]] = []
        memories_list: list[dict[str, Any]] = []
        for source in all_sources:
            if not source.get("included", False):
                continue
            tokens = int(source.get("estimated_tokens", 0) or 0)
            category = str(source.get("category", "other"))
            # Map categories to segment kinds
            if category == "identity":
                kind = "system"
            elif category in ("history", "conversation_summary"):
                kind = "conversation"
            elif category == "tool_result":
                kind = "tool_results"
            elif category in ("curated_memory", "retrieved_memory"):
                kind = "memory"
                memories_list.append({
                    "id": source.get("label", ""),
                    "namespace": category.replace("_memory", ""),
                    "tokens": tokens,
                    "label": source.get("label"),
                    "confidence": source.get("confidence"),
                })
            elif category == "skill":
                kind = "skills"
                skills_list.append({
                    "skill_id": source.get("label", ""),
                    "name": source.get("label", ""),
                    "tokens": tokens,
                    "sha256": source.get("sha256"),
                })
            else:
                kind = "other"
            segment_map[kind] = segment_map.get(kind, 0) + tokens

        segments = [{"kind": k, "tokens": v} for k, v in segment_map.items()]
        usage_ratio = round(used / budget, 4) if budget > 0 else 0

        # Also check run_skill_context for richer skill data
        skill_context_raw = store.get_run_skill_context(run_id)
        if skill_context_raw and not skills_list:
            try:
                skill_ctx = json.loads(skill_context_raw)
                for entry in skill_ctx.get("loaded", []):
                    skills_list.append({
                        "skill_id": entry.get("skill_id", ""),
                        "name": entry.get("name", ""),
                        "revision": entry.get("revision"),
                        "tokens": entry.get("tokens", 0),
                    })
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "run_id": run_id,
            "model": model,
            "context_limit": budget,
            "input_tokens": used,
            "output_tokens": None,
            "token_source": "estimated",
            "segments": segments,
            "skills": skills_list,
            "memories": memories_list,
            "transformations": None,
            "conversation_tokens": segment_map.get("conversation"),
            "last_invocation_tokens": used,
            "created_at": run.created_at,
            "usage_ratio": usage_ratio,
            "remaining_tokens": max(0, budget - used),
            "run_status": run.status,
            "is_final": False,
            "is_estimated": True,
        }

    @app.post("/api/runs/{run_id}/context-snapshot")
    @app.post("/api/v1/runs/{run_id}/context-snapshot")
    async def save_run_context_snapshot(run_id: str, request: Request) -> dict[str, Any]:
        """Persist or update a context snapshot for a run.

        Called by the execution pipeline when context is compiled or
        when the provider returns authoritative token usage.
        """
        try:
            store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        body = await request.json()
        store.save_context_snapshot(
            run_id,
            model=body.get("model", "unknown"),
            context_limit=int(body.get("context_limit", 0)),
            input_tokens=int(body.get("input_tokens", 0)),
            output_tokens=body.get("output_tokens"),
            token_source=body.get("token_source", "estimated"),
            segments=body.get("segments"),
            skills=body.get("skills"),
            memories=body.get("memories"),
            transformations=body.get("transformations"),
            conversation_tokens=body.get("conversation_tokens"),
            last_invocation_tokens=body.get("last_invocation_tokens"),
        )
        return {"status": "saved", "run_id": run_id}

    @app.get("/conversations/{conversation_id}/context-watermark")
    async def inspect_context_watermark(conversation_id: str) -> dict[str, Any]:
        try:
            store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        watermark = store.get_context_watermark(conversation_id)
        return asdict(watermark) if watermark is not None else {}

    @app.get("/conversations/{conversation_id}/context-status")
    @app.get("/api/v1/conversations/{conversation_id}/context-status")
    async def conversation_context_status(conversation_id: str) -> dict[str, Any]:
        try:
            # Pass current profile budgets so the endpoint uses the live ceiling instead of stale stored values
            profile_budgets = {name: p.total_tokens for name, p in PROFILES.items()}
            return store.conversation_context_status(conversation_id, profile_budgets=profile_budgets)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

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

    @app.get("/api/v1/status")
    async def agent_status() -> dict[str, Any]:
        """Composite status card — no AGY call required."""
        snapshot = control_snapshot(include_activity=False)
        conversations = store.list_conversations()
        # Find the most recently active conversation
        active_conversation = None
        if conversations:
            active_conversation = max(conversations, key=lambda c: c.updated_at or c.created_at)

        # Context info from the active conversation
        context_info: dict[str, Any] = {"used_tokens": 0, "budget_tokens": 0, "percent": 0}
        if active_conversation:
            try:
                ctx_status = store.conversation_context_status(active_conversation.id, profile_budgets={name: p.total_tokens for name, p in PROFILES.items()})
                context_info = {
                    "used_tokens": ctx_status["used_tokens"],
                    "budget_tokens": ctx_status["budget_tokens"],
                    "percent": ctx_status["percent"],
                    "model": ctx_status.get("model"),
                    "context_profile": ctx_status.get("context_profile"),
                }
            except (KeyError, Exception):
                pass

        # Current/active run
        active_runs = snapshot.get("active_runs", [])
        run_info: dict[str, Any] | None = None
        if active_runs:
            latest_run = active_runs[0]
            started = latest_run.get("started_at") or latest_run.get("created_at", "")
            run_info = {
                "id": latest_run.get("id"),
                "status": latest_run.get("status"),
                "started_at": started,
            }

        # Scheduler — find next heartbeat
        schedules = store.list_schedules()
        next_heartbeat = None
        for schedule in schedules:
            if schedule.enabled and schedule.trigger_type == "heartbeat" and schedule.next_run_at:
                next_heartbeat = schedule.next_run_at
                break
        if not next_heartbeat:
            for schedule in schedules:
                if schedule.enabled and schedule.next_run_at:
                    if next_heartbeat is None or schedule.next_run_at < next_heartbeat:
                        next_heartbeat = schedule.next_run_at

        # Memory count
        memories = store.list_memories(limit=1000)
        memory_count = len(memories)

        # Workers/execution
        workers = store.list_workers()
        worker_healthy = any(item.state == "running" for item in workers) if workers else None

        # Active goal
        active_goal = None
        if active_conversation:
            goal = store.get_active_goal(active_conversation.id)
            if goal:
                active_goal = {
                    "id": goal.id,
                    "objective": goal.objective,
                    "status": goal.status,
                    "turns_used": goal.turns_used,
                    "max_turns": goal.max_turns,
                    "current_step": goal.current_step,
                }

        # Model from model policy or default
        resolved_model = context_info.get("model") or configured.agy_default_model

        return {
            "agent": {
                "status": snapshot["health"]["status"],
                "mode": snapshot["health"]["mode"],
            },
            "conversation": {
                "id": active_conversation.id if active_conversation else None,
                "title": active_conversation.title if active_conversation else None,
            },
            "model": resolved_model,
            "context": context_info,
            "run": run_info,
            "queue": snapshot["counts"]["queued_runs"],
            "memory": {"count": memory_count},
            "scheduler": {
                "next_run_at": next_heartbeat,
                "schedules_count": snapshot["counts"]["schedules"],
            },
            "execution": {
                "backend": configured.mode,
                "worker_healthy": worker_healthy,
                "worker_image": configured.worker_image,
            },
            "channels": {
                "telegram": snapshot["health"]["telegram"],
            },
            "goal": active_goal,
        }

    # ──────────────────────────────────────────────────────────────────
    # Goals
    # ──────────────────────────────────────────────────────────────────

    @app.get("/api/v1/goals")
    async def list_goals(
        conversation_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> list[dict[str, Any]]:
        statuses = (status,) if status else None
        goals = store.list_goals(conversation_id=conversation_id, statuses=statuses)
        return [asdict(item) for item in goals]

    @app.post("/api/v1/goals", status_code=201)
    async def create_goal(body: GoalCreate, request: Request) -> dict[str, Any]:
        try:
            goal = store.create_goal(
                conversation_id=body.conversation_id,
                objective=body.objective,
                acceptance=body.acceptance,
                max_turns=body.max_turns,
            )
            store.record_audit(
                actor=request.state.actor, action="goal.create",
                resource_type="goal", resource_id=goal.id,
                payload={"conversation_id": goal.conversation_id, "objective": goal.objective},
            )
            return asdict(goal)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/goals/{goal_id}")
    async def get_goal(goal_id: str) -> dict[str, Any]:
        try:
            goal = store.get_goal(goal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return asdict(goal)

    @app.post("/api/v1/goals/{goal_id}/pause")
    async def pause_goal(goal_id: str, request: Request) -> dict[str, Any]:
        try:
            goal = store.get_goal(goal_id)
            if goal.status != "active":
                raise HTTPException(status_code=409, detail=f"goal is {goal.status}, not active")
            goal = store.update_goal(goal_id, status="paused")
            store.record_audit(
                actor=request.state.actor, action="goal.pause",
                resource_type="goal", resource_id=goal_id, payload={},
            )
            return asdict(goal)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/goals/{goal_id}/resume")
    async def resume_goal(goal_id: str, request: Request) -> dict[str, Any]:
        try:
            goal = store.get_goal(goal_id)
            if goal.status != "paused":
                raise HTTPException(status_code=409, detail=f"goal is {goal.status}, not paused")
            goal = store.update_goal(goal_id, status="active")
            store.record_audit(
                actor=request.state.actor, action="goal.resume",
                resource_type="goal", resource_id=goal_id, payload={},
            )
            # Enqueue a continuation run to restart work
            from .goals import GoalEvaluator, EvaluationResult, build_continuation_prompt
            evaluation = EvaluationResult(
                verdict="continue",
                reason="goal resumed by user",
                acceptance_state=[],
            )
            prompt = build_continuation_prompt(goal, evaluation)
            run = await manager.submit(goal.conversation_id, {
                "prompt": prompt,
                "context_profile": "chat",
                "goal_id": goal.id,
                "goal_continuation": True,
            })
            return asdict(store.get_goal(goal_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/goals/{goal_id}/cancel")
    async def cancel_goal(goal_id: str, request: Request) -> dict[str, Any]:
        try:
            goal = store.get_goal(goal_id)
            if goal.status in ("completed", "cancelled"):
                raise HTTPException(status_code=409, detail=f"goal is already {goal.status}")
            goal = store.update_goal(goal_id, status="cancelled")
            store.record_audit(
                actor=request.state.actor, action="goal.cancel",
                resource_type="goal", resource_id=goal_id, payload={},
            )
            return asdict(goal)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/goals/{goal_id}/complete")
    async def complete_goal(goal_id: str, request: Request) -> dict[str, Any]:
        try:
            goal = store.get_goal(goal_id)
            if goal.status not in ("active", "paused"):
                raise HTTPException(status_code=409, detail=f"goal is {goal.status}")
            goal = store.update_goal(goal_id, status="completed")
            store.record_audit(
                actor=request.state.actor, action="goal.complete",
                resource_type="goal", resource_id=goal_id, payload={},
            )
            return asdict(goal)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/goals/{goal_id}/evaluations")
    async def list_goal_evaluations(goal_id: str) -> list[dict[str, Any]]:
        try:
            store.get_goal(goal_id)  # 404 if missing
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        evaluations = store.list_goal_evaluations(goal_id)
        return [asdict(item) for item in evaluations]

    @app.get("/api/v1/workspaces")
    async def control_workspaces() -> list[dict[str, Any]]:
        return [{**asdict(item), "path": str(item.path)} for item in store.list_workspaces()]

    def automation_json(schedule: Any, *, triggers: list[Any] | None = None) -> dict[str, Any]:
        value = asdict(schedule)
        if triggers is not None:
            value["triggers"] = [asdict(item) for item in triggers]
        return value

    @app.get("/api/v1/automations")
    async def control_automations(include_deleted: bool = False) -> list[dict[str, Any]]:
        return [
            automation_json(item, triggers=store.list_triggers(schedule_id=item.id, limit=20))
            for item in store.list_schedules(include_deleted=include_deleted)
        ]

    @app.get("/api/v1/automations/{schedule_id}")
    async def control_automation(schedule_id: str) -> dict[str, Any]:
        try:
            schedule = store.get_schedule(schedule_id, include_deleted=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return automation_json(schedule, triggers=store.list_triggers(schedule_id=schedule_id, limit=1000))

    @app.post("/api/v1/automations", status_code=201)
    async def control_create_automation(body: ScheduleCreate, request: Request) -> dict[str, Any]:
        values = body.model_dump()
        if values.get("context_profile") is None:
            values["context_profile"] = "heartbeat" if body.trigger_type == "heartbeat" else "scheduled"
        try:
            schedule = scheduler.create_schedule(**values)
            store.record_audit(
                actor=request.state.actor, action="automation.create",
                resource_type="schedule", resource_id=schedule.id,
                resulting_version=schedule.version,
                payload={"name": schedule.name, "trigger_type": schedule.trigger_type},
            )
            return automation_json(schedule, triggers=[])
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.put("/api/v1/automations/{schedule_id}")
    async def control_update_automation(schedule_id: str, body: ScheduleUpdate, request: Request) -> dict[str, Any]:
        values = body.model_dump(exclude={"expected_version"})
        if values.get("context_profile") is None:
            values["context_profile"] = "heartbeat" if body.trigger_type == "heartbeat" else "scheduled"
        try:
            schedule = scheduler.update_schedule(
                schedule_id, expected_version=body.expected_version, **values
            )
            store.record_audit(
                actor=request.state.actor, action="automation.update",
                resource_type="schedule", resource_id=schedule.id,
                expected_version=body.expected_version, resulting_version=schedule.version,
            )
            return automation_json(schedule, triggers=store.list_triggers(schedule_id=schedule.id, limit=20))
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/automations/{schedule_id}/enable")
    async def control_enable_automation(schedule_id: str, request: Request, body: VersionedMutation = VersionedMutation()) -> dict[str, Any]:
        try:
            schedule = store.set_schedule_enabled(schedule_id, True, expected_version=body.expected_version)
            store.record_audit(actor=request.state.actor, action="automation.enable", resource_type="schedule", resource_id=schedule_id, expected_version=body.expected_version, resulting_version=schedule.version)
            return automation_json(schedule)
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/automations/{schedule_id}/disable")
    async def control_disable_automation(schedule_id: str, request: Request, body: VersionedMutation = VersionedMutation()) -> dict[str, Any]:
        try:
            schedule = store.set_schedule_enabled(schedule_id, False, expected_version=body.expected_version)
            store.record_audit(actor=request.state.actor, action="automation.disable", resource_type="schedule", resource_id=schedule_id, expected_version=body.expected_version, resulting_version=schedule.version)
            return automation_json(schedule)
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/automations/{schedule_id}/run-now", status_code=202)
    async def control_run_automation_now(schedule_id: str, body: RunNowRequest, request: Request) -> dict[str, Any]:
        try:
            trigger, run = await scheduler.run_now(schedule_id, body.request_id)
            store.record_audit(actor=request.state.actor, action="automation.run_now", resource_type="schedule", resource_id=schedule_id, payload={"request_id": body.request_id, "trigger_id": trigger.id, "run_id": run.id if run else None})
            return {"trigger": asdict(trigger), "run": _run_json(run) if run else None}
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/capabilities")
    async def control_capabilities(workspace_id: str | None = None, profile: str = "coding") -> dict[str, Any]:
        workspaces = store.list_workspaces()
        selected = store.get_workspace(workspace_id) if workspace_id else (workspaces[0] if workspaces else None)
        if selected is None:
            return {"workspace": None, "skills": [], "mcp": [], "bindings": [], "snapshots": []}
        skills = capabilities.list_skills(selected.id)
        mcps = capabilities.list_mcp(selected.id)
        return {
            "workspace": {**asdict(selected), "path": str(selected.path)},
            "profile": profile,
            "isolation": {
                "worker": "container-backed", "workspace_rw": str(selected.path),
                "host_home": "inaccessible", "other_workspaces": "inaccessible",
                "network": "restricted by worker policy", "permission_profile": "allow-all inside worker",
            },
            "skills": [_skill_json(item) for item in skills],
            "mcp": [_mcp_json(item) for item in mcps],
            "bindings": capabilities.list_bindings(selected.id),
            "snapshots": store.list_capability_manifests(selected.id, limit=50),
        }

    @app.post("/api/v1/capabilities/skills/{skill_id}/enable")
    async def control_enable_skill(skill_id: str, request: Request, body: CapabilityMutation = CapabilityMutation()) -> dict[str, Any]:
        try:
            item = capabilities.set_skill_enabled(skill_id, True, expected_updated_at=body.expected_updated_at)
            store.record_audit(actor=request.state.actor, action="skill.enable", resource_type="skill", resource_id=skill_id)
            return _skill_json(item)
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/capabilities/skills/{skill_id}/disable")
    async def control_disable_skill(skill_id: str, request: Request, body: CapabilityMutation = CapabilityMutation()) -> dict[str, Any]:
        try:
            item = capabilities.set_skill_enabled(skill_id, False, expected_updated_at=body.expected_updated_at)
            store.record_audit(actor=request.state.actor, action="skill.disable", resource_type="skill", resource_id=skill_id)
            return _skill_json(item)
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/capabilities/mcp/{server_id}/enable")
    async def control_enable_mcp(server_id: str, request: Request, body: CapabilityMutation = CapabilityMutation()) -> dict[str, Any]:
        try:
            item = capabilities.set_mcp_enabled(server_id, True, expected_updated_at=body.expected_updated_at)
            return _mcp_json(item)
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/capabilities/mcp/{server_id}/disable")
    async def control_disable_mcp(server_id: str, request: Request, body: CapabilityMutation = CapabilityMutation()) -> dict[str, Any]:
        try:
            item = capabilities.set_mcp_enabled(server_id, False, expected_updated_at=body.expected_updated_at)
            return _mcp_json(item)
        except VersionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/v1/capabilities/mcp/{server_id}/health")
    async def control_health_mcp(server_id: str, request: Request) -> dict[str, Any]:
        try:
            item = capabilities.health_check(server_id)
            store.record_audit(actor=request.state.actor, action="mcp.health_check", resource_type="mcp", resource_id=server_id, payload={"health_state": item.health_state})
            return _mcp_json(item)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/v1/conversations/search")
    async def search_conversations(
        q: str = Query(min_length=1, max_length=500),
        workspace_id: str | None = None,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        return store.search_conversations(q, workspace_id=workspace_id, limit=limit)

    @app.get("/api/v1/conversations")
    async def control_conversations(
        workspace_id: str | None = None, channel: str | None = None,
        include_archived: bool = False,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        return [_conversation_json(item, store.get_model_default()) for item in store.list_conversations(
            workspace_id=workspace_id, channel=channel, include_archived=include_archived, limit=limit
        )]

    @app.get("/api/v1/conversations/{conversation_id}")
    async def control_conversation(conversation_id: str, limit: int = Query(default=200, ge=1, le=1000)) -> dict[str, Any]:
        try:
            conversation = store.get_conversation(conversation_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "conversation": _conversation_json(conversation, store.get_model_default()),
            "messages": [asdict(item) for item in store.list_messages(conversation_id, limit=limit)],
            "runs": [_run_json(item) for item in store.list_runs(conversation_id=conversation_id)],
        }

    @app.patch("/api/v1/conversations/{conversation_id}")
    async def patch_conversation(conversation_id: str, body: ConversationUpdate, request: Request) -> dict[str, Any]:
        try:
            record = store.update_conversation(
                conversation_id,
                title=body.title,
                model_override=body.model_override,
            )
            store.record_audit(actor=request.state.actor, action="conversation.update",
                               resource_type="conversation", resource_id=conversation_id,
                               payload={"title": body.title, "model_override": body.model_override})
            return _conversation_json(record, store.get_model_default())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/v1/conversations/{conversation_id}")
    async def delete_conversation(
        conversation_id: str,
        request: Request,
        permanent: bool = Query(default=False),
    ) -> dict[str, Any]:
        try:
            if permanent:
                store.delete_conversation(conversation_id)
                store.record_audit(actor=request.state.actor, action="conversation.delete_permanent",
                                   resource_type="conversation", resource_id=conversation_id)
                return {"deleted": True, "conversation_id": conversation_id}
            record = store.archive_conversation(conversation_id)
            store.record_audit(actor=request.state.actor, action="conversation.archive",
                               resource_type="conversation", resource_id=conversation_id,
                               payload={"archived_at": record.archived_at})
            return {"archived": True, "conversation": _conversation_json(record, store.get_model_default())}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/conversations/{conversation_id}/restore")
    async def restore_conversation(conversation_id: str, request: Request) -> dict[str, Any]:
        try:
            record = store.restore_conversation(conversation_id)
            store.record_audit(actor=request.state.actor, action="conversation.restore",
                               resource_type="conversation", resource_id=conversation_id)
            return {"restored": True, "conversation": _conversation_json(record, store.get_model_default())}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
    async def control_run_timeline(
        run_id: str,
        after: int = Query(default=0, ge=0),
        limit: int = Query(default=1000, ge=1, le=5000),
    ) -> dict[str, Any]:
        try:
            run = store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        events = store.list_events(run_id, after_sequence=after, limit=limit + 1)
        has_more = len(events) > limit
        events = events[:limit]
        return {
            "run": _run_json(run),
            "events": [_event_json(item) for item in events],
            "has_more": has_more,
            "next_after": events[-1].sequence if events else after,
        }

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

    # ─── Phase 3.1: Learning API routes ───────────────────────────────────────

    class LearnBody(BaseModel):
        source: str = Field(min_length=1, max_length=100_000)
        skill_name: str | None = None
        force_new: bool = False
        trust_override: str | None = None
        conversation_id: str | None = None
        run_id: str | None = None

    @app.post("/api/learning/learn", status_code=201)
    async def api_learn(body: LearnBody, request: Request) -> dict[str, Any]:
        req = LearnRequest(
            source=body.source,
            requested_by=request.state.actor,
            channel=LearnChannel.API,
            conversation_id=body.conversation_id,
            run_id=body.run_id,
            options=LearnOptions(
                skill_name=body.skill_name,
                force_new=body.force_new,
                trust_override=body.trust_override,
                title_hint=body.skill_name,
            ),
        )
        response = learn_service.learn(req)
        status_code = {
            "success": 201,
            "duplicate": 200,
            "failed": 422,
            "pending_approval": 202,
            "disabled": 503,
        }.get(response.status.value, 200)
        return JSONResponse(content=response.to_dict(), status_code=status_code)

    @app.get("/api/learning/config")
    async def api_learning_config() -> dict[str, Any]:
        return learning_config.to_dict()

    @app.get("/api/learning/curator/status")
    async def api_curator_status() -> dict[str, Any]:
        return curator_job.status()

    @app.post("/api/learning/curator/run")
    async def api_curator_run_now(request: Request) -> dict[str, Any]:
        report = curator_job.run(force=True)
        if report is None:
            return {"status": "skipped", "reason": "curator disabled or lock contention"}
        return {
            "status": "completed",
            "skills_evaluated": report.skills_evaluated,
            "actions_taken": len(report.actions_taken),
            "actions_blocked": len(report.actions_blocked),
        }

    # ─── Phase 4A: Learning Studio API ────────────────────────────────────────

    @app.get("/api/learning/overview")
    async def api_learning_overview() -> dict[str, Any]:
        """Overview: is learning enabled, stats, and what needs attention."""
        from dataclasses import asdict as _asdict
        skills = skill_service.registry.list_skills(limit=1000)
        pending_proposals = skill_service.registry.list_proposals(status="pending", limit=1000)
        memories_count = len(store.list_memories(limit=1000))

        # Compute success rate from telemetry
        total_executed = 0
        total_successful = 0
        total_corrected = 0
        for s in skills:
            stats = skill_service.registry.usage_stats(s.skill_id)
            total_executed += stats.get("executed", 0)
            total_successful += stats.get("successful", 0)
            total_corrected += stats.get("corrected", 0)

        success_rate = (
            round(total_successful / total_executed * 100, 1)
            if total_executed > 0 else None
        )

        return {
            "enabled": learning_config.enabled,
            "trust_mode": learning_config.skills.trust_mode,
            "stats": {
                "memories": memories_count,
                "skills": len(skills),
                "pending_proposals": len(pending_proposals),
                "success_rate": success_rate,
                "corrections": total_corrected,
            },
            "curator": curator_job.status(),
        }

    @app.get("/api/learning/events")
    async def api_learning_events(
        limit: int = Query(default=50, ge=1, le=500),
        after: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        """Recent learning activity from the audit trail."""
        all_audit = store.list_audit(after_id=after, limit=limit * 3)
        learning_events = [
            asdict(item) for item in all_audit
            if item.action.startswith("learning.") or item.action.startswith("skill.")
        ][:limit]
        return learning_events

    @app.get("/api/learning/proposals")
    async def api_learning_proposals(
        status: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        """List skill proposals with optional status filter."""
        proposals = skill_service.registry.list_proposals(
            status=status, limit=limit,
        )
        return [_proposal_json(p) for p in proposals]

    @app.get("/api/learning/proposals/{proposal_id}")
    async def api_learning_proposal_detail(proposal_id: str) -> dict[str, Any]:
        """Get full proposal detail including diff data."""
        try:
            proposal = skill_service.registry.get_proposal(proposal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _proposal_json(proposal)

    @app.post("/api/learning/proposals/{proposal_id}/approve")
    async def api_learning_proposal_approve(
        proposal_id: str, request: Request, body: ProposalAction = ProposalAction(),
    ) -> dict[str, Any]:
        """Approve a pending proposal — applies the operation."""
        from .skills.models import ProposalStatus, SkillOwner, SkillTrust
        try:
            proposal = skill_service.registry.get_proposal(proposal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if proposal.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"proposal already resolved: {proposal.status}",
            )

        # Check base revision for patches
        if proposal.operation == "patch" and proposal.skill_id:
            current = skill_service.registry.get_skill(proposal.skill_id)
            if proposal.base_revision is not None and current.revision != proposal.base_revision:
                skill_service.registry.resolve_proposal(
                    proposal_id, "conflict",
                    reason=f"base revision mismatch: expected {proposal.base_revision}, current {current.revision}",
                )
                raise HTTPException(
                    status_code=409,
                    detail=f"Base revision conflict: proposal targets rev {proposal.base_revision} but skill is at rev {current.revision}",
                )

        # Apply the operation
        if proposal.operation == "create":
            skill = skill_service.create(
                proposal.skill_name,
                proposal.description,
                proposal.content,
                owner=SkillOwner.AGENT,
                source_run_id=proposal.source_run_id,
            )
            skill_service.registry.resolve_proposal(
                proposal_id, "approved", reason=body.reason,
            )
        elif proposal.operation == "patch":
            skill_service.patch(
                proposal.skill_name,
                proposal.content,
                proposal.reason,
                source_run_id=proposal.source_run_id,
            )
            skill_service.registry.resolve_proposal(
                proposal_id, "approved", reason=body.reason,
            )
        elif proposal.operation == "archive":
            skill_service.archive(proposal.skill_name, reason=proposal.reason)
            skill_service.registry.resolve_proposal(
                proposal_id, "approved", reason=body.reason,
            )
        else:
            raise HTTPException(status_code=422, detail=f"unknown operation: {proposal.operation}")

        store.record_audit(
            actor=request.state.actor, action="learning.proposal.approved",
            resource_type="skill_proposal", resource_id=proposal_id,
            payload={"skill_name": proposal.skill_name, "operation": proposal.operation},
        )
        return _proposal_json(skill_service.registry.get_proposal(proposal_id))

    @app.post("/api/learning/proposals/{proposal_id}/reject")
    async def api_learning_proposal_reject(
        proposal_id: str, request: Request, body: ProposalAction = ProposalAction(),
    ) -> dict[str, Any]:
        """Reject a pending proposal."""
        try:
            proposal = skill_service.registry.get_proposal(proposal_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if proposal.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=f"proposal already resolved: {proposal.status}",
            )

        skill_service.registry.resolve_proposal(
            proposal_id, "rejected", reason=body.reason,
        )
        store.record_audit(
            actor=request.state.actor, action="learning.proposal.rejected",
            resource_type="skill_proposal", resource_id=proposal_id,
            payload={"skill_name": proposal.skill_name, "operation": proposal.operation},
        )
        return _proposal_json(skill_service.registry.get_proposal(proposal_id))

    @app.get("/api/learning/skills")
    async def api_learning_skills(
        state: str | None = Query(default=None),
        owner: str | None = Query(default=None),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        """List learned skills with telemetry stats."""
        skills = skill_service.registry.list_skills(
            owner=owner, state=state, limit=limit,
        )
        result = []
        for s in skills:
            stats = skill_service.registry.usage_stats(s.skill_id)
            executed = stats.get("executed", 0)
            successful = stats.get("successful", 0)
            result.append({
                **_learned_skill_json(s),
                "stats": {
                    "matched": stats.get("discovered", 0),
                    "selected": stats.get("selected", 0),
                    "loaded": stats.get("loaded", 0),
                    "executed": executed,
                    "successful": successful,
                    "failed": stats.get("failed", 0),
                    "corrected": stats.get("corrected", 0),
                    "success_rate": round(successful / executed * 100, 1) if executed > 0 else None,
                },
            })
        return result

    @app.get("/api/learning/skills/{skill_id}")
    async def api_learning_skill_detail(skill_id: str) -> dict[str, Any]:
        """Get a single skill with telemetry and content."""
        try:
            skill = skill_service.registry.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        stats = skill_service.registry.usage_stats(skill_id)
        content = skill_service.view(skill.name)
        executed = stats.get("executed", 0)
        successful = stats.get("successful", 0)

        return {
            **_learned_skill_json(skill),
            "content": content,
            "stats": {
                "matched": stats.get("discovered", 0),
                "selected": stats.get("selected", 0),
                "loaded": stats.get("loaded", 0),
                "executed": executed,
                "successful": successful,
                "failed": stats.get("failed", 0),
                "corrected": stats.get("corrected", 0),
                "success_rate": round(successful / executed * 100, 1) if executed > 0 else None,
            },
        }

    @app.get("/api/learning/skills/{skill_id}/revisions")
    async def api_learning_skill_revisions(
        skill_id: str,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        """Get revision history for a skill."""
        try:
            skill_service.registry.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        revisions = skill_service.registry.list_revisions(skill_id, limit=limit)
        return [_revision_json(r) for r in revisions]

    @app.get("/api/learning/skills/{skill_id}/runs")
    async def api_learning_skill_runs(
        skill_id: str,
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        """Get runs where this skill was used (from telemetry)."""
        try:
            skill_service.registry.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        # Get telemetry events that have run_ids
        with store._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT run_id, event, created_at
                   FROM skill_usage
                   WHERE skill_id=? AND run_id IS NOT NULL
                   ORDER BY created_at DESC LIMIT ?""",
                (skill_id, limit),
            ).fetchall()
        return [
            {"run_id": row["run_id"], "event": row["event"], "created_at": row["created_at"]}
            for row in rows
        ]

    @app.post("/api/learning/skills/{skill_id}/pin")
    async def api_learning_skill_pin(skill_id: str, request: Request) -> dict[str, Any]:
        """Pin a skill (prevents curator from archiving)."""
        try:
            skill = skill_service.registry.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        skill_service.pin(skill.name)
        store.record_audit(
            actor=request.state.actor, action="skill.pin",
            resource_type="skill", resource_id=skill_id,
        )
        return _learned_skill_json(skill_service.registry.get_skill(skill_id))

    @app.post("/api/learning/skills/{skill_id}/unpin")
    async def api_learning_skill_unpin(skill_id: str, request: Request) -> dict[str, Any]:
        """Unpin a skill."""
        try:
            skill = skill_service.registry.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        skill_service.unpin(skill.name)
        store.record_audit(
            actor=request.state.actor, action="skill.unpin",
            resource_type="skill", resource_id=skill_id,
        )
        return _learned_skill_json(skill_service.registry.get_skill(skill_id))

    @app.post("/api/learning/skills/{skill_id}/archive")
    async def api_learning_skill_archive(skill_id: str, request: Request) -> dict[str, Any]:
        """Archive a skill (soft-delete)."""
        try:
            skill = skill_service.registry.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        skill_service.archive(skill.name)
        store.record_audit(
            actor=request.state.actor, action="skill.archive",
            resource_type="skill", resource_id=skill_id,
        )
        return _learned_skill_json(skill_service.registry.get_skill(skill_id))

    @app.post("/api/learning/skills/{skill_id}/restore")
    async def api_learning_skill_restore(skill_id: str, request: Request) -> dict[str, Any]:
        """Restore an archived skill."""
        try:
            skill = skill_service.registry.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        skill_service.restore(skill.name)
        store.record_audit(
            actor=request.state.actor, action="skill.restore",
            resource_type="skill", resource_id=skill_id,
        )
        return _learned_skill_json(skill_service.registry.get_skill(skill_id))

    @app.post("/api/learning/skills/{skill_id}/rollback")
    async def api_learning_skill_rollback(
        skill_id: str, request: Request, body: RollbackBody = RollbackBody(target_revision=1),
    ) -> dict[str, Any]:
        """Rollback a skill to a previous revision (creates new revision)."""
        try:
            skill = skill_service.registry.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        new_rev = skill_service.rollback(
            skill.name, body.target_revision, reason=body.reason or "UI rollback",
        )
        store.record_audit(
            actor=request.state.actor, action="skill.rollback",
            resource_type="skill", resource_id=skill_id,
            payload={"target_revision": body.target_revision, "new_revision": new_rev},
        )
        return _learned_skill_json(skill_service.registry.get_skill(skill_id))

    @app.get("/api/learning/memory")
    async def api_learning_memory(
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        """List learning-relevant memories."""
        return store.list_memories(limit=limit)

    @app.patch("/api/learning/memory/{memory_id}")
    async def api_learning_memory_update(
        memory_id: str, body: MemoryPatch, request: Request,
    ) -> dict[str, Any]:
        """Update a memory record's content."""
        try:
            existing = store.get_memory(memory_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        store.update_memory(memory_id, content=body.content)
        store.record_audit(
            actor=request.state.actor, action="learning.memory.updated",
            resource_type="memory", resource_id=memory_id,
        )
        return store.get_memory(memory_id)

    @app.delete("/api/learning/memory/{memory_id}")
    async def api_learning_memory_delete(memory_id: str, request: Request) -> dict[str, Any]:
        """Delete a memory record."""
        try:
            existing = store.get_memory(memory_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        store.delete_memory(memory_id)
        store.record_audit(
            actor=request.state.actor, action="learning.memory.deleted",
            resource_type="memory", resource_id=memory_id,
        )
        return {"deleted": True, "id": memory_id}

    @app.get("/api/learning/settings")
    async def api_learning_settings() -> dict[str, Any]:
        """Get current learning settings."""
        return learning_config.to_dict()

    @app.patch("/api/learning/settings")
    async def api_learning_settings_update(
        request: Request, body: LearningSettingsUpdate = LearningSettingsUpdate(),
    ) -> dict[str, Any]:
        """Update learning settings at runtime (non-persistent — affects running instance only).

        For persistent changes, update gravityclaw.toml directly.
        """
        # Note: LearningConfig is frozen, so we rebuild it with overrides.
        # This changes runtime behavior until restart. To persist, user edits TOML.
        changes: dict[str, Any] = {}
        if body.enabled is not None:
            changes["enabled"] = body.enabled
        if body.trust_mode is not None:
            changes["trust_mode"] = body.trust_mode
        if body.min_confidence is not None:
            changes["min_confidence"] = body.min_confidence
        if body.curator_enabled is not None:
            changes["curator_enabled"] = body.curator_enabled
        if body.notifications_mode is not None:
            changes["notifications_mode"] = body.notifications_mode

        store.record_audit(
            actor=request.state.actor, action="learning.settings.updated",
            resource_type="learning_config", resource_id="runtime",
            payload=changes,
        )
        # Return current config (runtime mutation is intentionally limited;
        # config reload requires restart or TOML edit)
        return {**learning_config.to_dict(), "runtime_changes_pending": changes}

    # ─── Phase 4B: Journey Graph API ─────────────────────────────────────────

    @app.get("/api/learning/journey")
    async def api_learning_journey(
        skill_id: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        """Build a provenance graph of learning lifecycle events.

        Returns nodes (runs, skills, proposals, revisions) and edges
        representing causal relationships in the learning lifecycle:
        experience → extract → create → reuse → observe → correct → improve → reuse.
        """
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        seen_nodes: set[str] = set()

        def add_node(node_id: str, kind: str, label: str, meta: dict[str, Any]) -> None:
            if node_id not in seen_nodes:
                seen_nodes.add(node_id)
                nodes.append({"id": node_id, "kind": kind, "label": label, **meta})

        def add_edge(source: str, target: str, relation: str) -> None:
            edges.append({"source": source, "target": target, "relation": relation})

        # Scope: either a specific skill or all skills
        if skill_id:
            try:
                skill = skill_service.registry.get_skill(skill_id)
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            target_skills = [skill]
        else:
            target_skills = skill_service.registry.list_skills(limit=limit)

        for sk in target_skills:
            skill_node_id = f"skill:{sk.skill_id}"
            add_node(skill_node_id, "skill", sk.name, {
                "description": sk.description,
                "state": sk.state,
                "revision": sk.revision,
                "trust": sk.trust,
                "owner": sk.owner,
                "created_at": sk.created_at,
            })

            # Revisions — each is a node linked to the skill
            revisions = skill_service.registry.list_revisions(sk.skill_id, limit=limit)
            for rev in revisions:
                rev_node_id = f"revision:{rev.id}"
                add_node(rev_node_id, "revision", f"Rev {rev.revision} ({rev.operation})", {
                    "skill_id": rev.skill_id,
                    "revision": rev.revision,
                    "parent_revision": rev.parent_revision,
                    "operation": rev.operation,
                    "reason": rev.reason,
                    "created_at": rev.created_at,
                })
                add_edge(rev_node_id, skill_node_id, "produces")

                # Link revision to parent revision
                if rev.parent_revision is not None:
                    parent_revs = [r for r in revisions if r.revision == rev.parent_revision]
                    if parent_revs:
                        add_edge(f"revision:{parent_revs[0].id}", rev_node_id, "evolves_to")

                # Link revision to source run (experience → extract/improve)
                if rev.source_run_id:
                    run_node_id = f"run:{rev.source_run_id}"
                    add_node(run_node_id, "run", f"Run {rev.source_run_id[:8]}", {
                        "run_id": rev.source_run_id,
                        "created_at": rev.created_at,
                    })
                    relation = "triggers_creation" if rev.operation == "create" else "triggers_improvement"
                    add_edge(run_node_id, rev_node_id, relation)

                # Link revision to proposal
                if rev.proposal_id:
                    proposal_node_id = f"proposal:{rev.proposal_id}"
                    add_edge(proposal_node_id, rev_node_id, "approved_as")

            # Proposals — each is a node
            proposals = skill_service.registry.list_proposals(skill_id=sk.skill_id, limit=limit)
            for prop in proposals:
                proposal_node_id = f"proposal:{prop.id}"
                add_node(proposal_node_id, "proposal", f"{prop.operation}: {prop.skill_name}", {
                    "skill_name": prop.skill_name,
                    "operation": prop.operation,
                    "status": prop.status,
                    "confidence": prop.confidence,
                    "reason": prop.reason,
                    "created_at": prop.created_at,
                    "resolved_at": prop.resolved_at,
                })

                # Link proposal to source run
                if prop.source_run_id:
                    run_node_id = f"run:{prop.source_run_id}"
                    add_node(run_node_id, "run", f"Run {prop.source_run_id[:8]}", {
                        "run_id": prop.source_run_id,
                        "created_at": prop.created_at,
                    })
                    add_edge(run_node_id, proposal_node_id, "generates_proposal")

                # Link proposal to skill
                add_edge(proposal_node_id, skill_node_id, "targets")

            # Usage telemetry — skill_usage events with run_ids
            with store._connect() as conn:
                usage_rows = conn.execute(
                    """SELECT DISTINCT run_id, event, created_at
                       FROM skill_usage
                       WHERE skill_id=? AND run_id IS NOT NULL
                       ORDER BY created_at DESC LIMIT ?""",
                    (sk.skill_id, limit),
                ).fetchall()

            for row in usage_rows:
                run_id = row["run_id"]
                event = row["event"]
                run_node_id = f"run:{run_id}"
                add_node(run_node_id, "run", f"Run {run_id[:8]}", {
                    "run_id": run_id,
                    "created_at": row["created_at"],
                })

                # Map usage events to edge relations
                if event in ("discovered", "selected", "loaded", "executed"):
                    add_edge(skill_node_id, run_node_id, "used_in")
                elif event == "successful":
                    add_edge(run_node_id, skill_node_id, "validates")
                elif event == "failed":
                    add_edge(run_node_id, skill_node_id, "fails_with")
                elif event == "corrected":
                    add_edge(run_node_id, skill_node_id, "corrects")
                elif event == "proposal_generated":
                    add_edge(run_node_id, skill_node_id, "proposes_change")

        # Build summary stats
        kind_counts = {}
        for node in nodes:
            kind_counts[node["kind"]] = kind_counts.get(node["kind"], 0) + 1

        return {
            "nodes": nodes,
            "edges": edges,
            "stats": {
                "total_nodes": len(nodes),
                "total_edges": len(edges),
                "by_kind": kind_counts,
            },
        }

    # ─── Phase 4A: Learning Studio JSON helpers ───────────────────────────────

    def _proposal_json(proposal: Any) -> dict[str, Any]:
        return {
            "id": proposal.id,
            "skill_id": proposal.skill_id,
            "skill_name": proposal.skill_name,
            "operation": proposal.operation,
            "description": proposal.description,
            "reason": proposal.reason,
            "confidence": proposal.confidence,
            "content": proposal.content,
            "before": proposal.before,
            "base_revision": proposal.base_revision,
            "source_run_id": proposal.source_run_id,
            "review_model": proposal.review_model,
            "status": proposal.status,
            "status_reason": proposal.status_reason,
            "created_at": proposal.created_at,
            "resolved_at": proposal.resolved_at,
        }

    def _learned_skill_json(skill: Any) -> dict[str, Any]:
        return {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "path": skill.path,
            "owner": skill.owner,
            "state": skill.state,
            "trust": skill.trust,
            "revision": skill.revision,
            "pinned": skill.pinned,
            "created_at": skill.created_at,
            "updated_at": skill.updated_at,
        }

    def _revision_json(revision: Any) -> dict[str, Any]:
        return {
            "id": revision.id,
            "skill_id": revision.skill_id,
            "revision": revision.revision,
            "parent_revision": revision.parent_revision,
            "operation": revision.operation,
            "source_run_id": revision.source_run_id,
            "proposal_id": revision.proposal_id,
            "model": revision.model,
            "reason": revision.reason,
            "created_at": revision.created_at,
        }

    if frontend_directory is not None:
        # Mount last so every API and WebSocket route above wins first. The
        # mount supplies index.html for browser history routes only.
        app.mount("/", SPAStaticFiles(frontend_directory), name="frontend")

    return app


def _model_list_from_environment(value: Any) -> list[str]:
    raw = os.environ.get("GRAVITYCLAW_AGY_MODELS")
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _conversation_json(conversation: Any, default_model: str | None) -> dict[str, Any]:
    value = asdict(conversation)
    value["model_policy"] = {
        "mode": "explicit" if conversation.model_override else "default",
        "model": conversation.model_override,
        "global_default": default_model,
    }
    return value


def _conversation_model_json(conversation: Any, default_model: str | None) -> dict[str, Any]:
    return {
        "conversation_id": conversation.id,
        "model_policy": "explicit" if conversation.model_override else "default",
        "requested_model": conversation.model_override,
        "resolved_model": conversation.model_override or default_model,
        "global_default_model": default_model,
        "effort": conversation.effort_override,
    }


def _run_json(run: RunRecord) -> dict[str, Any]:
    value = asdict(run)
    value["requested_model"] = run.request.get("requested_model")
    value["resolved_model"] = run.request.get("resolved_model")
    value["agy_version"] = run.request.get("agy_version")
    return value


def _attachment_json(record: AttachmentRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "workspace_id": record.workspace_id,
        "conversation_id": record.conversation_id,
        "message_id": record.message_id,
        "filename": record.filename,
        "mime_type": record.mime_type,
        "kind": record.kind,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "source": record.source,
        "width": record.width,
        "height": record.height,
        "duration_ms": record.duration_ms,
        "state": record.state,
        "created_at": record.created_at,
    }


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
    data = _read_secret_file(secret_file, "Telegram token")
    if len(data) > 4096:
        raise ValueError("Telegram token file is unexpectedly large")
    token = data.decode("utf-8").strip()
    if not token:
        raise ValueError("Telegram token file is empty")
    return token


def _read_config_secret(filename: Any, *, required: bool) -> str | None:
    if not filename:
        if required:
            raise ValueError("configured secret file path is empty")
        return None
    try:
        return read_secret_file(Path(str(filename)))
    except ValueError:
        if required:
            raise
        return None


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
    data = _read_secret_file(secret_file, "control token")
    if len(data) > 4096:
        raise ValueError("control token file is unexpectedly large")
    token = data.decode("utf-8").strip()
    if not token:
        raise ValueError("control token file is empty")
    return token


def _read_secret_file(filename: str, label: str) -> bytes:
    path = Path(filename)
    try:
        mode = path.stat().st_mode
    except FileNotFoundError as exc:
        raise ValueError(f"{label} file does not exist") from exc
    if mode & 0o077:
        raise ValueError(f"{label} file must not be group/world accessible")
    if not path.is_file():
        raise ValueError(f"{label} path is not a regular file")
    return path.read_bytes()
