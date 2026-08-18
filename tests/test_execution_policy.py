import pytest
from gravityclaw.execution_policy import ExecutionPolicy, PolicyDecision


def test_balanced_policy_normal_commands():
    policy = ExecutionPolicy(mode="balanced")
    for cmd in [
        "pwd",
        "git status",
        "git log -n 5",
        "npm test",
        "npm run build",
        "python3 -m pytest",
        "journalctl --user -u gravityclaw",
        "systemctl --user status gravityclaw",
        "ps aux",
        "curl -s https://example.com",
        "ls -la /home/ubuntu",
        "cat package.json",
        "mkdir -p src/utils",
    ]:
        decision = policy.evaluate(cmd)
        assert decision.allowed is True, f"Failed for {cmd}"
        assert decision.requires_approval is False, f"Approval wrongly required for {cmd}"
        assert decision.classification == "normal"


def test_balanced_policy_elevated_commands():
    policy = ExecutionPolicy(mode="balanced")
    for cmd in [
        "sudo systemctl restart nginx",
        "sudo apt-get update",
        "reboot",
        "shutdown -r now",
        "systemctl restart nginx",
        "useradd -m newuser",
        "ufw allow 80/tcp",
        "chmod 777 /etc/passwd",
    ]:
        decision = policy.evaluate(cmd)
        assert decision.allowed is True, f"Failed for {cmd}"
        assert decision.requires_approval is True, f"Approval not requested for {cmd}"
        assert decision.classification == "elevated"


def test_destructive_commands_blocked():
    policy = ExecutionPolicy(mode="balanced")
    for cmd in [
        "rm -rf /",
        "rm -rf /*",
        "rm -rf /etc",
        "rm -rf /var",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        ":(){ :|:& };:",
    ]:
        decision = policy.evaluate(cmd)
        assert decision.allowed is False, f"Destructive command should not be allowed directly: {cmd}"
        assert decision.requires_approval is True
        assert decision.classification == "destructive"


def test_full_mode_autonomy():
    policy = ExecutionPolicy(mode="full")
    decision = policy.evaluate("sudo systemctl restart nginx")
    assert decision.allowed is True
    assert decision.requires_approval is False


def test_restricted_mode():
    policy = ExecutionPolicy(mode="restricted")
    normal = policy.evaluate("git status")
    assert normal.allowed is True
    assert normal.requires_approval is False

    elevated = policy.evaluate("sudo apt update")
    assert elevated.allowed is False
    assert elevated.requires_approval is False
