import os

from dotenv import load_dotenv

load_dotenv()


def _optional_int(name: str, default: int | None = None) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    return int(raw)


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

    # OpenCode 模型，格式 provider/model，例如 local/Qwen3.6-35B-A3B-oQ4-mtp
    OPENCODE_MODEL = os.getenv(
        "OPENCODE_MODEL", "local/Qwen3.6-35B-A3B-oQ4-mtp"
    ).strip()
    OPENCODE_TIMEOUT_SECONDS = float(os.getenv("OPENCODE_TIMEOUT_SECONDS", "600"))
    WORKER_MAX_WORKERS = int(os.getenv("WORKER_MAX_WORKERS", "2"))
    FLASK_HOST = os.getenv("FLASK_HOST", "0.0.0.0")
    FLASK_PORT = int(os.getenv("FLASK_PORT", "5000"))
    FLASK_DEBUG = os.getenv("FLASK_DEBUG", "0") in {"1", "true", "True", "yes"}
