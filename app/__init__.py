from __future__ import annotations

import logging

from flask import Flask

from app.config import Config
from app.models import TaskStore
from app.routes import api_bp
from app.session_sandbox import session_sandbox_manager
from app.worker import TaskWorker


def create_app() -> Flask:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    app = Flask(__name__)
    app.config.from_object(Config)

    store = TaskStore()
    worker = TaskWorker(store)

    app.extensions["task_store"] = store
    app.extensions["task_worker"] = worker
    app.register_blueprint(api_bp)
    session_sandbox_manager.start_keepalive()

    return app
