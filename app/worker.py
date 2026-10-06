from __future__ import annotations

import atexit
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.config import Config
from app.logging_utils import preview
from app.models import TaskStore
from app.sandbox_client import OpenCodeError, run_opencode

logger = logging.getLogger(__name__)


class TaskWorker:
    def __init__(self, store: TaskStore, max_workers: int | None = None) -> None:
        self.store = store
        workers = max_workers or Config.WORKER_MAX_WORKERS
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="opencode-worker",
        )
        atexit.register(self.shutdown)
        logger.info("任务线程池已启动 最大线程数=%s", workers)

    def submit(self, task_id: str) -> None:
        logger.info("任务已提交到线程池 任务ID=%s", task_id)
        self._executor.submit(self._run_task, task_id)

    def _run_task(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is None:
            logger.warning("执行前任务已不存在 任务ID=%s", task_id)
            return

        logger.info(
            "开始执行任务 任务ID=%s session_id=%s 问题=%s",
            task_id,
            task.session_id,
            preview(task.query),
        )
        self.store.mark_running(task_id)
        started = time.monotonic()
        try:
            answer = run_opencode(task.query, session_id=task.session_id)
            elapsed_ms = (time.monotonic() - started) * 1000
            self.store.mark_succeeded(task_id, answer)
            logger.info(
                "任务执行成功 任务ID=%s session_id=%s 耗时毫秒=%.1f 回复=%s",
                task_id,
                task.session_id,
                elapsed_ms,
                preview(answer),
            )
        except OpenCodeError as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.warning(
                "任务执行失败 任务ID=%s session_id=%s 耗时毫秒=%.1f 错误=%s",
                task_id,
                task.session_id,
                elapsed_ms,
                preview(exc, 500),
            )
            self.store.mark_failed(task_id, str(exc))
        except Exception as exc:  # 未预期异常
            elapsed_ms = (time.monotonic() - started) * 1000
            logger.exception(
                "任务发生未预期异常 任务ID=%s session_id=%s 耗时毫秒=%.1f",
                task_id,
                task.session_id,
                elapsed_ms,
            )
            self.store.mark_failed(task_id, f"unexpected error: {exc}")

    def shutdown(self) -> None:
        logger.info("任务线程池正在关闭")
        self._executor.shutdown(wait=False, cancel_futures=True)
