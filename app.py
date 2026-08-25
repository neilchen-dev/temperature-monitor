from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from flask import Flask
from waitress import serve

import config
from routes.analytics import analytics_bp
from routes.api import api_bp
from routes.dashboard import dashboard_bp, dashboard_session_secret
from routes.history import history_bp
from routes.temperature import temperature_bp
from services import collector, db


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
    db.init_db()
    flask_app = Flask(__name__)
    flask_app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
    # 看板登录会话的签名密钥：从 HISTORY_API_KEY 派生，重启不掉线，换钥匙全员下线。
    flask_app.secret_key = dashboard_session_secret()
    flask_app.config["SESSION_COOKIE_HTTPONLY"] = True
    flask_app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    flask_app.register_blueprint(temperature_bp)
    flask_app.register_blueprint(history_bp)
    flask_app.register_blueprint(analytics_bp)
    flask_app.register_blueprint(dashboard_bp)
    flask_app.register_blueprint(api_bp)
    return flask_app


app = create_app()


def run_server() -> None:
    logger = logging.getLogger("temperature_monitor")
    # 后台采集线程只在生产入口启动一次；create_app 保持无副作用，
    # 测试与多次调用不会重复启动。多 worker 部署见 .env.example 注释。
    collector.start_collectors()
    logger.info(
        "温湿度监控服务启动 | 设备数量=%s | HA温度源单位=%s | "
        "历史表数量=%s | 历史清理启用=%s | Modbus启用=%s | 离线状态由HA明确提供",
        len(config.DEVICES),
        config.SOURCE_TEMPERATURE_UNIT,
        len(config.HISTORY_TABLE_MAP),
        config.HISTORY_CLEANUP_ENABLED,
        config.MODBUS_ENABLED,
    )
    try:
        serve(
            app,
            host=config.HOST,
            port=config.PORT,
            threads=config.WAITRESS_THREADS,
        )
    finally:
        # Waitress 正常退出/被中断时停掉采集线程，保证应用级优雅关闭。
        collector.stop_collectors()


if __name__ == "__main__":
    run_server()
