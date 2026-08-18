"""Query the running AGY process's local Connect RPC for live quota information.

The AGY binary hosts an HTTPS Connect RPC server on 127.0.0.1. We probe the
listening ports of the `agy` process and POST to:
  /exa.language_server_pb.LanguageServerService/GetUserStatus

This returns per-model quota fractions (5-hour rolling window) and reset times.
The weekly limit is NOT exposed by this endpoint.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from typing import Any


async def fetch_agy_quota() -> dict[str, Any]:
    """Return normalized quota data from the local AGY Connect RPC."""
    ports = _discover_agy_ports()
    if not ports:
        return {"available": False, "error": "No running AGY process detected", "models": []}

    for port in ports:
        result = await _query_get_user_status(port)
        if result is not None:
            return _normalize_response(result)

    return {"available": False, "error": "AGY RPC endpoint not responding on any port", "models": []}


def _discover_agy_ports() -> list[int]:
    """Find loopback ports the AGY process is listening on via `ss`."""
    try:
        completed = subprocess.run(
            ["ss", "-tulpn"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    ports: list[int] = []
    for line in completed.stdout.splitlines():
        if "agy" not in line:
            continue
        # Extract port from 127.0.0.1:PORT
        for part in line.split():
            if "127.0.0.1:" in part:
                try:
                    port_str = part.split("127.0.0.1:")[-1].split()[0]
                    ports.append(int(port_str))
                except (ValueError, IndexError):
                    continue
    return sorted(set(ports))


async def _query_get_user_status(port: int) -> dict[str, Any] | None:
    """POST the GetUserStatus RPC to the given port, return parsed JSON or None."""
    import httpx

    url = f"https://127.0.0.1:{port}/exa.language_server_pb.LanguageServerService/GetUserStatus"
    payload = {"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}}

    try:
        async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                },
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if "userStatus" in data:
                return data
            return None
    except Exception:
        return None


def _normalize_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Transform the raw GetUserStatus response into a frontend-friendly shape."""
    user_status = raw.get("userStatus", {})
    model_data = user_status.get("cascadeModelConfigData", {})
    configs = model_data.get("clientModelConfigs", [])

    plan_status = user_status.get("planStatus", {})
    plan_info = plan_status.get("planInfo", {})
    user_tier = user_status.get("userTier", {})

    # Group quota by pool (Gemini models share one pool, Claude/GPT have their own)
    seen_pools: dict[str, dict[str, Any]] = {}
    model_quotas: list[dict[str, Any]] = []

    for cfg in configs:
        quota_info = cfg.get("quotaInfo", {})
        remaining = quota_info.get("remainingFraction")
        reset_time = quota_info.get("resetTime")
        model_id = cfg.get("modelId", "")
        label = cfg.get("label", model_id)

        # Pool grouping: infer from model provider prefix
        pool_name = _infer_pool_name(model_id, label)
        pool_key = pool_name  # Group by provider, not by reset time
        if pool_key not in seen_pools:
            seen_pools[pool_key] = {
                "pool": pool_name,
                "remaining_fraction": remaining,
                "remaining_percent": round(remaining * 100, 1) if remaining is not None else None,
                "reset_time": reset_time,
                "models": [],
            }
        # Update pool fraction if this model has data and pool doesn't yet
        if remaining is not None and seen_pools[pool_key]["remaining_fraction"] is None:
            seen_pools[pool_key]["remaining_fraction"] = remaining
            seen_pools[pool_key]["remaining_percent"] = round(remaining * 100, 1)
            seen_pools[pool_key]["reset_time"] = reset_time

        seen_pools[pool_key]["models"].append({
            "id": model_id,
            "label": label,
        })

        model_quotas.append({
            "id": model_id,
            "label": label,
            "remaining_fraction": remaining,
            "remaining_percent": round(remaining * 100, 1) if remaining is not None else None,
            "reset_time": reset_time,
        })

    pools = list(seen_pools.values())

    return {
        "available": True,
        "plan": {
            "name": user_tier.get("name") or plan_info.get("planName", "Unknown"),
            "tier_id": user_tier.get("id"),
            "available_prompt_credits": plan_status.get("availablePromptCredits"),
            "available_flow_credits": plan_status.get("availableFlowCredits"),
        },
        "pools": pools,
        "models": model_quotas,
        "window": "5-hour rolling",
        "note": "Weekly limit is not exposed by the AGY local RPC endpoint.",
    }


def _infer_pool_name(model_id: str, label: str) -> str:
    """Guess the pool name from the model ID prefix."""
    lower = model_id.lower()
    if "gemini" in lower:
        return "Gemini"
    if "claude" in lower:
        return "Claude"
    if "gpt" in lower:
        return "GPT-OSS"
    # Fallback from label
    if "Gemini" in label:
        return "Gemini"
    if "Claude" in label:
        return "Claude"
    return "Other"
