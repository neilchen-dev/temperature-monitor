from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
from services import db, devices
from services.modbus_client import (
    TRANSPORT_TCP,
    ModbusConfigError,
    ModbusEndpoint,
    ModbusPoller,
    _contiguous_runs,
    build_modbus_client,
    decode_register,
    parse_modbus_endpoint,
    parse_register_map,
)
from tests.helpers import ModbusTestServer


def tcp_endpoint(port: int, timeout: float = 2.0) -> ModbusEndpoint:
    return parse_modbus_endpoint(
        transport="tcp", host="127.0.0.1", port=port, serial_port="",
        baudrate=9600, parity="N", stopbits=1, bytesize=8, timeout=timeout,
    )


def rtu_endpoint(serial_port: str, timeout: float = 1.0) -> ModbusEndpoint:
    return parse_modbus_endpoint(
        transport="rtu", host="127.0.0.1", port=5020, serial_port=serial_port,
        baudrate=9600, parity="N", stopbits=1, bytesize=8, timeout=timeout,
    )


class EndpointTests(unittest.TestCase):
    def test_tcp_defaults(self) -> None:
        endpoint = tcp_endpoint(5020)
        self.assertEqual(endpoint.transport, TRANSPORT_TCP)
        self.assertEqual(endpoint.describe(), "127.0.0.1:5020")

    def test_invalid_transport_rejected(self) -> None:
        with self.assertRaises(ModbusConfigError):
            parse_modbus_endpoint(
                transport="udp", host="127.0.0.1", port=5020, serial_port="",
                baudrate=9600, parity="N", stopbits=1, bytesize=8, timeout=5.0,
            )

    def test_rtu_requires_serial_port(self) -> None:
        with self.assertRaises(ModbusConfigError) as ctx:
            rtu_endpoint("")
        self.assertIn("MODBUS_SERIAL_PORT", str(ctx.exception))

    def test_rtu_serial_parameter_validation(self) -> None:
        base = dict(
            transport="rtu", host="", port=5020, serial_port="COM3",
            baudrate=9600, parity="N", stopbits=1, bytesize=8, timeout=5.0,
        )
        for field, bad in [
            ("baudrate", 100), ("parity", "X"), ("stopbits", 3),
            ("bytesize", 9), ("timeout", 0.5),
        ]:
            with self.assertRaises(ModbusConfigError, msg=field):
                parse_modbus_endpoint(**{**base, field: bad})

    def test_rtu_endpoint_describe(self) -> None:
        endpoint = parse_modbus_endpoint(
            transport="rtu", host="", port=0, serial_port="/dev/ttyUSB0",
            baudrate=9600, parity="E", stopbits=1, bytesize=8, timeout=5.0,
        )
        self.assertEqual(endpoint.describe(), "/dev/ttyUSB0@9600 8E1")

    def test_tcp_port_validation(self) -> None:
        with self.assertRaises(ModbusConfigError):
            tcp_endpoint(70000)


class BuildClientTests(unittest.TestCase):
    def test_tcp_factory(self) -> None:
        from pymodbus.client import ModbusTcpClient

        client = build_modbus_client(tcp_endpoint(5020, timeout=3.0))
        self.assertIsInstance(client, ModbusTcpClient)

    def test_rtu_factory_kwargs(self) -> None:
        from pymodbus.client import ModbusSerialClient

        with patch("pymodbus.client.ModbusSerialClient") as serial_client:
            build_modbus_client(parse_modbus_endpoint(
                transport="rtu", host="", port=0, serial_port="COM3",
                baudrate=4800, parity="E", stopbits=2, bytesize=8, timeout=2.0,
            ))
        serial_client.assert_called_once_with(
            "COM3", baudrate=4800, parity="E", stopbits=2,
            bytesize=8, timeout=2.0, retries=1,
        )
        # 直接构造真实串口客户端（不打开串口，无需硬件）
        client = build_modbus_client(rtu_endpoint("COM_PROBE_99", timeout=1.0))
        self.assertIsInstance(client, ModbusSerialClient)
        self.assertFalse(client.connected)


class RegisterMapTests(unittest.TestCase):
    def test_default_map_when_empty(self) -> None:
        parsed = parse_register_map("")
        self.assertEqual(parsed["temperature"]["address"], 0)
        self.assertEqual(parsed["temperature"]["encoding"], "int16")
        self.assertEqual(parsed["temperature"]["type"], "holding")
        self.assertEqual(parsed["humidity"]["address"], 1)
        self.assertEqual(parsed["device_status"]["online_value"], 1)

    def test_invalid_json_rejected(self) -> None:
        with self.assertRaises(ModbusConfigError):
            parse_register_map("{not json")

    def test_measurement_fields_required(self) -> None:
        with self.assertRaises(ModbusConfigError):
            parse_register_map('{"temperature": {"address": 0}}')

    def test_unknown_field_rejected(self) -> None:
        with self.assertRaises(ModbusConfigError):
            parse_register_map(
                '{"temperature": {"address": 0},'
                ' "humidity": {"address": 1},'
                ' "device_status": {"address": 2},'
                ' "extra": {"address": 3}}'
            )

    def test_device_status_optional(self) -> None:
        parsed = parse_register_map(
            '{"temperature": {"address": 0, "scale": 0.1, "encoding": "int16"},'
            ' "humidity": {"address": 1, "scale": 0.1}}'
        )
        self.assertNotIn("device_status", parsed)

    def test_register_type_validation(self) -> None:
        with self.assertRaises(ModbusConfigError):
            parse_register_map(
                '{"temperature": {"address": 0, "type": "coil"},'
                ' "humidity": {"address": 1}}'
            )
        parsed = parse_register_map(
            '{"temperature": {"address": 0, "type": "input"},'
            ' "humidity": {"address": 1, "type": "input"}}'
        )
        self.assertEqual(parsed["temperature"]["type"], "input")

    def test_sparse_map_is_valid_and_segmented(self) -> None:
        # 稀疏地址合法：0 与 200 会分成两次独立读取，不再展开成 0..200
        parsed = parse_register_map(
            '{"temperature": {"address": 0, "scale": 0.1},'
            ' "humidity": {"address": 200, "scale": 0.1}}'
        )
        self.assertEqual(parsed["humidity"]["address"], 200)

    def test_contiguous_runs_grouping(self) -> None:
        self.assertEqual(
            _contiguous_runs([0, 1, 2, 5, 6, 10]),
            [(0, 2), (5, 6), (10, 10)],
        )
        self.assertEqual(_contiguous_runs([7]), [(7, 7)])

    def test_unit_id_range_enforced(self) -> None:
        with self.assertRaises(ModbusConfigError):
            ModbusPoller(
                device_id="X", endpoint=tcp_endpoint(5020), unit_id=0,
                poll_interval=1.0, register_map=parse_register_map(None),
                record_sample=lambda **kwargs: [],
            )
        with self.assertRaises(ModbusConfigError):
            ModbusPoller(
                device_id="X", endpoint=tcp_endpoint(5020), unit_id=248,
                poll_interval=1.0, register_map=parse_register_map(None),
                record_sample=lambda **kwargs: [],
            )


class DecodeRegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.register_map = parse_register_map("")

    def test_int16_positive_with_scale(self) -> None:
        self.assertAlmostEqual(
            decode_register(self.register_map["temperature"], 252), 25.2, places=6
        )

    def test_int16_negative_wraps(self) -> None:
        # 0xFFEC = 65516 -> int16 解释为 -20 -> x0.1 = -2.0C
        self.assertAlmostEqual(
            decode_register(self.register_map["temperature"], 0xFFEC), -2.0, places=6
        )

    def test_uint16_humidity(self) -> None:
        self.assertAlmostEqual(
            decode_register(self.register_map["humidity"], 481), 48.1, places=6
        )


class ModbusPollerIntegrationTests(unittest.TestCase):
    """对真实 pymodbus TCP server 的集成测试（TCP 回归保护）。"""

    def setUp(self) -> None:
        self._tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp_dir.cleanup)
        self._original = {
            "SQLITE_ENABLED": config.SQLITE_ENABLED,
            "SQLITE_DB_PATH": config.SQLITE_DB_PATH,
            "EVENT_TEMPERATURE_HIGH_C": config.EVENT_TEMPERATURE_HIGH_C,
        }
        db.close()
        db._init_failed = False
        config.SQLITE_ENABLED = True
        config.SQLITE_DB_PATH = Path(self._tmp_dir.name) / "modbus.db"
        config.EVENT_TEMPERATURE_HIGH_C = None
        self.addCleanup(self._restore)

        self.server = ModbusTestServer([252, 481, 1], unit_id=1)
        self.server.start()
        self.addCleanup(self.server.stop)

    def _restore(self) -> None:
        for name, value in self._original.items():
            setattr(config, name, value)
        db._init_failed = False
        db.close()

    def _poller(self, register_map: str = "", unit_id: int = 1) -> ModbusPoller:
        poller = ModbusPoller(
            device_id="PLC-01",
            endpoint=tcp_endpoint(self.server.port),
            unit_id=unit_id,
            poll_interval=1.0,
            register_map=parse_register_map(register_map or None),
            record_sample=devices.record_sample,
        )
        self.addCleanup(poller.close)
        return poller

    def test_successful_poll_reads_simulated_plc(self) -> None:
        poller = self._poller()
        sample = poller.poll_once()
        self.assertIsNotNone(sample)
        self.assertEqual(sample["temperature"], 25.2)
        self.assertEqual(sample["humidity"], 48.1)
        self.assertEqual(sample["status"], "online")
        status = poller.status()
        self.assertEqual(status["transport"], "tcp")
        self.assertIsNone(status["last_error_summary"])

    def test_status_word_zero_means_offline(self) -> None:
        self.server.set_registers([252, 481, 0])
        sample = self._poller().poll_once()
        self.assertEqual(sample["status"], "offline")

    def test_without_device_status_read_success_means_online(self) -> None:
        register_map = (
            '{"temperature": {"address": 0, "scale": 0.1, "encoding": "int16"},'
            ' "humidity": {"address": 1, "scale": 0.1}}'
        )
        sample = self._poller(register_map).poll_once()
        self.assertEqual(sample["temperature"], 25.2)
        self.assertEqual(sample["humidity"], 48.1)
        self.assertEqual(sample["status"], "online")

    def test_input_register_type_reads_fc04(self) -> None:
        # 单列表模拟器同时应答 FC03/FC04 且值相同；
        # type=input 的字段应走 read_input_registers 且解码一致。
        register_map = (
            '{"temperature": {"address": 0, "scale": 0.1, "encoding": "int16",'
            ' "type": "input"},'
            ' "humidity": {"address": 1, "scale": 0.1, "type": "input"},'
            ' "device_status": {"address": 2, "online_value": 1, "type": "input"}}'
        )
        poller = self._poller(register_map)
        with patch.object(
            poller._client, "read_input_registers",
            wraps=poller._client.read_input_registers,
        ) as input_read, patch.object(
            poller._client, "read_holding_registers",
        ) as holding_read:
            sample = poller.poll_once()
        self.assertIsNotNone(sample)
        self.assertEqual(sample["temperature"], 25.2)
        self.assertEqual(sample["humidity"], 48.1)
        input_read.assert_called_once()
        holding_read.assert_not_called()

    def test_mixed_register_types_issue_segmented_reads(self) -> None:
        # temperature@0(holding) + humidity@1(input) + device_status@2(holding)：
        # input 一次读；holding 因地址不连续被拆成 (0,1) 和 (2,1) 两段
        register_map = (
            '{"temperature": {"address": 0, "scale": 0.1, "encoding": "int16"},'
            ' "humidity": {"address": 1, "scale": 0.1, "type": "input"},'
            ' "device_status": {"address": 2, "online_value": 1}}'
        )
        poller = self._poller(register_map)
        with patch.object(
            poller._client, "read_input_registers",
            wraps=poller._client.read_input_registers,
        ) as input_read, patch.object(
            poller._client, "read_holding_registers",
            wraps=poller._client.read_holding_registers,
        ) as holding_read:
            sample = poller.poll_once()
        self.assertIsNotNone(sample)
        self.assertEqual(sample["temperature"], 25.2)
        self.assertEqual(sample["humidity"], 48.1)
        self.assertEqual(sample["status"], "online")
        input_read.assert_called_once()
        self.assertEqual(holding_read.call_count, 2)

    def test_out_of_range_humidity_stored_as_none(self) -> None:
        # 4810 x0.1 = 481%RH，超出 0~100 合理范围 -> None，不崩溃
        self.server.set_registers([252, 4810, 1])
        sample = self._poller().poll_once()
        self.assertEqual(sample["temperature"], 25.2)
        self.assertIsNone(sample["humidity"])

    def test_server_down_returns_none_without_crash(self) -> None:
        poller = self._poller()
        self.server.stop()
        self.assertIsNone(poller.poll_once())
        self.assertIsNotNone(poller.status()["last_error_summary"])
        self.assertGreaterEqual(poller.status()["consecutive_failures"], 1)

    def test_same_address_in_holding_and_input_never_collides(self) -> None:
        # holding 0 = 100 (10.0C)，input 0 = 200：若两空间按键合并，
        # 温度会被 input 的 200 覆盖解码成 20.0C。独立地址空间必须各读各的。
        self.server2 = ModbusTestServer(
            [100, 481, 1], unit_id=1, input_registers=[200, 300]
        )
        self.server2.start()
        self.addCleanup(self.server2.stop)
        poller = ModbusPoller(
            device_id="PLC-01",
            endpoint=tcp_endpoint(self.server2.port),
            unit_id=1,
            poll_interval=1.0,
            register_map=parse_register_map(
                '{"temperature": {"address": 0, "scale": 0.1, "encoding": "int16"},'
                ' "humidity": {"address": 0, "scale": 0.1, "type": "input"}}'
            ),
            record_sample=devices.record_sample,
        )
        self.addCleanup(poller.close)
        sample = poller.poll_once()
        self.assertIsNotNone(sample)
        self.assertEqual(sample["temperature"], 10.0)
        self.assertEqual(sample["humidity"], 20.0)

    def test_sparse_registers_read_as_segments_not_one_block(self) -> None:
        # holding 地址 0 与 3：必须拆成 (0,1) 和 (3,1) 两次读，
        # 不得展开成 0..3 的连续区间（中间 1、2 可能是保留寄存器）
        self.server_sparse = ModbusTestServer([252, 0, 0, 481])
        self.server_sparse.start()
        self.addCleanup(self.server_sparse.stop)
        poller = ModbusPoller(
            device_id="PLC-01",
            endpoint=tcp_endpoint(self.server_sparse.port),
            unit_id=1,
            poll_interval=1.0,
            register_map=parse_register_map(
                '{"temperature": {"address": 0, "scale": 0.1},'
                ' "humidity": {"address": 3, "scale": 0.1}}'
            ),
            record_sample=devices.record_sample,
        )
        self.addCleanup(poller.close)
        requested: list[tuple[int, int]] = []
        original = poller._client.read_holding_registers

        def recording_read(start, *, count=1, device_id=1):
            requested.append((start, count))
            return original(start, count=count, device_id=device_id)

        with patch.object(
            poller._client, "read_holding_registers", side_effect=recording_read
        ):
            sample = poller.poll_once()
        self.assertIsNotNone(sample)
        self.assertEqual(requested, [(0, 1), (3, 1)])
        self.assertEqual(sample["temperature"], 25.2)
        self.assertEqual(sample["humidity"], 48.1)

    def test_short_response_does_not_kill_poller(self) -> None:
        # 设备返回数量不足的寄存器：必须转为失败而不是 IndexError 杀线程
        poller = self._poller()
        short_response = type("R", (), {
            "isError": staticmethod(lambda: False),
            "registers": [252],
        })()
        with patch.object(
            poller._client, "read_holding_registers", return_value=short_response
        ):
            self.assertIsNone(poller.poll_once())
        self.assertEqual(poller.status()["last_error_summary"],
                         "ModbusShortResponseError")
        # poller 仍可用：恢复正常客户端后下一轮成功
        self.server.set_registers([252, 481, 1])
        sample = poller.poll_once()
        self.assertIsNotNone(sample)
        self.assertEqual(sample["temperature"], 25.2)

    def test_communication_failure_closes_client_for_reconnect(self) -> None:
        # 模拟 USB 拔出：connected 仍为 True 但读取抛 OSError；
        # 必须主动 close，下一轮才会重新 connect（reviewer 复现的回归）
        poller = self._poller()
        calls = {"close": 0, "connect": 0}
        original_close = poller._client.close
        original_connect = poller._client.connect
        original_read = poller._client.read_holding_registers
        state = {"failed_once": False}

        def flaky_read(start, *, count=1, device_id=1):
            if not state["failed_once"]:
                state["failed_once"] = True
                raise OSError("simulated unplug")
            return original_read(start, count=count, device_id=device_id)

        def counting_close():
            calls["close"] += 1
            original_close()

        def counting_connect():
            calls["connect"] += 1
            return original_connect()

        from unittest.mock import PropertyMock

        with patch.object(
            type(poller._client), "connected", new_callable=PropertyMock
        ) as connected_prop, patch.object(
            poller._client, "read_holding_registers", side_effect=flaky_read
        ), patch.object(
            poller._client, "close", side_effect=counting_close
        ), patch.object(
            poller._client, "connect", side_effect=counting_connect
        ):
            connected_prop.side_effect = [True, False]
            self.assertIsNone(poller.poll_once())
            self.assertEqual(calls["close"], 1)
            sample = poller.poll_once()  # 重插后：串口重新可打开
        # 不主动 close 就永远不会有后续 connect；pymodbus 内部重连可能
        # 追加调用，因此只要求至少发生一次重新连接
        self.assertGreaterEqual(calls["connect"], 1)
        self.assertIsNotNone(sample)
        self.assertEqual(sample["temperature"], 25.2)

    def test_wrong_unit_id_is_failure_not_crash(self) -> None:
        poller = self._poller(unit_id=9)
        self.assertIsNone(poller.poll_once())
        self.assertEqual(poller.status()["last_error_summary"], "ModbusReadError")

    def test_full_pipeline_poll_to_unified_store(self) -> None:
        config.EVENT_TEMPERATURE_HIGH_C = 30.0
        # 第一轮：基线
        poller = self._poller()
        sample = poller.poll_once()
        self.assertEqual(devices.record_sample(
            sample["device"], sample["source"], sample["temperature"],
            sample["humidity"], sample["status"],
        ), [])
        # 模拟温度越限（315 x0.1 = 31.5C）
        self.server.set_registers([315, 481, 1])
        sample = poller.poll_once()
        transitions = devices.record_sample(
            sample["device"], sample["source"], sample["temperature"],
            sample["humidity"], sample["status"],
        )
        self.assertEqual(
            [(t["event_type"], t["new_state"]) for t in transitions],
            [("temperature_alert", "TEMPERATURE_HIGH")],
        )
        # 停机 -> 离线事件
        self.server.set_registers([315, 481, 0])
        sample = poller.poll_once()
        transitions = devices.record_sample(
            sample["device"], sample["source"], sample["temperature"],
            sample["humidity"], sample["status"],
        )
        self.assertEqual(
            [(t["event_type"], t["new_state"]) for t in transitions],
            [("status_change", "OFFLINE")],
        )
        events = db.fetch_device_events(device_id="PLC-01")
        self.assertEqual(len(events), 2)
        states = db.fetch_latest_device_states()
        self.assertEqual(states[0]["status"], "offline")


class RtuPollerTests(unittest.TestCase):
    """RTU 失败路径：真实 pyserial，不需要任何硬件。"""

    def test_missing_serial_port_poll_fails_gracefully(self) -> None:
        poller = ModbusPoller(
            device_id="PLC-RTU",
            endpoint=rtu_endpoint("COM_DEFINITELY_MISSING_99", timeout=1.0),
            unit_id=1,
            poll_interval=1.0,
            register_map=parse_register_map(None),
            record_sample=lambda **kwargs: [],
        )
        self.addCleanup(poller.close)
        self.assertIsNone(poller.poll_once())
        status = poller.status()
        self.assertEqual(status["transport"], "rtu")
        self.assertIsNotNone(status["last_error_summary"])
        # 串口路径不得出现在对外 status 摘要里
        self.assertNotIn("COM_DEFINITELY_MISSING_99", str(status))


if __name__ == "__main__":
    unittest.main()
