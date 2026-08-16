"""Human-editable GravityClaw identity documents."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


IDENTITY_FILES = ("SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md", "HEARTBEAT.md")
# HEARTBEAT.md is scheduler input, not a standing instruction injected into
# ordinary conversations.  Keeping this list explicit prevents a future file
# from silently becoming authoritative model context.
EXECUTION_IDENTITY_FILES = ("SOUL.md", "USER.md", "AGENTS.md", "TOOLS.md")
DEFAULT_DOCUMENTS = {
    "SOUL.md": "# Soul\n\nDescribe the agent's personality, values, and communication style.\n",
    "USER.md": "# User\n\nRecord durable preferences the agent should know about its user.\n",
    "AGENTS.md": "# Operating Instructions\n\nDescribe how the agent should operate.\n",
    "TOOLS.md": "# Tool Notes\n\nRecord environment-specific tool and device notes.\n",
    "HEARTBEAT.md": "# Heartbeat\n\nDescribe periodic checks. Empty means no proactive checks.\n",
    "MEMORY.md": "# Long-term Memory\n\nCurated durable memories belong here.\n",
}


@dataclass(frozen=True, slots=True)
class IdentityDocument:
    name: str
    path: Path
    content: str
    sha256: str


class IdentityStore:
    def __init__(self, home: Path, max_document_bytes: int = 256_000) -> None:
        self.home = home
        self.max_document_bytes = max_document_bytes

    def bootstrap(self) -> list[Path]:
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.home.chmod(0o700)
        (self.home / "memory").mkdir(mode=0o700, exist_ok=True)
        (self.home / "workspaces").mkdir(mode=0o700, exist_ok=True)
        created: list[Path] = []
        for name, content in DEFAULT_DOCUMENTS.items():
            path = self.home / name
            if not path.exists():
                _atomic_write(path, content)
                created.append(path)
        return created

    def load(self, names: tuple[str, ...] = IDENTITY_FILES) -> list[IdentityDocument]:
        documents: list[IdentityDocument] = []
        for name in names:
            if name not in DEFAULT_DOCUMENTS:
                raise ValueError(f"unsupported identity document: {name}")
            path = self.home / name
            data = path.read_bytes()
            if len(data) > self.max_document_bytes:
                raise ValueError(f"identity document is too large: {path}")
            content = data.decode("utf-8")
            documents.append(
                IdentityDocument(name, path, content, hashlib.sha256(data).hexdigest())
            )
        return documents

    def load_curated_memory(self) -> IdentityDocument:
        return self.load(("MEMORY.md",))[0]

    def load_execution_identity(self) -> list[IdentityDocument]:
        return self.load(EXECUTION_IDENTITY_FILES)


def _atomic_write(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
