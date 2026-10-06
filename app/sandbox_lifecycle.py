from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import ssl
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
import requests
from requests.adapters import HTTPAdapter

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
        "Accept": "application/json",
        "Connection": "close",
        "User-Agent": "sandbox-proxy/1.0",
    }


def _apig_base() -> str:
    endpoint = Config.SANDBOX_APIG_ENDPOINT
    if not endpoint:
        raise SandboxLifecycleError("SANDBOX_APIG_ENDPOINT is required")
    return endpoint.rstrip("/")


def _apig_ssl_context() -> ssl.SSLContext:
    """尽量贴近 Postman：可关校验、放宽密码套件，默认不锁死 TLS 上限。"""
    verify = Config.apig_ssl_verify()
    if isinstance(verify, str):
        ctx = ssl.create_default_context(cafile=verify)
    elif verify:
        ctx = ssl.create_default_context()
    else:
        # 比 create_default_context + CERT_NONE 更接近 Postman 关 SSL 校验
        ctx = ssl._create_unverified_context()

    tls = (Config.SANDBOX_APIG_TLS_VERSION or "auto").strip().lower()
    if tls in {"1.2", "tlsv1.2", "tls1.2"}:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    elif tls in {"1.3", "tlsv1.3", "tls1.3"}:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
        ctx.maximum_version = ssl.TLSVersion.TLSv1_3
    elif tls in {"", "auto", "default"}:
        # 只要求最低 1.2，允许协商到 1.3（Postman 默认行为）
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    else:
        logger.warning("未知 SANDBOX_APIG_TLS_VERSION=%s，使用 auto", tls)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    if Config.SANDBOX_APIG_SSL_COMPAT:
        try:
            # OpenSSL 3 默认 SECLEVEL 偏高，部分企业网关握手会挂起
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError as exc:
            logger.warning("设置兼容密码套件失败，继续默认套件: %s", exc)
        # 允许更老的握手行为（OpenSSL 3）
        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT

    return ctx


def _resolve_hosts(hostname: str, port: int) -> list[tuple[str, int]]:
    """返回候选 (ip, family)，可按配置优先 IPv4。"""
    ipv4: list[tuple[str, int]] = []
    ipv6: list[tuple[str, int]] = []
    try:
        for fam, _, _, _, sockaddr in socket.getaddrinfo(
            hostname, port, type=socket.SOCK_STREAM
        ):
            ip = sockaddr[0]
            if fam == socket.AF_INET:
                ipv4.append((ip, fam))
            elif fam == socket.AF_INET6:
                ipv6.append((ip, fam))
    except socket.gaierror as exc:
        logger.error("DNS解析失败 host=%s port=%s 错误=%s", hostname, port, exc)
        raise SandboxLifecycleError(
            f"DNS resolve failed for {hostname}:{port}: {exc}"
        ) from exc

    ordered = (ipv4 + ipv6) if Config.SANDBOX_APIG_PREFER_IPV4 else (ipv6 + ipv4)
    if not ordered:
        raise SandboxLifecycleError(f"DNS resolve empty for {hostname}:{port}")
    logger.info(
        "APIG DNS解析 host=%s port=%s 优先IPv4=%s 候选=%s",
        hostname,
        port,
        Config.SANDBOX_APIG_PREFER_IPV4,
        [f"{ip}/{'v4' if fam == socket.AF_INET else 'v6'}" for ip, fam in ordered],
    )
    return ordered


def _probe_tcp(ip: str, port: int, family: int, timeout: float) -> None:
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    started = time.monotonic()
    try:
        sock.connect((ip, port))
        logger.info(
            "APIG TCP连通 目标=%s:%s 耗时毫秒=%.1f",
            ip,
            port,
            (time.monotonic() - started) * 1000,
        )
    finally:
        sock.close()


def _log_env_proxy_hint() -> None:
    env_keys = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY")
    found = {k: os.environ.get(k) for k in env_keys if os.environ.get(k)}
    if found:
        logger.warning(
            "检测到系统代理环境变量=%s；当前 TRUST_ENV=%s。"
            "若 Postman 未走代理而 Python 走了（或反过来），会导致握手超时。"
            "可设 SANDBOX_APIG_TRUST_ENV=false 并按需填 SANDBOX_APIG_PROXY",
            found,
            Config.SANDBOX_APIG_TRUST_ENV,
        )


def _apig_client(timeout: float | None = None) -> httpx.Client:
    verify = Config.apig_ssl_verify()
    ssl_label = verify if isinstance(verify, str) else ("开启" if verify else "关闭")
    timeout_s = (
        Config.SANDBOX_APIG_TIMEOUT_SECONDS if timeout is None else timeout
    )
    connect_s = Config.SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS
    proxy = Config.SANDBOX_APIG_PROXY or None
    tls = Config.SANDBOX_APIG_TLS_VERSION or "auto"
    logger.info(
        "创建APIG(httpx)客户端 超时秒=%s 连接超时秒=%s SSL校验=%s TLS=%s "
        "兼容套件=%s 代理=%s TRUST_ENV=%s 地址=%s",
        timeout_s,
        connect_s,
        ssl_label,
        tls,
        Config.SANDBOX_APIG_SSL_COMPAT,
        proxy or "(无)",
        Config.SANDBOX_APIG_TRUST_ENV,
        Config.SANDBOX_APIG_ENDPOINT or "(未配置)",
    )
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout_s, connect=connect_s),
        "verify": _apig_ssl_context(),
        "trust_env": bool(Config.SANDBOX_APIG_TRUST_ENV),
        "http2": False,
    }
    if proxy:
        kwargs["proxy"] = proxy
    return httpx.Client(**kwargs)


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    """先解析并优先连 IPv4，TLS 使用显式 server_hostname（SNI）。"""

    def __init__(
        self,
        host: str,
        port: int | None = None,
        *,
        timeout: float = 30.0,
        context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._resolved_ip: Optional[str] = None

    def connect(self) -> None:
        port = self.port or 443
        candidates = _resolve_hosts(self.host, port)
        connect_timeout = self.timeout if self.timeout is not None else 30.0
        last_exc: Optional[Exception] = None
        for ip, fam in candidates:
            sock: Optional[socket.socket] = None
            try:
                logger.info(
                    "APIG stdlib 尝试连接 host=%s ip=%s port=%s family=%s",
                    self.host,
                    ip,
                    port,
                    "IPv4" if fam == socket.AF_INET else "IPv6",
                )
                _probe_tcp(ip, port, fam, float(connect_timeout))
                sock = socket.socket(fam, socket.SOCK_STREAM)
                sock.settimeout(connect_timeout)
                sock.connect((ip, port))
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                self._resolved_ip = ip
                logger.info(
                    "APIG TLS握手成功 host=%s ip=%s 协议=%s 套件=%s",
                    self.host,
                    ip,
                    self.sock.version(),
                    self.sock.cipher(),
                )
                return
            except Exception as exc:  # 尝试下一个 IP
                last_exc = exc
                logger.warning(
                    "APIG连接失败 host=%s ip=%s 错误=%s",
                    self.host,
                    ip,
                    preview(exc, 300),
                )
                if sock is not None:
                    try:
                        sock.close()
                    except Exception:
                        pass
                self.sock = None
                continue
        raise SandboxLifecycleError(
            f"APIG connect/handshake failed for {self.host}:{port}: {last_exc}"
        )


def _apig_post_stdlib(
    url: str, *, json_body: dict[str, Any], action: str
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SandboxLifecycleError(
            f"stdlib backend currently supports https only, got {parsed.scheme}"
        )
    if Config.SANDBOX_APIG_PROXY:
        raise SandboxLifecycleError(
            "stdlib backend does not support SANDBOX_APIG_PROXY; "
            "set SANDBOX_APIG_HTTP_BACKEND=requests (or httpx) or clear proxy"
        )

    host = parsed.hostname
    if not host:
        raise SandboxLifecycleError(f"invalid APIG url: {url}")
    port = parsed.port or 443
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
    headers = _apig_headers()
    headers["Content-Length"] = str(len(body_bytes))
    headers["Host"] = host

    connect_s = Config.SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS
    total_s = Config.SANDBOX_APIG_TIMEOUT_SECONDS
    ctx = _apig_ssl_context()
    verify = Config.apig_ssl_verify()
    ssl_label = verify if isinstance(verify, str) else ("开启" if verify else "关闭")
    logger.info(
        "创建APIG(stdlib)客户端 动作=%s host=%s port=%s 路径=%s "
        "连接超时秒=%s 总超时秒=%s SSL校验=%s TLS=%s 兼容套件=%s 优先IPv4=%s",
        action,
        host,
        port,
        path,
        connect_s,
        total_s,
        ssl_label,
        Config.SANDBOX_APIG_TLS_VERSION or "auto",
        Config.SANDBOX_APIG_SSL_COMPAT,
        Config.SANDBOX_APIG_PREFER_IPV4,
    )

    conn = _IPv4HTTPSConnection(
        host, port, timeout=connect_s, context=ctx
    )
    try:
        conn.connect()
        # 握手后读写改用总超时，避免长响应被连接超时掐断
        if conn.sock is not None:
            conn.sock.settimeout(total_s)
        conn.request("POST", path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        text = raw.decode("utf-8", errors="replace")
        logger.info(
            "APIG请求完成 动作=%s 后端=stdlib HTTP状态码=%s 正文=%s",
            action,
            resp.status,
            preview(text, 800),
        )
        if resp.status >= 400:
            raise SandboxLifecycleError(
                f"{action} failed: HTTP {resp.status} {preview(text, 500)}"
            )
        try:
            return json.loads(text)
        except ValueError as exc:
            raise SandboxLifecycleError(f"{action} invalid JSON: {exc}") from exc
    finally:
        conn.close()


def _apig_post_httpx(
    url: str, *, json_body: dict[str, Any], action: str
) -> dict[str, Any]:
    with _apig_client() as client:
        resp = client.post(url, headers=_apig_headers(), json=json_body)
        logger.info(
            "APIG请求完成 动作=%s 后端=httpx HTTP状态码=%s Content-Type=%s 正文=%s",
            action,
            resp.status_code,
            resp.headers.get("content-type"),
            preview(resp.text, 800),
        )
        resp.raise_for_status()
        return resp.json()


class _APIGSSLAdapter(HTTPAdapter):
    """把自定义 SSLContext 注入 urllib3，供 requests 使用。"""

    def init_poolmanager(self, *args: Any, **kwargs: Any):
        kwargs["ssl_context"] = _apig_ssl_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any):
        kwargs["ssl_context"] = _apig_ssl_context()
        return super().proxy_manager_for(*args, **kwargs)


def _apig_post_requests(
    url: str, *, json_body: dict[str, Any], action: str
) -> dict[str, Any]:
    """默认后端：requests + urllib3，行为更接近 Postman。"""
    verify = Config.apig_ssl_verify()
    ssl_label = verify if isinstance(verify, str) else ("开启" if verify else "关闭")
    connect_s = Config.SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS
    total_s = Config.SANDBOX_APIG_TIMEOUT_SECONDS
    proxy = Config.SANDBOX_APIG_PROXY or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # 解析日志：方便对照 Postman 实际打到的地址
    parsed = urlparse(url)
    if parsed.hostname:
        try:
            _resolve_hosts(parsed.hostname, parsed.port or 443)
        except SandboxLifecycleError:
            pass

    logger.info(
        "创建APIG(requests)客户端 动作=%s 超时=(连接%.1f,总%.1f) SSL校验=%s "
        "TLS=%s 兼容套件=%s 优先IPv4=%s TRUST_ENV=%s 代理=%s",
        action,
        connect_s,
        total_s,
        ssl_label,
        Config.SANDBOX_APIG_TLS_VERSION or "auto",
        Config.SANDBOX_APIG_SSL_COMPAT,
        Config.SANDBOX_APIG_PREFER_IPV4,
        Config.SANDBOX_APIG_TRUST_ENV,
        proxy or "(无)",
    )

    session = requests.Session()
    session.trust_env = bool(Config.SANDBOX_APIG_TRUST_ENV)
    adapter = _APIGSSLAdapter(max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", HTTPAdapter(max_retries=0))

    # urllib3 全局 gai family：仅在本次请求窗口内优先 IPv4
    import urllib3.util.connection as urllib3_cn

    previous_gai = getattr(urllib3_cn, "allowed_gai_family", None)

    def _ipv4_only() -> int:
        return socket.AF_INET

    try:
        if Config.SANDBOX_APIG_PREFER_IPV4:
            urllib3_cn.allowed_gai_family = _ipv4_only  # type: ignore[attr-defined]

        # verify 仍传给 requests：CA 路径时双保险；自定义 context 已在 adapter
        req_verify: Any = True
        if isinstance(verify, str):
            req_verify = verify
        elif not verify:
            req_verify = False

        resp = session.post(
            url,
            headers=_apig_headers(),
            json=json_body,
            timeout=(connect_s, total_s),
            verify=req_verify,
            proxies=proxies,
        )
        logger.info(
            "APIG请求完成 动作=%s 后端=requests HTTP状态码=%s Content-Type=%s 正文=%s",
            action,
            resp.status_code,
            resp.headers.get("content-type"),
            preview(resp.text, 800),
        )
        if resp.status_code >= 400:
            raise SandboxLifecycleError(
                f"{action} failed: HTTP {resp.status_code} {preview(resp.text, 500)}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise SandboxLifecycleError(f"{action} invalid JSON: {exc}") from exc
    except requests.RequestException as exc:
        raise SandboxLifecycleError(f"{action} failed: {exc}") from exc
    finally:
        if previous_gai is not None:
            urllib3_cn.allowed_gai_family = previous_gai  # type: ignore[attr-defined]
        session.close()


def _wrap_http_error(action: str, exc: Exception) -> SandboxLifecycleError:
    text = str(exc)
    lower = text.lower()
    tip = ""
    if "handshake" in lower and "timeout" in lower:
        tip = (
            "；疑似HTTPS握手超时。Postman能通而Python不通时优先检查："
            "1) 使用 requests 后端：SANDBOX_APIG_HTTP_BACKEND=requests；"
            "2) 系统代理是否不一致（SANDBOX_APIG_TRUST_ENV=false）；"
            "3) 是否走了IPv6（SANDBOX_APIG_PREFER_IPV4=true）；"
            "4) 密码套件过严（SANDBOX_APIG_SSL_COMPAT=true）；"
            "5) TLS用auto：SANDBOX_APIG_TLS_VERSION=auto"
        )
    elif "CERTIFICATE_VERIFY_FAILED" in text:
        tip = (
            "；疑似HTTPS证书校验失败，隔离网可在.env设置 "
            "SANDBOX_APIG_SSL_VERIFY=false，或配置 SANDBOX_APIG_CA_BUNDLE=企业CA路径"
        )
    elif "ssl" in lower:
        tip = (
            "；HTTPS/SSL失败。可尝试 SANDBOX_APIG_SSL_VERIFY=false、"
            "SANDBOX_APIG_SSL_COMPAT=true、SANDBOX_APIG_HTTP_BACKEND=requests"
        )
    return SandboxLifecycleError(f"{action} failed: {exc}{tip}")


def _is_handshake_timeout(exc: Exception) -> bool:
    text = str(exc).lower()
    return ("handshake" in text and "timeout" in text) or "timed out" in text


def _backend_fallback_order(primary: str) -> list[str]:
    """主后端 + 备用顺序：requests -> stdlib -> httpx。"""
    order = ["requests", "stdlib", "httpx"]
    primary = (primary or "requests").lower()
    if primary not in order:
        primary = "requests"
    return [primary] + [b for b in order if b != primary]


def _dispatch_apig_post(
    backend: str, url: str, *, json_body: dict[str, Any], action: str
) -> dict[str, Any]:
    if backend == "requests":
        return _apig_post_requests(url, json_body=json_body, action=action)
    if backend == "stdlib":
        return _apig_post_stdlib(url, json_body=json_body, action=action)
    if backend == "httpx":
        return _apig_post_httpx(url, json_body=json_body, action=action)
    raise SandboxLifecycleError(f"unknown APIG HTTP backend: {backend}")


def _apig_post(url: str, *, json_body: dict[str, Any], action: str) -> dict[str, Any]:
    _log_env_proxy_hint()
    backend = (Config.SANDBOX_APIG_HTTP_BACKEND or "requests").lower()
    attempts = 3
    last_exc: Optional[Exception] = None
    backends = _backend_fallback_order(backend)

    for be in backends:
        for i in range(1, attempts + 1):
            try:
                logger.info(
                    "APIG发起请求 动作=%s 后端=%s 次数=%s/%s url=%s",
                    action,
                    be,
                    i,
                    attempts,
                    url,
                )
                return _dispatch_apig_post(
                    be, url, json_body=json_body, action=action
                )
            except SandboxLifecycleError as exc:
                last_exc = exc
                if _is_handshake_timeout(exc) and i < attempts:
                    logger.warning(
                        "APIG失败将重试 后端=%s 次数=%s/%s 错误=%s",
                        be,
                        i,
                        attempts,
                        preview(exc, 300),
                    )
                    time.sleep(float(i))
                    continue
                if _is_handshake_timeout(exc) and be != backends[-1]:
                    logger.warning(
                        "APIG后端=%s 仍握手失败，切换到 %s 重试 错误=%s",
                        be,
                        backends[backends.index(be) + 1],
                        preview(exc, 300),
                    )
                    break
                if _is_handshake_timeout(exc):
                    raise _wrap_http_error(action, exc) from exc
                raise
            except (httpx.HTTPError, requests.RequestException) as exc:
                last_exc = exc
                if _is_handshake_timeout(exc) and i < attempts:
                    logger.warning(
                        "APIG握手超时，准备重试 动作=%s 后端=%s 次数=%s/%s 错误=%s",
                        action,
                        be,
                        i,
                        attempts,
                        preview(exc, 300),
                    )
                    time.sleep(float(i))
                    continue
                if _is_handshake_timeout(exc) and be != backends[-1]:
                    logger.warning(
                        "APIG后端=%s 握手超时，切换到下一后端",
                        be,
                    )
                    break
                raise _wrap_http_error(action, exc) from exc
            except ValueError as exc:
                raise SandboxLifecycleError(f"{action} invalid JSON: {exc}") from exc
            except OSError as exc:
                last_exc = exc
                if i < attempts:
                    logger.warning(
                        "APIG网络错误将重试 后端=%s 次数=%s/%s 错误=%s",
                        be,
                        i,
                        attempts,
                        preview(exc, 300),
                    )
                    time.sleep(float(i))
                    continue
                if be != backends[-1]:
                    logger.warning(
                        "APIG后端=%s 网络失败，切换到下一后端 错误=%s",
                        be,
                        preview(exc, 300),
                    )
                    break
                raise _wrap_http_error(action, exc) from exc

    raise _wrap_http_error(action, last_exc or RuntimeError("unknown APIG error"))


def _check_response(payload: dict[str, Any], action: str) -> dict[str, Any]:
    # APIG 业务成功码为 200；兼容历史/部分环境返回 0
    code = payload.get("code")
    if code is not None and code not in (0, 200):
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
