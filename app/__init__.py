from __future__ import annotations

import logging

from flask import Flask

from app.config import Config
from app.logging_utils import mask_secret, setup_logging
from app.models import TaskStore
from app.routes import api_bp
from app.session_sandbox import session_sandbox_manager
from app.worker import TaskWorker

logger = logging.getLogger(__name__)


def _log_startup_summary() -> None:
    mode = Config.runtime_mode()
    mode_label = "Docker本地容器" if mode == "docker" else "APIG远程沙箱"
    logger.info(
        "服务启动 运行模式=%s 日志级别=%s 日志文件=%s 监听=%s:%s 调试=%s "
        "工作线程数=%s OpenCode模型=%s OpenCode超时秒=%s",
        mode_label,
        Config.LOG_LEVEL,
        Config.LOG_FILE or "(仅标准输出)",
        Config.FLASK_HOST,
        Config.FLASK_PORT,
        Config.FLASK_DEBUG,
        Config.WORKER_MAX_WORKERS,
        Config.OPENCODE_MODEL or "(默认)",
        Config.OPENCODE_TIMEOUT_SECONDS,
    )
    if mode == "docker":
        logger.info(
            "Docker模式 容器名=%s 沙箱地址=%s",
            Config.SANDBOX_DOCKER_CONTAINER,
            Config.SANDBOX_BASE_URL,
        )
    else:
        x_mounts = Config.build_x_mounts()
        ssl_verify = Config.apig_ssl_verify()
        ssl_label = (
            ssl_verify if isinstance(ssl_verify, str) else ("开启" if ssl_verify else "关闭")
        )
        logger.info(
            "APIG模式 管理面地址=%s 模板ID=%s 实例超时秒=%s "
            "续期秒数=%s 保活扫描间隔秒=%s 续期阈值秒=%s "
            "就绪等待秒=%s 运行面地址=%s HW_ID=%s HW_APPKEY=%s API_KEY=%s "
            "X-mounts=%s SSL校验=%s TLS版本=%s 连接超时秒=%s 请求超时秒=%s 代理=%s",
            Config.SANDBOX_APIG_ENDPOINT or "(未配置)",
            Config.SANDBOX_TEMPLATE_ID or "(未配置)",
            Config.SANDBOX_INSTANCE_TIMEOUT,
            Config.SANDBOX_REFRESH_DURATION,
            Config.SANDBOX_KEEPALIVE_INTERVAL_SECONDS,
            Config.SANDBOX_KEEPALIVE_MARGIN_SECONDS,
            Config.SANDBOX_READY_TIMEOUT_SECONDS,
            Config.SANDBOX_BASE_URL,
            mask_secret(Config.SANDBOX_APIG_HW_ID),
            mask_secret(Config.SANDBOX_APIG_HW_APPKEY),
            mask_secret(Config.SANDBOX_API_KEY),
            x_mounts if x_mounts is not None else "(未配置)",
            ssl_label,
            Config.SANDBOX_APIG_TLS_VERSION or "系统默认",
            Config.SANDBOX_APIG_CONNECT_TIMEOUT_SECONDS,
            Config.SANDBOX_APIG_TIMEOUT_SECONDS,
            Config.SANDBOX_APIG_PROXY or "(无)",
        )


def create_app() -> Flask:
    setup_logging()
    _log_startup_summary()

    app = Flask(__name__)
    app.config.from_object(Config)

    store = TaskStore()
    worker = TaskWorker(store)

    app.extensions["task_store"] = store
    app.extensions["task_worker"] = worker
    app.register_blueprint(api_bp)
    session_sandbox_manager.start_keepalive()
    logger.info(
        "服务就绪 路由蓝图=api 保活线程已启用=%s",
        not bool(Config.SANDBOX_DOCKER_CONTAINER),
    )

    return app
