"""First-class attachment primitives for GravityClaw.

Attachments belong to the conversation/run model, not to an individual UI or
provider.  Any channel (Web, Telegram, API) feeds the same intake pipeline,
producing an AttachmentRecord that lives alongside Message.

Architecture:
    Channel (Web / Telegram / API)
        → AttachmentService.ingest()
            → validate → store → detect type → extract metadata
        → AttachmentRecord
            → Message.attachments[]
                → Run Preparation
                    → AttachmentResolver
                        → native multimodal | AGY mount
"""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import os
import re
import shutil
import struct
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Sequence

LOGGER = logging.getLogger(__name__)

# ─── Domain Model ────────────────────────────────────────────────────────────

ATTACHMENT_KINDS = ("image", "audio", "video", "document", "archive", "other")
UPLOAD_STATES = ("queued", "ready", "failed")
SOURCES = ("web", "telegram", "api", "agent")

# Security limits
MAX_UPLOAD_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_ATTACHMENTS_PER_MESSAGE = 10
MAX_FILENAME_LENGTH = 255
FORBIDDEN_EXTENSIONS = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".msi", ".scr", ".pif", ".vbs", ".js",
    ".wsh", ".wsf", ".ps1",
})

# MIME → kind mapping
_MIME_KIND_MAP: dict[str, str] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
}
_DOCUMENT_MIMES = frozenset({
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "application/json",
    "application/xml",
})
_ARCHIVE_MIMES = frozenset({
    "application/zip",
    "application/x-tar",
    "application/gzip",
    "application/x-bzip2",
    "application/x-7z-compressed",
    "application/x-rar-compressed",
})


@dataclass(frozen=True, slots=True)
class AttachmentRecord:
    """Immutable attachment metadata — persisted in SQLite, referenced by messages."""

    id: str
    workspace_id: str
    conversation_id: str
    message_id: str | None
    filename: str
    mime_type: str
    kind: str
    size_bytes: int
    storage_path: str
    sha256: str
    source: str
    width: int | None = None
    height: int | None = None
    duration_ms: int | None = None
    metadata_json: str | None = None
    state: str = "ready"
    created_at: str = ""


# ─── Security Utilities ──────────────────────────────────────────────────────

_SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._\- ]")


def sanitize_filename(raw: str) -> str:
    """Sanitize a user-supplied filename: strip paths, collapse unsafe chars."""
    # Strip any path components
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    # Remove null bytes, control chars
    name = "".join(c for c in name if c.isprintable() and c != "\x00")
    # Replace unsafe characters
    name = _SAFE_FILENAME_RE.sub("_", name)
    # Collapse repeated underscores/dots
    name = re.sub(r"[_.]{2,}", "_", name)
    # Limit length
    if len(name) > MAX_FILENAME_LENGTH:
        stem, ext = os.path.splitext(name)
        name = stem[: MAX_FILENAME_LENGTH - len(ext)] + ext
    return name.strip("._") or "attachment"


def detect_mime_type(filename: str, content: bytes | None = None) -> str:
    """Detect MIME type by magic bytes first, then extension fallback."""
    if content:
        detected = _sniff_magic(content)
        if detected:
            return detected
    guessed, _ = mimetypes.guess_type(filename)
    return guessed or "application/octet-stream"


def classify_kind(mime_type: str) -> str:
    """Map a MIME type to an attachment kind."""
    major = mime_type.split("/", 1)[0]
    if major in _MIME_KIND_MAP:
        return _MIME_KIND_MAP[major]
    if mime_type in _DOCUMENT_MIMES:
        return "document"
    if mime_type in _ARCHIVE_MIMES:
        return "archive"
    return "other"


def validate_upload(filename: str, size: int, mime_type: str) -> list[str]:
    """Return a list of validation errors (empty = OK)."""
    errors: list[str] = []
    if size > MAX_UPLOAD_SIZE:
        errors.append(
            f"File exceeds maximum size ({size} > {MAX_UPLOAD_SIZE} bytes)"
        )
    if size == 0:
        errors.append("File is empty")
    ext = os.path.splitext(filename)[1].lower()
    if ext in FORBIDDEN_EXTENSIONS:
        errors.append(f"File extension '{ext}' is not allowed")
    return errors


def _sniff_magic(data: bytes) -> str | None:
    """Detect MIME from magic bytes (first ~12 bytes)."""
    if len(data) < 4:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:4] == b"PK\x03\x04":
        return "application/zip"
    if data[:3] == b"\x1f\x8b\x08":
        return "application/gzip"
    if data[:6] == b"\x37\x7a\xbc\xaf\x27\x1c":
        return "application/x-7z-compressed"
    return None


# ─── Image Metadata Extraction ───────────────────────────────────────────────

def extract_image_dimensions(data: bytes, mime_type: str) -> tuple[int | None, int | None]:
    """Extract width/height from image data without heavy dependencies."""
    try:
        if mime_type == "image/png" and len(data) >= 24:
            width = struct.unpack(">I", data[16:20])[0]
            height = struct.unpack(">I", data[20:24])[0]
            return width, height
        if mime_type == "image/jpeg":
            return _jpeg_dimensions(data)
        if mime_type == "image/gif" and len(data) >= 10:
            width = struct.unpack("<H", data[6:8])[0]
            height = struct.unpack("<H", data[8:10])[0]
            return width, height
    except (struct.error, IndexError):
        pass
    return None, None


def _jpeg_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Parse JPEG SOF marker for dimensions."""
    stream = io.BytesIO(data)
    stream.seek(2)  # Skip FFD8
    while True:
        marker = stream.read(2)
        if len(marker) < 2:
            break
        if marker[0] != 0xFF:
            break
        code = marker[1]
        if code in (0xC0, 0xC2):  # SOF0, SOF2
            stream.read(3)  # length (2) + precision (1)
            height_bytes = stream.read(2)
            width_bytes = stream.read(2)
            if len(height_bytes) == 2 and len(width_bytes) == 2:
                height = struct.unpack(">H", height_bytes)[0]
                width = struct.unpack(">H", width_bytes)[0]
                return width, height
            break
        # Skip other markers
        length_bytes = stream.read(2)
        if len(length_bytes) < 2:
            break
        length = struct.unpack(">H", length_bytes)[0]
        stream.seek(length - 2, 1)
    return None, None


# ─── Attachment Storage ──────────────────────────────────────────────────────

class AttachmentStorage:
    """Filesystem-backed attachment binary storage.

    Layout:
        {root}/workspaces/{workspace_id}/attachments/{yyyy}/{mm}/{attachment_id}/{filename}

    The indirection through the workspace and date directories keeps the tree
    navigable and prevents single-directory inode pressure.  The abstraction
    can later point at S3/R2/MinIO without changing callers.
    """

    def __init__(self, root: Path) -> None:
        self.root = root

    def store(
        self,
        attachment_id: str,
        workspace_id: str,
        filename: str,
        data: BinaryIO,
    ) -> tuple[str, str, int]:
        """Write file contents, return (relative_storage_path, sha256, size_bytes)."""
        now = datetime.now(UTC)
        relative = (
            f"workspaces/{workspace_id}/attachments"
            f"/{now.year:04d}/{now.month:02d}"
            f"/{attachment_id}/{filename}"
        )
        absolute = self.root / relative
        absolute.parent.mkdir(parents=True, exist_ok=True)

        hasher = hashlib.sha256()
        total = 0
        with open(absolute, "wb") as handle:
            while True:
                chunk = data.read(65536)
                if not chunk:
                    break
                if total + len(chunk) > MAX_UPLOAD_SIZE:
                    # Clean up partial write
                    handle.close()
                    absolute.unlink(missing_ok=True)
                    raise ValueError("Upload exceeds maximum allowed size")
                hasher.update(chunk)
                handle.write(chunk)
                total += len(chunk)

        # Set restrictive permissions
        os.chmod(absolute, 0o600)
        return relative, hasher.hexdigest(), total

    def read(self, storage_path: str) -> Path:
        """Return absolute path for a stored attachment."""
        absolute = (self.root / storage_path).resolve()
        if not str(absolute).startswith(str(self.root.resolve())):
            raise ValueError("Storage path escapes root directory")
        return absolute

    def delete(self, storage_path: str) -> None:
        """Remove a stored attachment file and empty parent dirs."""
        absolute = (self.root / storage_path).resolve()
        if not str(absolute).startswith(str(self.root.resolve())):
            raise ValueError("Storage path escapes root directory")
        absolute.unlink(missing_ok=True)
        # Clean empty parent directories up to the root
        parent = absolute.parent
        while parent != self.root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


# ─── Attachment Service ──────────────────────────────────────────────────────

class AttachmentService:
    """Core service: validate, store, create records.

    This is the single entry point all channels use to create attachments.
    Web uploads, Telegram downloads, and API calls all go through here.
    """

    def __init__(self, store: "AttachmentStore", storage: AttachmentStorage) -> None:
        self.store = store
        self.storage = storage

    def ingest(
        self,
        workspace_id: str,
        conversation_id: str,
        filename: str,
        data: BinaryIO,
        *,
        source: str = "web",
        message_id: str | None = None,
        mime_type_hint: str | None = None,
    ) -> AttachmentRecord:
        """Intake a file through the full validation/store/classify pipeline."""
        # Sanitize filename
        safe_name = sanitize_filename(filename)
        attachment_id = str(uuid.uuid4())

        # Read first 4KB for type detection
        header = data.read(4096)
        data.seek(0)

        # Detect MIME
        if mime_type_hint and mime_type_hint != "application/octet-stream":
            # Verify hint against magic bytes
            detected = detect_mime_type(safe_name, header)
            mime_type = detected if detected != "application/octet-stream" else mime_type_hint
        else:
            mime_type = detect_mime_type(safe_name, header)

        # Store the binary data
        storage_path, sha256, size_bytes = self.storage.store(
            attachment_id, workspace_id, safe_name, data
        )

        # Validate after storing (we have the size now)
        errors = validate_upload(safe_name, size_bytes, mime_type)
        if errors:
            self.storage.delete(storage_path)
            raise ValueError("; ".join(errors))

        # Classify
        kind = classify_kind(mime_type)

        # Extract media metadata
        width: int | None = None
        height: int | None = None
        duration_ms: int | None = None
        if kind == "image":
            absolute = self.storage.read(storage_path)
            image_data = absolute.read_bytes()
            width, height = extract_image_dimensions(image_data, mime_type)

        # Persist the record
        record = self.store.create_attachment(
            id=attachment_id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            message_id=message_id,
            filename=safe_name,
            mime_type=mime_type,
            kind=kind,
            size_bytes=size_bytes,
            storage_path=storage_path,
            sha256=sha256,
            source=source,
            width=width,
            height=height,
            duration_ms=duration_ms,
        )
        LOGGER.info(
            "attachment ingested: id=%s kind=%s size=%d source=%s",
            record.id, record.kind, record.size_bytes, record.source,
        )
        return record

    def link_to_message(self, attachment_id: str, message_id: str) -> None:
        """Associate a previously uploaded attachment with a message."""
        self.store.link_attachment(attachment_id, message_id)

    def get(self, attachment_id: str) -> AttachmentRecord:
        return self.store.get_attachment(attachment_id)

    def list_for_message(self, message_id: str) -> list[AttachmentRecord]:
        return self.store.list_attachments(message_id=message_id)

    def list_for_conversation(self, conversation_id: str) -> list[AttachmentRecord]:
        return self.store.list_attachments(conversation_id=conversation_id)

    def resolve_path(self, attachment_id: str) -> Path:
        """Return the absolute filesystem path for an attachment's binary."""
        record = self.store.get_attachment(attachment_id)
        return self.storage.read(record.storage_path)


# ─── Attachment Resolver (native multimodal vs AGY mount) ────────────────────

@dataclass(frozen=True, slots=True)
class ModelMediaCapabilities:
    """What media types a model can process natively."""

    vision: bool = False
    audio_input: bool = False
    video_input: bool = False
    pdf_native: bool = False

    @classmethod
    def for_model(cls, model_id: str) -> "ModelMediaCapabilities":
        """Resolve media capabilities from model identifier.

        This is a conservative lookup — models not explicitly listed get no
        native media support and attachments will be mounted as files.
        """
        normalized = model_id.lower()
        # GPT-4o and successors
        if any(tag in normalized for tag in ("gpt-4o", "gpt-5", "o3", "o4")):
            return cls(vision=True, pdf_native=True)
        # Claude 3+ family
        if any(tag in normalized for tag in ("claude-3", "claude-4")):
            return cls(vision=True, pdf_native=True)
        # Gemini Pro/Ultra/Flash with vision
        if "gemini" in normalized:
            return cls(vision=True, audio_input=True, video_input=True, pdf_native=True)
        return cls()


@dataclass(frozen=True, slots=True)
class ResolvedAttachment:
    """How an attachment should be presented to the model/agent."""

    record: AttachmentRecord
    strategy: str  # "native_image" | "native_pdf" | "file_mount"
    mount_path: str | None = None  # path inside container if strategy=file_mount


class AttachmentResolver:
    """Decide how each attachment reaches the model.

    Strategy:
    - native_image: image sent as multimodal content (model has vision)
    - native_pdf: PDF sent natively (model supports it)
    - file_mount: attachment mounted read-only at /run/attachments/{id}-{name}
    """

    def resolve(
        self,
        attachments: Sequence[AttachmentRecord],
        model_id: str,
    ) -> list[ResolvedAttachment]:
        caps = ModelMediaCapabilities.for_model(model_id)
        results: list[ResolvedAttachment] = []
        for attachment in attachments:
            strategy = self._pick_strategy(attachment, caps)
            mount_path: str | None = None
            if strategy == "file_mount":
                mount_path = f"/run/attachments/{attachment.id[:8]}-{attachment.filename}"
            results.append(ResolvedAttachment(
                record=attachment,
                strategy=strategy,
                mount_path=mount_path,
            ))
        return results

    @staticmethod
    def _pick_strategy(
        attachment: AttachmentRecord, caps: ModelMediaCapabilities
    ) -> str:
        if attachment.kind == "image" and caps.vision:
            # Images under 20MB can go natively
            if attachment.size_bytes <= 20 * 1024 * 1024:
                return "native_image"
        if attachment.mime_type == "application/pdf" and caps.pdf_native:
            # PDFs under 10MB native
            if attachment.size_bytes <= 10 * 1024 * 1024:
                return "native_pdf"
        return "file_mount"


# ─── Attachment Store (SQLite persistence) ───────────────────────────────────

class AttachmentStore:
    """SQLite-backed attachment metadata persistence.

    Designed to be instantiated from Store and share the same connection pool.
    """

    def __init__(self, store: "Store") -> None:  # type: ignore[name-defined]
        self._store = store

    def create_attachment(
        self,
        *,
        id: str,
        workspace_id: str,
        conversation_id: str,
        message_id: str | None,
        filename: str,
        mime_type: str,
        kind: str,
        size_bytes: int,
        storage_path: str,
        sha256: str,
        source: str,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        metadata_json: str | None = None,
    ) -> AttachmentRecord:
        now = datetime.now(UTC).isoformat()
        with self._store._connect() as connection:
            connection.execute(
                """INSERT INTO attachments(
                    id, workspace_id, conversation_id, message_id,
                    filename, mime_type, kind, size_bytes, storage_path,
                    sha256, source, width, height, duration_ms,
                    metadata_json, state, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', ?)""",
                (
                    id, workspace_id, conversation_id, message_id,
                    filename, mime_type, kind, size_bytes, storage_path,
                    sha256, source, width, height, duration_ms,
                    metadata_json, now,
                ),
            )
        return AttachmentRecord(
            id=id,
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            message_id=message_id,
            filename=filename,
            mime_type=mime_type,
            kind=kind,
            size_bytes=size_bytes,
            storage_path=storage_path,
            sha256=sha256,
            source=source,
            width=width,
            height=height,
            duration_ms=duration_ms,
            metadata_json=metadata_json,
            state="ready",
            created_at=now,
        )

    def get_attachment(self, attachment_id: str) -> AttachmentRecord:
        with self._store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM attachments WHERE id=?", (attachment_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"attachment not found: {attachment_id}")
        return _attachment_from_row(row)

    def link_attachment(self, attachment_id: str, message_id: str) -> None:
        with self._store._connect() as connection:
            connection.execute(
                "UPDATE attachments SET message_id=? WHERE id=?",
                (message_id, attachment_id),
            )

    def list_attachments(
        self,
        *,
        message_id: str | None = None,
        conversation_id: str | None = None,
    ) -> list[AttachmentRecord]:
        with self._store._connect() as connection:
            if message_id:
                rows = connection.execute(
                    "SELECT * FROM attachments WHERE message_id=? ORDER BY created_at",
                    (message_id,),
                ).fetchall()
            elif conversation_id:
                rows = connection.execute(
                    "SELECT * FROM attachments WHERE conversation_id=? ORDER BY created_at",
                    (conversation_id,),
                ).fetchall()
            else:
                rows = []
        return [_attachment_from_row(row) for row in rows]

    def list_for_run(self, run_id: str) -> list[AttachmentRecord]:
        """Get all attachments linked to the user message of a run."""
        with self._store._connect() as connection:
            rows = connection.execute(
                """SELECT a.* FROM attachments a
                   JOIN messages m ON a.message_id = m.id
                   WHERE m.source_run_id = ? AND m.role = 'user'
                   ORDER BY a.created_at""",
                (run_id,),
            ).fetchall()
        return [_attachment_from_row(row) for row in rows]

    def delete_attachment(self, attachment_id: str) -> None:
        with self._store._connect() as connection:
            connection.execute(
                "DELETE FROM attachments WHERE id=?", (attachment_id,)
            )


def _attachment_from_row(row: Any) -> AttachmentRecord:
    return AttachmentRecord(
        id=row["id"],
        workspace_id=row["workspace_id"],
        conversation_id=row["conversation_id"],
        message_id=row["message_id"],
        filename=row["filename"],
        mime_type=row["mime_type"],
        kind=row["kind"],
        size_bytes=row["size_bytes"],
        storage_path=row["storage_path"],
        sha256=row["sha256"],
        source=row["source"],
        width=row["width"],
        height=row["height"],
        duration_ms=row["duration_ms"],
        metadata_json=row["metadata_json"],
        state=row["state"],
        created_at=row["created_at"],
    )
