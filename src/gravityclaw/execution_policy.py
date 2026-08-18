"""Execution policy governing direct host execution on the VPS host."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The result of evaluating a shell command against the ExecutionPolicy."""

    allowed: bool
    requires_approval: bool = False
    reason: str | None = None
    classification: str = "normal"


# Destructive recursive or disk-wiping patterns that must never run unconfirmed
DESTRUCTIVE_COMMAND_PATTERNS = (
    re.compile(r"\brm\s+-[rfRF]{1,4}\s+(/\s*$|/\*|/etc|/boot|/usr|/var|/bin|/sbin|~/\*|\$HOME/\*)"),
    re.compile(r"\bmkfs(\.[a-z0-9]+)?\b"),
    re.compile(r"\bdd\s+if=.*\s+of=/dev/"),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),  # fork bomb
)

# Elevated commands that require administrative privilege or affect system-wide state
ELEVATED_BINARIES = frozenset({
    "sudo", "su", "pkexec", "doas",
    "reboot", "shutdown", "poweroff", "halt", "init",
    "apt", "apt-get", "dpkg", "pacman", "dnf", "yum", "rpm", "zypper", "apk",
    "ufw", "iptables", "ip6tables", "nft", "firewall-cmd",
    "useradd", "usermod", "userdel", "groupadd", "groupmod", "groupdel", "passwd", "chpasswd",
    "chown", "chmod",  # when applied to system paths
})

# Normal commands that are safe and autonomous for development/admin tasks
NORMAL_BINARIES = frozenset({
    "git", "npm", "npx", "yarn", "pnpm", "bun", "node", "deno",
    "python", "python3", "pip", "pip3", "pytest", "poetry", "uv", "venv",
    "cargo", "rustc", "go", "gcc", "g++", "make", "cmake",
    "ls", "cat", "grep", "find", "mkdir", "cp", "mv", "touch", "rm", "sed", "awk",
    "pwd", "echo", "printf", "diff", "head", "tail", "wc", "sort", "uniq", "tr", "cut",
    "journalctl", "systemctl",
    "ps", "top", "htop", "lsof", "free", "df", "du", "uname", "uptime", "whoami", "id", "env",
    "curl", "wget", "ping", "dig", "nc", "netstat", "ss", "ip", "host",
    "tar", "zip", "unzip", "gzip", "gunzip", "bzip2",
    "agy", "antigravity",
})


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Execution policy governing shell commands executed on the VPS host."""

    target: str = "host"
    sandbox: bool = False
    mode: str = "balanced"  # "balanced" | "full" | "restricted"
    allow_normal_commands: bool = True
    require_approval_for_elevated: bool = True

    def classify(self, command: str) -> str:
        """Classify a command string into 'normal', 'elevated', or 'destructive'."""
        clean = command.strip()
        if not clean:
            return "normal"

        for pattern in DESTRUCTIVE_COMMAND_PATTERNS:
            if pattern.search(clean):
                return "destructive"

        try:
            tokens = shlex.split(clean)
        except ValueError:
            tokens = clean.split()

        if not tokens:
            return "normal"

        first = Path(tokens[0]).name
        if first in ELEVATED_BINARIES:
            return "elevated"

        # Check systemctl without --user
        if first == "systemctl":
            if "--user" not in tokens:
                subcommand = next((t for t in tokens[1:] if not t.startswith("-")), "")
                if subcommand in {"restart", "stop", "start", "disable", "enable", "mask"}:
                    return "elevated"

        # Check chmod/chown on system root paths
        if first in {"chmod", "chown"}:
            for arg in tokens[1:]:
                if arg.startswith(("/", "/etc", "/usr", "/bin", "/var", "/lib")):
                    return "elevated"

        return "normal"

    def evaluate(self, command: str) -> PolicyDecision:
        """Evaluate a command string against the configured policy."""
        classification = self.classify(command)

        if classification == "destructive":
            return PolicyDecision(
                allowed=False,
                requires_approval=True,
                reason="Destructive command pattern detected; requires explicit confirmation.",
                classification=classification,
            )

        if self.mode == "full":
            return PolicyDecision(
                allowed=True,
                requires_approval=False,
                reason="Full autonomy mode enabled; all commands permitted.",
                classification=classification,
            )

        if classification == "elevated":
            if self.mode == "restricted":
                return PolicyDecision(
                    allowed=False,
                    requires_approval=False,
                    reason="Elevated command blocked under restricted execution policy.",
                    classification=classification,
                )
            if self.require_approval_for_elevated:
                return PolicyDecision(
                    allowed=True,
                    requires_approval=True,
                    reason="Elevated command requires approval in balanced execution mode.",
                    classification=classification,
                )
            return PolicyDecision(
                allowed=True,
                requires_approval=False,
                reason="Elevated command permitted by configuration.",
                classification=classification,
            )

        if not self.allow_normal_commands:
            return PolicyDecision(
                allowed=False,
                requires_approval=False,
                reason="Normal command execution disabled by policy.",
                classification=classification,
            )

        return PolicyDecision(
            allowed=True,
            requires_approval=False,
            reason="Normal command permitted by execution policy.",
            classification=classification,
        )
