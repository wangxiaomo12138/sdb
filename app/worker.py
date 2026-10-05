from __future__ import annotations

import atexit
import logging
from concurrent.futures import ThreadPoolExecutor

from app.config import Config
from app.models import TaskStore
from app.sandbox_client import OpenCodeError, run_opencode

logger = logging.getLogger(__name__)


class TaskWorker:
    def __init__(self, store: TaskStore, max_workers: int | None = None) -> None:
        self.store = store
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers or Config.WORKER_MAX_WORKERS,
            thread_name_prefix="opencode-worker",
        )
        atexit.register(self.shutdown)

    def submit(self, task_id: str) -> None:
        self._executor.submit(self._run_task, task_id)

    def _run_task(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is None:
            return

        self.store.mark_running(task_id)
        try:
            answer = run_opencode(task.query, session_id=task.session_id)
            self.store.mark_succeeded(task_id, answer)
        except OpenCodeError as exc:
            logger.warning("task %s failed: %s", task_id, exc)
            self.store.mark_failed(task_id, str(exc))
        except Exception as exc:  # 未预期异常
            logger.exception("task %s unexpected error", task_id)
            self.store.mark_failed(task_id, f"unexpected error: {exc}")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
