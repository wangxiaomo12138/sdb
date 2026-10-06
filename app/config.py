import json
import os
from typing import Any, Optional

from dotenv import load_dotenv

# 关闭插值，避免 SANDBOX_ENV_VARS 里的 $PATH 被本机环境展开
load_dotenv(interpolate=False)


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

    # 执行 opencode 时注入的环境变量；JSON 对象，空则不附加 export
    SANDBOX_ENV_VARS = os.getenv("SANDBOX_ENV_VARS", "").strip()

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
    # TLS：auto=只设最低1.2不封顶；1.2/1.3=固定版本。Postman 通而 Python 不通时用 auto+compat
    SANDBOX_APIG_TLS_VERSION = os.getenv("SANDBOX_APIG_TLS_VERSION", "auto").strip()
    # 放宽密码套件（DEFAULT:@SECLEVEL=0），对齐 Postman 宽松握手
    SANDBOX_APIG_SSL_COMPAT = _optional_bool("SANDBOX_APIG_SSL_COMPAT", True)
    # 优先连 IPv4，避免 Python 走 IPv6 导致握手挂起（Postman 常走 IPv4）
    SANDBOX_APIG_PREFER_IPV4 = _optional_bool("SANDBOX_APIG_PREFER_IPV4", True)
    # 默认不读取系统 HTTP(S)_PROXY，避免与 Postman 代理环境不一致
    SANDBOX_APIG_TRUST_ENV = _optional_bool("SANDBOX_APIG_TRUST_ENV", False)
    # requests（默认，贴近 Postman）| stdlib | httpx；失败会按序互切
    SANDBOX_APIG_HTTP_BACKEND = (
        os.getenv("SANDBOX_APIG_HTTP_BACKEND", "requests").strip().lower()
        or "requests"
    )
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
    def build_env_vars(cls) -> Optional[dict[str, str]]:
        """解析 SANDBOX_ENV_VARS；未配置或空对象时返回 None。"""
        raw = cls.SANDBOX_ENV_VARS
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"SANDBOX_ENV_VARS must be a JSON object, got {raw!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError(
                f"SANDBOX_ENV_VARS must be a JSON object, got {type(parsed).__name__}"
            )
        env_vars: dict[str, str] = {}
        for key, value in parsed.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("SANDBOX_ENV_VARS keys must be non-empty strings")
            if value is None:
                continue
            if isinstance(value, bool):
                env_vars[key] = "true" if value else "false"
            elif isinstance(value, (str, int, float)):
                env_vars[key] = str(value)
            else:
                raise ValueError(
                    f"SANDBOX_ENV_VARS[{key!r}] must be a string, got {type(value).__name__}"
                )
        return env_vars or None

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