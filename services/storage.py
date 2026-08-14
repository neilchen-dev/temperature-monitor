from __future__ import annotations

import csv
import threading
import time
from typing import Any

import config


_history_lock = threading.Lock()


def save_history(
    device: str,
    temperature_c: Any,
    humidity: Any,
    status: str,
    feishu_code: int,
    feishu_message: str,
) -> None:
    config.ensure_runtime_directories()
    history_file = config.DATA_DIR / time.strftime("history_%Y-%m.csv")

    with _history_lock:
        file_exists = history_file.exists()
        with history_file.open("a", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            if not file_exists:
                writer.writerow([
                    "记录时间",
                    "设备",
                    "温度_摄氏度",
                    "湿度_%RH",
                    "在线状态",
                    "飞书返回码",
                    "飞书消息",
                ])
            writer.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"),
                device,
                temperature_c,
                humidity,
                status,
                feishu_code,
                feishu_message,
            ])
