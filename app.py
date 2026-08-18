from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from flask import Flask
from waitress import serve

import config
from routes.history import history_bp
from routes.temperature import temperature_bp


def configure_logging() -> logging.Logger:
    """配置控制台与文件日志；重复导入时不重复添加 handler。"""
    config.ensure_runtime_directories()
    logger = logging.getLogger("temperature_monitor")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if not logger.handlers:
        formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

        file_handler = RotatingFileHandler(
            config.LOG_DIR / "app.log",
            maxBytes=config.LOG_MAX_BYTES,
            backupCount=config.LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)

    return logger


def create_app() -> Flask:
    configure_logging()
    flask_app = Flask(__name__)
    flask_app.register_blueprint(temperature_bp)
    flask_app.register_blueprint(history_bp)
    return flask_app


app = create_app()


def run_server() -> None:
    logger = logging.getLogger("temperature_monitor")
    logger.info(
        "温湿度监控服务启动 | 设备数量=%s | HA温度源单位=%s | "
        "历史表数量=%s | 历史清理启用=%s | 离线状态由HA明确提供",
        len(config.DEVICES),
        config.SOURCE_TEMPERATURE_UNIT,
        len(config.HISTORY_TABLE_MAP),
        config.HISTORY_CLEANUP_ENABLED,
    )
    serve(
        app,
        host=config.HOST,
        port=config.PORT,
        threads=config.WAITRESS_THREADS,
    )


if __name__ == "__main__":
    run_server()
