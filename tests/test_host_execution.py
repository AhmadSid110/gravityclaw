import asyncio
from pathlib import Path
import pytest
from gravityclaw.execution import ContainerSpec, HostExecutionBackend, AgyHostSpecFactory
from gravityclaw.harness import HarnessCompiler, HarnessContext


@pytest.mark.asyncio
async def test_host_execution_backend_runs_directly_on_host(tmp_path: Path):
    backend = HostExecutionBackend()
    test_file = tmp_path / "output.txt"
    spec = ContainerSpec(
        run_id="run-123",
        workspace_id="ws-123",
        workspace=tmp_path,
        image="host",
        command=("python3", "-c", f"import sys; open('{test_file}', 'w').write('executed_on_host'); print('line1'); sys.stderr.write('err1\\n')"),
        environment={"CUSTOM_VAR": "test_val"},
    )

    snapshot = await backend.start(spec)
    assert snapshot.running is True
    assert snapshot.state == "running"

    # Wait briefly for process completion
    for _ in range(50):
        await asyncio.sleep(0.05)
        inspected = await backend.inspect(snapshot.external_id)
        if inspected and not inspected.running:
            break

    assert inspected is not None
    assert inspected.running is False
    assert inspected.exit_code == 0
    assert test_file.read_text() == "executed_on_host"

    logs = await backend.logs(snapshot.external_id)
    stdout_lines = [log.line for log in logs if log.source == "stdout"]
    stderr_lines = [log.line for log in logs if log.source == "stderr"]
    assert "line1" in stdout_lines
    assert "err1" in stderr_lines

    await backend.remove(snapshot.external_id)
    assert await backend.inspect(snapshot.external_id) is None


def test_harness_compiler_host_runtime_prompt():
    compiler = HarnessCompiler()
    ctx = HarnessContext(
        model="claude-3-5-sonnet",
        provider="Anthropic",
        channel="web",
        workspace="/home/ubuntu/gravityclaw/workspaces/project-a",
        host_user="ubuntu",
        host_name="GravityClaw VPS",
        sandbox_enabled=False,
        execution_target="host",
    )
    prompt = compiler.compile(ctx)

    assert "You are operating as GravityClaw on the host machine." in prompt
    assert "Host terminal: available" in prompt
    assert "Host: GravityClaw VPS" in prompt
    assert "User: ubuntu" in prompt
    assert "Working directory: current workspace (/home/ubuntu/gravityclaw/workspaces/project-a)" in prompt
    assert "Filesystem access: host filesystem subject to OS permissions" in prompt
    assert "Network access: available" in prompt
    assert "System services: accessible according to OS permissions" in prompt
    assert "Elevated commands may require approval" in prompt
    assert "There is no sandboxed execution environment for this run." in prompt

    # Verify no sandbox terminology
    assert "workspace container" not in prompt
    assert "sandbox terminal" not in prompt
    assert "isolated runtime" not in prompt
