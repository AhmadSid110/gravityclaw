"""Deterministic Milestone 7 capability governance acceptance gate."""

from __future__ import annotations

import argparse
import asyncio
import json
import multiprocessing
import os
import signal
import tempfile
from pathlib import Path

from gravityclaw.capabilities import CapabilityManager
from gravityclaw.execution import AgyContainerSpecFactory
from gravityclaw.store import Store


def _kill_during_publication(database: str, home: str, run_id: str, workspace_id: str, boundary: str) -> None:
    import gravityclaw.capabilities as module

    store = Store(Path(database))
    store.initialize()
    manager = CapabilityManager(Path(home), store, secret_dir=Path(home) / "secrets")
    run = store.get_run(run_id)
    conversation = store.get_conversation(run.conversation_id)
    workspace = store.get_workspace(workspace_id)
    original = module.os.replace

    def crash_before_final_rename(source: str | bytes, destination: str | bytes) -> None:
        if Path(destination).name == run_id:
            if boundary == "before":
                os.kill(os.getpid(), signal.SIGKILL)
        original(source, destination)
        if Path(destination).name == run_id and boundary == "after":
            os.kill(os.getpid(), signal.SIGKILL)

    module.os.replace = crash_before_final_rename
    manager.prepare_run(run, conversation, workspace)


def _run_killed_child(*args: str) -> None:
    process = multiprocessing.Process(target=_kill_during_publication, args=args)
    process.start()
    process.join(10)
    if process.exitcode != -signal.SIGKILL:
        raise AssertionError(f"publication child did not receive SIGKILL: {process.exitcode}")


async def gate(root: Path) -> dict[str, int | bool]:
    store = Store(root / "gravityclaw.db")
    store.initialize()
    workspace_a = store.create_workspace("a", root / "a")
    workspace_b = store.create_workspace("b", root / "b")
    secret_dir = root / "secrets"
    secret_dir.mkdir(mode=0o700)
    (secret_dir / "github-token").write_text("m7-secret-must-not-persist\n", encoding="utf-8")
    skill = workspace_a.path / ".agents" / "skills" / "coding"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Coding\nUse the approved coding workflow.\n", encoding="utf-8")

    capabilities = CapabilityManager(root, store, secret_dir=secret_dir)
    capabilities.register_skill(
        skill_id="workspace-a:coding", name="Coding", path=skill,
        workspace_id=workspace_a.id, profiles=("coding",), version="1",
    )
    capabilities.register_mcp(
        server_id="workspace-a:github", name="GitHub", transport="stdio",
        command="echo", args=("github-mcp",),
        env_refs={"GITHUB_TOKEN": "secret:github-token"}, workspace_id=workspace_a.id,
    )
    assert capabilities.health_check("workspace-a:github").health_state == "HEALTHY"

    manifests: list[dict[str, object]] = []
    prepared_runs = []
    for index in range(50):
        conversation = store.create_conversation(workspace_a.id, channel="m7", channel_key=str(index))
        run = store.submit_run(
            conversation.id,
            {"prompt": f"coding task {index}", "context_profile": "coding", "allow_all": True},
        )
        claimed = store.claim_run(run.id)
        assert claimed is not None
        prepared = capabilities.prepare_run(claimed, conversation, workspace_a)
        manifest = store.get_capability_manifest(run.id)
        manifests.append(manifest)
        prepared_runs.append(prepared)
        assert manifest["manifest_hash"]
        assert "m7-secret-must-not-persist" not in json.dumps(manifest)
        assert "m7-secret-must-not-persist" not in json.dumps(prepared.request)
        spec = capabilities.apply_to_spec(
            AgyContainerSpecFactory().build(prepared, conversation, workspace_a), prepared
        )
        assert spec.environment["GRAVITYCLAW_SECRET_GITHUB_TOKEN"] == "m7-secret-must-not-persist"
        assert any(target.endswith("mcp_config.json") for _, target, _ in spec.mounts)

    # A worker from workspace B sees neither A's skill nor A's MCP server.
    conversation_b = store.create_conversation(workspace_b.id)
    run_b = store.submit_run(conversation_b.id, {"prompt": "b", "context_profile": "coding"})
    claimed_b = store.claim_run(run_b.id)
    assert claimed_b is not None
    prepared_b = capabilities.prepare_run(claimed_b, conversation_b, workspace_b)
    assert prepared_b.request["capability_manifest"]["skills"] == []
    assert prepared_b.request["capability_manifest"]["mcp"] == []

    # Publishing is atomic: SIGKILL before rename leaves no half-visible final
    # directory. A restarted publisher can produce a complete replacement.
    crash_conversation = store.create_conversation(workspace_a.id, channel="m7", channel_key="crash")
    crash_run = store.submit_run(crash_conversation.id, {"prompt": "crash", "context_profile": "coding"})
    crash_claimed = store.claim_run(crash_run.id)
    assert crash_claimed is not None
    _run_killed_child(str(store.path), str(root), crash_run.id, workspace_a.id, "before")
    final_snapshot = root / "capability-snapshots" / crash_run.id
    assert not final_snapshot.exists()
    restarted = CapabilityManager(root, store, secret_dir=secret_dir)
    restarted.prepare_run(crash_claimed, crash_conversation, workspace_a)
    assert (final_snapshot / "manifest.json").is_file()
    assert (final_snapshot / "mcp_config.json").is_file()

    adopted_conversation = store.create_conversation(workspace_a.id, channel="m7", channel_key="adopt")
    adopted_run = store.submit_run(adopted_conversation.id, {"prompt": "adopt", "context_profile": "coding"})
    adopted_claimed = store.claim_run(adopted_run.id)
    assert adopted_claimed is not None
    _run_killed_child(str(store.path), str(root), adopted_run.id, workspace_a.id, "after")
    adopted_snapshot = root / "capability-snapshots" / adopted_run.id
    assert (adopted_snapshot / "manifest.json").is_file()
    restarted.prepare_run(adopted_claimed, adopted_conversation, workspace_a)
    assert store.get_capability_manifest(adopted_run.id)["manifest_hash"]

    # Registry changes do not mutate any already published run snapshot.
    capabilities.set_skill_enabled("workspace-a:coding", False)
    for manifest in manifests:
        assert len(manifest["skills"]) == 1
    assert all(item["manifest_hash"] == manifests[0]["manifest_hash"] for item in manifests)

    with store._connect() as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        wal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        raw_database = b"".join(
            path.read_bytes() for path in (
                root / "gravityclaw.db", root / "gravityclaw.db-wal", root / "gravityclaw.db-shm"
            ) if path.exists()
        )
    assert integrity == "ok" and wal.lower() == "wal"
    assert b"m7-secret-must-not-persist" not in raw_database
    return {
        "simultaneous_snapshots": len(manifests),
        "workspace_b_skills": len(prepared_b.request["capability_manifest"]["skills"]),
        "workspace_b_mcp": len(prepared_b.request["capability_manifest"]["mcp"]),
        "atomic_republication": (final_snapshot / "manifest.json").is_file(),
        "secret_absent_from_sqlite": b"m7-secret-must-not-persist" not in raw_database,
        "sqlite_integrity": integrity == "ok",
        "wal": wal.lower() == "wal",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.root:
        args.root.mkdir(parents=True, exist_ok=True)
        print(asyncio.run(gate(args.root)))
        return
    with tempfile.TemporaryDirectory(prefix="gravityclaw-m7-acceptance-") as directory:
        print(asyncio.run(gate(Path(directory))))
    print("M7_CAPABILITY_GATE_PASSED")


if __name__ == "__main__":
    main()
