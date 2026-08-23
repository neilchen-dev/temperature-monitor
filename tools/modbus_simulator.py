"""本地 Modbus TCP 模拟 PLC（开发/演示/测试用，不进入生产主进程）。

寄存器布局（与 services.modbus_client.DEFAULT_REGISTER_MAP 一致）：

    保持寄存器 0: 温度，int16，实际值 = 寄存器值 x 0.1（摄氏度）
    保持寄存器 1: 湿度，uint16，实际值 = 寄存器值 x 0.1（%RH）
    保持寄存器 2: 设备状态字，1 = 运行/在线，0 = 停机/离线

数值按正弦缓慢漂移（温度 18~33C，湿度 40~70%），温度周期性越过 30C
阈值，便于演示 TEMPERATURE_HIGH 事件。

用法：
    python tools/modbus_simulator.py [--host 127.0.0.1] [--port 5020]
        [--unit 1] [--offline-after 秒]   # 演示离线：N 秒后状态字置 0
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import time

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator.simdata import DataType, SimData
from pymodbus.simulator.simdevice import SimDevice

TEMP_MIN_C, TEMP_MAX_C = 18.0, 33.0
HUMIDITY_MIN_PCT, HUMIDITY_MAX_PCT = 40.0, 70.0
STATUS_RUNNING, STATUS_STOPPED = 1, 0


def build_device(unit_id: int) -> SimDevice:
    return SimDevice(unit_id, [
        SimData(0, 1, [250], datatype=DataType.INT16),
        SimData(1, 1, [550], datatype=DataType.UINT16),
        SimData(2, 1, [STATUS_RUNNING], datatype=DataType.INT16),
    ])


async def drift_loop(
    server: ModbusTcpServer,
    unit_id: int,
    offline_at: float | None,
    period_seconds: float,
    started_at: float,
) -> None:
    cycle = 0
    while True:
        # 相位从模拟器启动时刻起算（monotonic 裸值在 Windows 是开机时长，
        # 直接取模会导致每次启动落在漂移周期中的随机位置）。
        phase = ((time.monotonic() - started_at) % period_seconds) / period_seconds
        temperature = TEMP_MIN_C + (TEMP_MAX_C - TEMP_MIN_C) * (
            0.5 - 0.5 * math.cos(2 * math.pi * phase)
        )
        humidity = HUMIDITY_MIN_PCT + (HUMIDITY_MAX_PCT - HUMIDITY_MIN_PCT) * (
            0.5 + 0.5 * math.sin(2 * math.pi * phase * 2)
        )
        status = (
            STATUS_STOPPED
            if offline_at is not None and time.monotonic() > offline_at
            else STATUS_RUNNING
        )
        values = [
            int(round(temperature * 10)),
            int(round(humidity * 10)),
            status,
        ]
        await server.async_setValues(unit_id, 0x03, 0, values)
        if cycle % 60 == 0:
            logging.info(
                "寄存器更新 | temp=%.1fC humidity=%.1f%% status=%s",
                temperature, humidity, status,
            )
        cycle += 1
        await asyncio.sleep(1.0)


async def main(
    host: str,
    port: int,
    unit_id: int,
    offline_after: float | None,
    period_seconds: float,
) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    started_at = time.monotonic()
    offline_at = (
        started_at + offline_after if offline_after is not None else None
    )
    server = ModbusTcpServer(build_device(unit_id), address=(host, port))
    logging.info(
        "Modbus 模拟 PLC 已启动 | %s:%s | unit=%s | 温度漂移 %s~%sC（周期 %s 秒）",
        host, port, unit_id, TEMP_MIN_C, TEMP_MAX_C, int(period_seconds),
    )
    drift = asyncio.create_task(
        drift_loop(server, unit_id, offline_at, period_seconds, started_at)
    )
    try:
        await server.serve_forever()
    finally:
        drift.cancel()
        await server.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本地 Modbus TCP 模拟 PLC")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5020)
    parser.add_argument("--unit", type=int, default=1)
    parser.add_argument(
        "--period",
        type=float,
        default=600.0,
        help="温度漂移周期秒数（演示可调小，例如 60 秒内完成一次越过 30C 阈值）",
    )
    parser.add_argument(
        "--offline-after",
        type=float,
        default=None,
        help="N 秒后把状态字置 0，演示离线事件",
    )
    args = parser.parse_args()
    try:
        asyncio.run(
            main(args.host, args.port, args.unit, args.offline_after, args.period)
        )
    except KeyboardInterrupt:
        print("模拟器已停止")
