from __future__ import annotations

import logging
import ssl
import time
from typing import Any, Optional

import httpx

from app.config import Config
from app.logging_utils import log_step, preview

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


def _apig_ssl_context() -> ssl.SSLContext:
    verify = Config.apig_ssl_verify()
    if isinstance(verify, str):
        ctx = ssl.create_default_context(cafile=verify)
    else:
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

    tls = (Config.SANDBOX_APIG_TLS_VERSION or "").strip()
    if tls in {"1.2", "TLSv1.2", "tls1.2"}:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    elif tls in {"1.3", "TLSv1.3", "tls1.3"}:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    return ctx


def _apig_client(timeout: float | None = None) -> httpx.Client:
    verify = Config.apig_ssl_verify()
    ssl_label = verify if isinstance(verify, str) else ("开启" if verify else "关闭")
    timeout_s = (
        Config.SANDBOX_APIG_TIMEOUT_SECONDS if timeout is None else timeout
    )
    connect_s = Config.SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS
    proxy = Config.SANDBOX_APIG_PROXY or None
    tls = Config.SANDBOX_APIG_TLS_VERSION or "系统默认"
    logger.info(
        "创建APIG客户端 超时秒=%s 连接超时秒=%s SSL校验=%s TLS版本=%s 代理=%s 地址=%s",
        timeout_s,
        connect_s,
        ssl_label,
        tls,
        proxy or "(无)",
        Config.SANDBOX_APIG_ENDPOINT or "(未配置)",
    )
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout_s, connect=connect_s),
        "verify": _apig_ssl_context(),
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


def _wrap_http_error(action: str, exc: Exception) -> SandboxLifecycleError:
    text = str(exc)
    lower = text.lower()
    tip = ""
    if "handshake" in lower and "timeout" in lower:
        tip = (
            "；疑似HTTPS握手超时（ssl.c handshake timeout）。隔离网常见原因："
            "防火墙/DPI拦截443、需走代理、或仅支持TLS1.2。"
            "可设置 SANDBOX_APIG_TLS_VERSION=1.2、"
            "SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS=60、"
            "SANDBOX_APIG_PROXY=http://host:port，并确认能访问 SANDBOX_APIG_ENDPOINT"
        )
    elif "CERTIFICATE_VERIFY_FAILED" in text:
        tip = (
            "；疑似HTTPS证书校验失败，隔离网可在.env设置 "
            "SANDBOX_APIG_SSL_VERIFY=false，或配置 SANDBOX_APIG_CA_BUNDLE=企业CA路径"
        )
    elif "ssl" in lower:
        tip = (
            "；HTTPS/SSL失败。可尝试 SANDBOX_APIG_SSL_VERIFY=false、"
            "SANDBOX_APIG_TLS_VERSION=1.2，或配置代理 SANDBOX_APIG_PROXY"
        )
    return SandboxLifecycleError(f"{action} failed: {exc}{tip}")


def _is_handshake_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return "handshake" in text and "timeout" in text


def _apig_post(url: str, *, json_body: dict[str, Any], action: str) -> dict[str, Any]:
    attempts = 3
    last_exc: Optional[Exception] = None
    for i in range(1, attempts + 1):
        try:
            with _apig_client() as client:
                resp = client.post(url, headers=_apig_headers(), json=json_body)
                logger.info(
                    "APIG请求完成 动作=%s HTTP状态码=%s Content-Type=%s 正文=%s",
                    action,
                    resp.status_code,
                    resp.headers.get("content-type"),
                    preview(resp.text, 800),
                )
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPError as exc:
            last_exc = exc
            if _is_handshake_timeout(exc) and i < attempts:
                logger.warning(
                    "APIG握手超时，准备重试 动作=%s 次数=%s/%s 错误=%s",
                    action,
                    i,
                    attempts,
                    preview(exc, 300),
                )
                time.sleep(float(i))
                continue
            raise _wrap_http_error(action, exc) from exc
        except ValueError as exc:
            raise SandboxLifecycleError(f"{action} invalid JSON: {exc}") from exc
    raise _wrap_http_error(action, last_exc or RuntimeError("unknown APIG error"))


def _check_response(payload: dict[str, Any], action: str) -> dict[str, Any]:
    code = payload.get("code")
    if code is not None and code != 0:
        message = payload.get("message") or f"{action} failed with code={code}"
        logger.error(
            "APIG业务错误 动作=%s 业务码=%s 消息=%s 响应=%s",
            action,
            code,
            preview(message, 300),
            preview(payload, 500),
        )
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

    x_mounts = Config.build_x_mounts()
    if x_mounts is not None:
        # 与 APIG 文档字段名一致：X-mounts
        body["X-mounts"] = x_mounts

    url = f"{_apig_base()}/livefunction/sandboxes"
    logger.info(
        "调用APIG创建沙箱 地址=%s 模板ID=%s 超时秒=%s X-mounts=%s",
        url,
        tid,
        instance_timeout,
        preview(x_mounts) if x_mounts is not None else "(未配置)",
    )
    with log_step(logger, "APIG创建沙箱", template_id=tid):
        payload = _apig_post(url, json_body=body, action="create sandbox request")

    _check_response(payload, "创建沙箱")
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("sandboxId"):
        raise SandboxLifecycleError("create sandbox response missing data.sandboxId")
    logger.info(
        "APIG创建沙箱成功 sandbox_id=%s 状态=%s 超时秒=%s",
        data.get("sandboxId"),
        data.get("status"),
        data.get("timeout"),
    )
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
    logger.info(
        "调用APIG续期沙箱 地址=%s sandbox_id=%s 续期秒数=%s",
        url,
        sandbox_id,
        ttl,
    )
    with log_step(logger, "APIG续期沙箱", sandbox_id=sandbox_id, duration=ttl):
        payload = _apig_post(
            url, json_body={"duration": ttl}, action="refresh sandbox request"
        )

    return _check_response(payload, "续期沙箱")


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
    logger.info(
        "等待沙箱就绪 sandbox_id=%s 初始状态=%s",
        sandbox_id,
        status or "(空)",
    )
    if status in {"", "running"}:
        logger.info(
            "沙箱无需等待 sandbox_id=%s 状态=%s",
            sandbox_id,
            status or "(空，视为可用)",
        )
        return sandbox_id
    if status in {"stopped", "error"}:
        raise SandboxLifecycleError(f"sandbox {sandbox_id} in bad status={status}")

    wait_s = Config.SANDBOX_READY_TIMEOUT_SECONDS
    logger.info(
        "沙箱尚未就绪，短时等待 sandbox_id=%s 状态=%s 等待秒=%.1f（不续期）",
        sandbox_id,
        status,
        wait_s,
    )
    time.sleep(wait_s)
    logger.info(
        "沙箱等待结束 sandbox_id=%s 已等待秒=%.1f", sandbox_id, wait_s
    )
    return sandbox_id


def create_and_wait() -> tuple[str, int]:
    """创建沙箱并等到可用，返回 (sandboxId, ttl 秒)。"""
    logger.info("开始创建沙箱并等待就绪")
    started = time.monotonic()
    data = create_sandbox()
    sandbox_id = str(data["sandboxId"])
    status = data.get("status")
    wait_until_running(sandbox_id, initial_status=str(status) if status else None)
    ttl = data.get("timeout")
    if ttl is None:
        ttl = Config.SANDBOX_INSTANCE_TIMEOUT or 900
        logger.info(
            "创建响应未带TTL，使用配置值 TTL秒=%s", ttl
        )
    elapsed_ms = (time.monotonic() - started) * 1000
    logger.info(
        "创建并等待完成 sandbox_id=%s TTL秒=%s 耗时毫秒=%.1f",
        sandbox_id,
        int(ttl),
        elapsed_ms,
    )
    return sandbox_id, int(ttl)
