import logging

from app import create_app
from app.config import Config

app = create_app()
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info(
        "启动Flask服务 主机=%s 端口=%s 调试=%s",
        Config.FLASK_HOST,
        Config.FLASK_PORT,
        Config.FLASK_DEBUG,
    )
    app.run(host=Config.FLASK_HOST, port=Config.FLASK_PORT, debug=Config.FLASK_DEBUG)
