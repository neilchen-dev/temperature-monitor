"""Shared test fixtures: a real in-process Modbus TCP server (pymodbus).

The server runs on an ephemeral port in its own thread with a dedicated
asyncio loop, so integration tests exercise the actual wire protocol
instead of mocks.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

from pymodbus.server import ModbusTcpServer
from pymodbus.simulator.simdata import DataType, SimData
from pymodbus.simulator.simdevice import SimDevice

# pymodbus logs connection noise at INFO; keep test output readable.
logging.getLogger("pymodbus").setLevel(logging.WARNING)


def reserve_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class ModbusTestServer:
    """Modbus TCP server for integration tests.

    By default the single-block form is used: holding (FC03) and input
    (FC04) reads both return ``registers``. Pass ``input_registers`` to use
    the four-block tuple form with *independent* holding and input address
    spaces — needed to prove same-numbered addresses never collide.
    """

    def __init__(
        self,
        registers: list[int],
        unit_id: int = 1,
        input_registers: list[int] | None = None,
    ) -> None:
        self.port = reserve_port()
        self.unit_id = unit_id
        self._initial = list(registers)
        self._initial_input = (
            list(input_registers) if input_registers is not None else None
        )
        self._server: ModbusTcpServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self) -> None:
        initial = self._initial
        initial_input = self._initial_input
        loop_holder: dict[str, object] = {}

        def run() -> None:
            try:
                asyncio.run(self._serve(initial, initial_input, loop_holder))
            except Exception as exc:  # pragma: no cover - startup diagnostic
                loop_holder["error"] = exc

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            error = loop_holder.get("error")
            if error is not None:
                raise RuntimeError("Modbus 测试服务器启动失败") from error
            if "server" in loop_holder and "loop" in loop_holder:
                try:
                    with socket.create_connection(
                        ("127.0.0.1", self.port), timeout=0.1
                    ):
                        self._server = loop_holder["server"]  # type: ignore[assignment]
                        self._loop = loop_holder["loop"]  # type: ignore[assignment]
                        return
                except OSError:
                    # The server object exists before serve_forever has bound
                    # and started listening.  Wait for actual TCP readiness.
                    pass
            time.sleep(0.01)
        raise RuntimeError("Modbus 测试服务器未能在 5 秒内监听")

    async def _serve(self, initial: list[int], initial_input, holder: dict) -> None:
        def block(values: list[int]) -> list[SimData]:
            return [
                SimData(address, 1, [value], datatype=DataType.INT16)
                for address, value in enumerate(values)
            ]

        if initial_input is None:
            device: SimDevice = SimDevice(self.unit_id, block(initial))
        else:
            # 四元组顺序: (coils, discrete inputs, holding registers, input
            # registers)；coils/discrete 必须非空且使用 BITS 类型。
            pad_bits = [SimData(0, 1, [0], datatype=DataType.BITS)]
            device = SimDevice(self.unit_id, (
                pad_bits,
                pad_bits,
                block(initial),
                block(initial_input),
            ))
        server = ModbusTcpServer(device, address=("127.0.0.1", self.port))
        holder["server"] = server
        holder["loop"] = asyncio.get_running_loop()
        await server.serve_forever()

    def set_registers(self, values: list[int], start: int = 0) -> None:
        assert self._server is not None and self._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self._server.async_setValues(self.unit_id, 0x03, start, values),
            self._loop,
        )
        future.result(timeout=3.0)

    def stop(self) -> None:
        if self._server is not None and self._loop is not None:
            future = asyncio.run_coroutine_threadsafe(
                self._server.shutdown(), self._loop
            )
            future.result(timeout=3.0)
        self._thread.join(timeout=3.0)
        self._server = None
        self._loop = None
