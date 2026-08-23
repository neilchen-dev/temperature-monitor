"""Modbus 探针工具（开发/部署辅助，不进入生产主进程）。

新设备到手时先用它确认 unit id 与寄存器布局，再写 .env：

单次读取（默认行为）：
    python tools/modbus_probe.py --tcp 127.0.0.1:5020 --unit 1
    python tools/modbus_probe.py --rtu COM3 --unit 1 --baudrate 9600
    python tools/modbus_probe.py --rtu /dev/ttyUSB0 --map '{"temperature": ...}'

显式启用 unit-id 扫描（不会默认扫描；对真实 RS485 总线逐一试探每个地址）：
    python tools/modbus_probe.py --rtu COM3 --scan
    python tools/modbus_probe.py --tcp 127.0.0.1:5020 --scan --scan-end 16

读取复用 services.modbus_client 的完整解码与状态推断逻辑，
输出即统一设备模型样本（与正式采集链路一致）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.modbus_client import (  # noqa: E402
    ModbusConfigError,
    ModbusPoller,
    parse_modbus_endpoint,
    parse_register_map,
)


def _noop_record_sample(**_kwargs) -> list:
    return []


def _build_endpoint(args: argparse.Namespace):
    if bool(args.tcp) == bool(args.rtu):
        raise SystemExit("必须且只能指定 --tcp HOST:PORT 或 --rtu SERIAL_PORT 其中之一")

    if args.tcp:
        host, _, port_text = args.tcp.partition(":")
        try:
            port = int(port_text or "502")
        except ValueError:
            raise SystemExit(f"--tcp 端口必须是整数: {port_text!r}") from None
        return parse_modbus_endpoint(
            transport="tcp", host=host, port=port,
            serial_port="", baudrate=9600, parity="N",
            stopbits=1, bytesize=8, timeout=args.timeout,
        )

    return parse_modbus_endpoint(
        transport="rtu", host="127.0.0.1", port=5020,
        serial_port=args.rtu,
        baudrate=args.baudrate, parity=args.parity,
        stopbits=args.stopbits, bytesize=args.bytesize,
        timeout=args.timeout,
    )


def _build_poller(args: argparse.Namespace, unit_id: int) -> ModbusPoller:
    return ModbusPoller(
        device_id="PROBE",
        endpoint=_build_endpoint(args),
        unit_id=unit_id,
        poll_interval=1.0,
        register_map=parse_register_map(args.map or None),
        record_sample=_noop_record_sample,
    )


def _probe_unit(args: argparse.Namespace, unit_id: int) -> bool:
    poller = _build_poller(args, unit_id)
    try:
        sample = poller.poll_once()
        if sample is None:
            status = poller.status()
            print(f"unit {unit_id:>3} | 失败 | {status['last_error_summary']}")
            return False
        print(f"unit {unit_id:>3} | OK | {json.dumps(sample, ensure_ascii=False)}")
        return True
    finally:
        poller.close()


def main() -> int:
    # pyserial/pymodbus 对连接失败的 logging 告警与探针自己的失败行重复，
    # 统一压到 ERROR 以下不出。
    logging.getLogger().setLevel(logging.ERROR)
    parser = argparse.ArgumentParser(
        description="Modbus 单次读取探针（--scan 显式启用 unit-id 扫描）"
    )
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--tcp", metavar="HOST:PORT", help="Modbus TCP 目标")
    transport.add_argument("--rtu", metavar="SERIAL_PORT",
                           help="RTU 串口（Windows: COM3；Linux: /dev/ttyUSB0）")
    parser.add_argument("--unit", type=int, default=1,
                        help="单次读取使用的 unit id（默认 1）")
    parser.add_argument("--map", default=None,
                        help="可选 JSON 寄存器映射（默认与采集服务一致）")
    parser.add_argument("--baudrate", type=int, default=9600)
    parser.add_argument("--parity", default="N", choices=["N", "E", "O"])
    parser.add_argument("--stopbits", type=int, default=1, choices=[1, 2])
    parser.add_argument("--bytesize", type=int, default=8, choices=[7, 8])
    parser.add_argument("--timeout", type=float, default=2.0,
                        help="单次请求超时秒数（默认 2）")
    parser.add_argument("--scan", action="store_true",
                        help="显式启用 unit-id 扫描（默认不扫描）")
    parser.add_argument("--scan-start", type=int, default=1)
    parser.add_argument("--scan-end", type=int, default=16,
                        help="扫描上限（Modbus 地址最大 247）")
    args = parser.parse_args()

    if args.scan:
        if not 1 <= args.scan_start <= args.scan_end <= 247:
            raise SystemExit("--scan-start/--scan-end 必须满足 1 <= start <= end <= 247")
        print(f"扫描 unit {args.scan_start}..{args.scan_end}（逐个试探，请耐心等待）")
        found = []
        for unit_id in range(args.scan_start, args.scan_end + 1):
            if _probe_unit(args, unit_id):
                found.append(unit_id)
        print(f"扫描完成 | 响应的 unit id: {found if found else '无'}")
        return 0 if found else 1

    return 0 if _probe_unit(args, args.unit) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ModbusConfigError, KeyboardInterrupt) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        sys.exit(2)
