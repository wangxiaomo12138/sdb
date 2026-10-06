from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Generator, Optional


# 排查日志默认截断长度，避免把整段模型输出刷进日志
DEFAULT_PREVIEW_LEN = 200


def preview(value: Any, max_len: int = DEFAULT_PREVIEW_LEN) -> str:
    """把任意值转成适合日志的短文本，过长则截断。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) <= max_len:
        return text
    return f"{text[:max_len]}...(长度={len(text)})"


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """脱敏密钥：只保留前后少量字符。"""
    if not value:
        return "(空)"
    if len(value) <= keep * 2:
        return "***"
    return f"{value[:keep]}***{value[-keep:]}"


def setup_logging() -> None:
    """按环境变量配置根日志；可重复调用，不会叠多层 handler。"""
    level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
    level = getattr(logging, level_name, logging.INFO)
    log_file = os.getenv("LOG_FILE", "").strip()

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] [%(name)s] %(message)s"
    )

    # 清掉 basicConfig / Flask 可能留下的重复 handler
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    stream.setLevel(level)
    root.addHandler(stream)

    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)
        root.addHandler(file_handler)

    # 降低第三方库噪音，业务排查优先看 app.*
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.INFO)


@contextmanager
def log_step(
    logger: logging.Logger,
    step: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> Generator[None, None, None]:
    """记录一步的开始/结束/耗时；异常时带耗时再抛出。"""
    extras = " ".join(f"{k}={preview(v)}" for k, v in fields.items())
    prefix = f"步骤={step}"
    if extras:
        prefix = f"{prefix} {extras}"
    logger.log(level, "%s 开始", prefix)
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.error(
            "%s 失败 耗时毫秒=%.1f 错误=%s",
            prefix,
            elapsed_ms,
            preview(exc, 500),
        )
        raise
    else:
        elapsed_ms = (time.monotonic() - started) * 1000
        logger.log(level, "%s 成功 耗时毫秒=%.1f", prefix, elapsed_ms)
