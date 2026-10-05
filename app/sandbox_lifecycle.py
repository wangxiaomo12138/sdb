from __future__ import annotations

import logging
import time
from typing import Any, Optional

import httpx

from app.config import Config

logger = logging.getLogger(__name__)


class SandboxLifecycleError(Exception):
    """APIG 创建、刷新或等待就绪失败时抛出。"""


def _apig_headers() -> dict[str, str]:
    hw_id = Config.SANDBOX_APIG_HW_ID
    hw_appkey = Config.SANDBOX_APIG_HW_APPKEY
    if not hw_id or not hw_appkey:
        raise SandboxLifecycleError(
            "SANDBOX_APIG_HW_ID and SANDBOX_APIG_HW_APPKEY are required"
        )
    return {
        "X-HW-ID": hw_id,
        "X-HW-APPKEY": hw_appkey,
        "Content-Type": "application/json",
    }


def _apig_base() -> str:
    endpoint = Config.SANDBOX_APIG_ENDPOINT
    if not endpoint:
        raise SandboxLifecycleError("SANDBOX_APIG_ENDPOINT is required")
    return endpoint.rstrip("/")


def _check_response(payload: dict[str, Any], action: str) -> dict[str, Any]:
    code = payload.get("code")
    if code is not None and code != 0:
        message = payload.get("message") or f"{action} failed with code={code}"
        raise SandboxLifecycleError(str(message))
    return payload


def create_sandbox(
    *,
    template_id: Optional[str] = None,
    timeout: Optional[int] = None,
) -> dict[str, Any]:
    """通过 APIG 创建沙箱实例，返回含 sandboxId 的 data 对象。"""
    tid = (template_id or Config.SANDBOX_TEMPLATE_ID or "").strip()
    if not tid:
        raise SandboxLifecycleError("SANDBOX_TEMPLATE_ID is required")

    body: dict[str, Any] = {"templateId": tid}
    instance_timeout = (
        timeout if timeout is not None else Config.SANDBOX_INSTANCE_TIMEOUT
    )
    if instance_timeout is not None:
        body["timeout"] = instance_timeout

    url = f"{_apig_base()}/livefunction/sandboxes"
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, headers=_apig_headers(), json=body)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise SandboxLifecycleError(f"create sandbox request failed: {exc}") from exc
    except ValueError as exc:
        raise SandboxLifecycleError(f"create sandbox invalid JSON: {exc}") from exc

    _check_response(payload, "create sandbox")
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("sandboxId"):
        raise SandboxLifecycleError("create sandbox response missing data.sandboxId")
    return data


def refresh_sandbox(
    sandbox_id: str, *, duration: Optional[int] = None
) -> dict[str, Any]:
    """刷新沙箱存活时间，返回接口原始响应。"""
    if not sandbox_id:
        raise SandboxLifecycleError("sandbox_id is required")

    ttl = duration if duration is not None else Config.SANDBOX_REFRESH_DURATION
    # 接口硬上限 1800 秒
    ttl = min(int(ttl), 1800)

    url = f"{_apig_base()}/livefunction/sandboxes/refresh/{sandbox_id}"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url, headers=_apig_headers(), json={"duration": ttl}
            )
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise SandboxLifecycleError(f"refresh sandbox request failed: {exc}") from exc
    except ValueError as exc:
        raise SandboxLifecycleError(f"refresh sandbox invalid JSON: {exc}") from exc

    return _check_response(payload, "refresh sandbox")


def wait_until_running(
    sandbox_id: str,
    *,
    initial_status: Optional[str] = None,
) -> str:
    """等待沙箱进入可用状态。

    创建接口可能直接返回 status，文档没有按 id 查询的接口。
    已是 running（或未返回 status）则立即返回；stopped/error 直接失败。
    starting 时短等一段时间后继续（不调用 refresh，避免无任务时续期）。
    """
    status = (initial_status or "").lower()
    if status in {"", "running"}:
        return sandbox_id
    if status in {"stopped", "error"}:
        raise SandboxLifecycleError(f"sandbox {sandbox_id} in bad status={status}")

    wait_s = Config.SANDBOX_READY_TIMEOUT_SECONDS
    logger.info("sandbox %s status=%s, waiting %.1ss without refresh", sandbox_id, status, wait_s)
    time.sleep(wait_s)
    return sandbox_id


def create_and_wait() -> tuple[str, int]:
    """创建沙箱并等到可用，返回 (sandboxId, ttl 秒)。"""
    data = create_sandbox()
    sandbox_id = str(data["sandboxId"])
    status = data.get("status")
    wait_until_running(sandbox_id, initial_status=str(status) if status else None)
    ttl = data.get("timeout")
    if ttl is None:
        ttl = Config.SANDBOX_INSTANCE_TIMEOUT or 900
    return sandbox_id, int(ttl)
