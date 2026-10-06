import os
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()


def _optional_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


def _optional_bool(name: str, default: bool | None = None) -> bool | None:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


class Config:
    SANDBOX_BASE_URL = os.getenv("SANDBOX_BASE_URL", "http://39.98.58.9:28080").rstrip(
        "/"
    )
    SANDBOX_API_KEY = os.getenv("SANDBOX_API_KEY", "").strip()
    # 设置后走 docker exec，跳过沙箱 HTTP API（本地 python-server 异常时可用）
    SANDBOX_DOCKER_CONTAINER = os.getenv("SANDBOX_DOCKER_CONTAINER", "").strip()

    # APIG 管理面：创建 / 刷新沙箱实例
    SANDBOX_APIG_ENDPOINT = os.getenv("SANDBOX_APIG_ENDPOINT", "").rstrip("/")
    SANDBOX_APIG_HW_ID = os.getenv("SANDBOX_APIG_HW_ID", "").strip()
    SANDBOX_APIG_HW_APPKEY = os.getenv("SANDBOX_APIG_HW_APPKEY", "").strip()
    SANDBOX_TEMPLATE_ID = os.getenv("SANDBOX_TEMPLATE_ID", "").strip()
    # 创建时的默认生命周期（秒），平台默认过期销毁为 900s
    SANDBOX_INSTANCE_TIMEOUT = _optional_int("SANDBOX_INSTANCE_TIMEOUT", 900)
    # 刷新时续期时长（秒），接口硬上限 1800
    SANDBOX_REFRESH_DURATION = int(os.getenv("SANDBOX_REFRESH_DURATION", "900"))
    # 剩余 TTL 低于该阈值且仍有任务时才 refresh
    SANDBOX_KEEPALIVE_MARGIN_SECONDS = float(
        os.getenv("SANDBOX_KEEPALIVE_MARGIN_SECONDS", "120")
    )
    SANDBOX_KEEPALIVE_INTERVAL_SECONDS = float(
        os.getenv("SANDBOX_KEEPALIVE_INTERVAL_SECONDS", "15")
    )
    SANDBOX_READY_TIMEOUT_SECONDS = float(
        os.getenv("SANDBOX_READY_TIMEOUT_SECONDS", "15")
    )

    # 创建沙箱时的用户空间绑定（APIG body.X-mounts）；workspaceId 为空则不传
    SANDBOX_X_MOUNTS_WORKSPACE_ID = os.getenv(
        "SANDBOX_X_MOUNTS_WORKSPACE_ID", ""
    ).strip()
    SANDBOX_X_MOUNTS_SUBPATH = os.getenv("SANDBOX_X_MOUNTS_SUBPATH", "").strip()
    SANDBOX_X_MOUNTS_MOUNT_PATH = os.getenv("SANDBOX_X_MOUNTS_MOUNT_PATH", "").strip()
    SANDBOX_X_MOUNTS_READ_ONLY = _optional_bool("SANDBOX_X_MOUNTS_READ_ONLY")

    # APIG HTTPS：隔离网自签/内网证书可关校验，或指定 CA 文件
    # verify=false 时跳过证书校验；CA_BUNDLE 非空时优先用作 verify 路径
    SANDBOX_APIG_SSL_VERIFY = _optional_bool("SANDBOX_APIG_SSL_VERIFY", True)
    SANDBOX_APIG_CA_BUNDLE = os.getenv("SANDBOX_APIG_CA_BUNDLE", "").strip()
    # TLS 版本：空=系统默认；隔离网握手超时可试 1.2
    SANDBOX_APIG_TLS_VERSION = os.getenv("SANDBOX_APIG_TLS_VERSION", "1.2").strip()
    SANDBOX_APIG_TIMEOUT_SECONDS = float(
        os.getenv("SANDBOX_APIG_TIMEOUT_SECONDS", "120")
    )
    SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS = float(
        os.getenv("SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS", "30")
    )
    SANDBOX_APIG_PROXY = os.getenv("SANDBOX_APIG_PROXY", "").strip()

    # OpenCode 模型，格式 provider/model，例如 local/Qwen3.6-35B-A3B-oQ4-mtp
    OPENCODE_MODEL = os.getenv(
        "OPENCODE_MODEL", "local/Qwen3.6-35B-A3B-oQ4-mtp"
    ).strip()
    OPENCODE_TIMEOUT_SECONDS = float(os.getenv("OPENCODE_TIMEOUT_SECONDS", "600"))
    WORKER_MAX_WORKERS = int(os.getenv("WORKER_MAX_WORKERS", "2"))
    FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
    FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") in {"1", "true", "True", "yes"}

    # 日志：级别 + 可选落盘路径（隔离网环境便于远程捞文件）
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    LOG_FILE = os.getenv("LOG_FILE", "").strip()

    @classmethod
    def runtime_mode(cls) -> str:
        if cls.SANDBOX_DOCKER_CONTAINER:
            return "docker"
        return "apig"

    @classmethod
    def build_x_mounts(cls) -> Optional[list[dict[str, Any]]]:
        """组装创建沙箱请求的 X-mounts（对象数组）；未配置 workspaceId 时返回 None。"""
        workspace_id = cls.SANDBOX_X_MOUNTS_WORKSPACE_ID
        if not workspace_id:
            return None
        item: dict[str, Any] = {"workspaceId": workspace_id}
        if cls.SANDBOX_X_MOUNTS_SUBPATH:
            item["subpath"] = cls.SANDBOX_X_MOUNTS_SUBPATH
        if cls.SANDBOX_X_MOUNTS_MOUNT_PATH:
            item["mountPath"] = cls.SANDBOX_X_MOUNTS_MOUNT_PATH
        if cls.SANDBOX_X_MOUNTS_READ_ONLY is not None:
            item["readOnly"] = cls.SANDBOX_X_MOUNTS_READ_ONLY
        return [item]

    @classmethod
    def apig_ssl_verify(cls) -> bool | str:
        """httpx verify：CA 文件路径，或 True/False。"""
        if cls.SANDBOX_APIG_CA_BUNDLE:
            return cls.SANDBOX_APIG_CA_BUNDLE
        return bool(cls.SANDBOX_APIG_SSL_VERIFY)