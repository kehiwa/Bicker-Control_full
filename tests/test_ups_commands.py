import asyncio
import queue
import threading
import unittest

from bicker_control.ups.commands import UpsCommandService
from bicker_control.ups.protocol import (
    CMD_STATUS_FLAGS,
    CMD_UPS_OUTPUT,
    INDEX_GENERIC,
    INDEX_PARAMETER,
    PARAM_MAX_BACKUP_TIME,
    Frame,
    encode_frame,
)
from bicker_control.ups.transport import UpsPreconditionFailed, UpsSerialService


def parameter_data(parameter_id: int, enabled: bool, value: int) -> bytes:
    return bytes((parameter_id, 1, 0, 120, 0, 60, 0, int(enabled))) + value.to_bytes(2, "little")


class FakePort:
    def __init__(self, power_present: bool = True) -> None:
        self.power_present = power_present
        self.backup_time_enabled = False
        self.backup_time_value = 60
        self.writes: list[bytes] = []
        self._incoming: queue.Queue[bytes] = queue.Queue()
        self._pending = bytearray()
        self._lock = threading.Lock()

    def read(self, size: int = 1) -> bytes:
        if not self._pending:
            try:
                self._pending.extend(self._incoming.get(timeout=0.005))
            except queue.Empty:
                return b""
        result = bytes(self._pending[:size])
        del self._pending[:size]
        return result

    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append(bytes(data))
        index, command = data[2], data[3]
        if index == INDEX_GENERIC and command == CMD_STATUS_FLAGS:
            status = int(self.power_present) << 2 | (1 << 3)
            response = encode_frame(Frame(index, command, bytes((status,))))
        elif index == INDEX_PARAMETER and command == PARAM_MAX_BACKUP_TIME:
            if data[1] == 3:
                response = encode_frame(
                    Frame(index, command, parameter_data(command, self.backup_time_enabled, self.backup_time_value))
                )
            else:
                enabled, low, high = data[4:7]
                self.backup_time_enabled = bool(enabled)
                self.backup_time_value = low | high << 8
                response = encode_frame(
                    Frame(index, command)
                )
        else:
            response = encode_frame(Frame(index, command))
        self._incoming.put(response)
        return len(data)

    def reset_input_buffer(self) -> None:
        self._pending.clear()
        while True:
            try:
                self._incoming.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        pass


class UpsCommandServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.port = FakePort()
        self.serial = UpsSerialService(self.port, response_timeout=0.2)
        await self.serial.start()

    async def asyncTearDown(self) -> None:
        await self.serial.close()

    async def test_restart_checks_fresh_mains_status_and_sends_delay(self) -> None:
        commands = UpsCommandService(self.serial, max_restart_delay_seconds=120)
        await commands.restart_output(30)

        self.assertEqual([frame[3] for frame in self.port.writes], [CMD_STATUS_FLAGS, CMD_UPS_OUTPUT])
        self.assertEqual(self.port.writes[-1], bytes((0x01, 0x04, INDEX_GENERIC, CMD_UPS_OUTPUT, 30, 0x04)))

    async def test_restart_is_rejected_on_battery_before_output_command(self) -> None:
        self.port.power_present = False
        commands = UpsCommandService(self.serial)

        with self.assertRaises(UpsPreconditionFailed):
            await commands.restart_output(10)

        self.assertEqual([frame[3] for frame in self.port.writes], [CMD_STATUS_FLAGS])

    async def test_restart_delay_must_fit_root_limit(self) -> None:
        commands = UpsCommandService(self.serial, max_restart_delay_seconds=20)

        with self.assertRaises(UpsPreconditionFailed):
            await commands.restart_output(21)

        self.assertEqual(self.port.writes, [])

    async def test_shutdown_on_battery_requires_root_permission(self) -> None:
        self.port.power_present = False
        commands = UpsCommandService(self.serial)

        with self.assertRaises(UpsPreconditionFailed):
            await commands.shutdown_output()

        self.assertEqual([frame[3] for frame in self.port.writes], [CMD_STATUS_FLAGS])

    async def test_root_can_enable_shutdown_on_battery(self) -> None:
        self.port.power_present = False
        commands = UpsCommandService(self.serial, allow_shutdown_on_battery=True)
        await commands.shutdown_output()

        self.assertEqual([frame[3] for frame in self.port.writes], [CMD_STATUS_FLAGS, CMD_UPS_OUTPUT])
        self.assertEqual(self.port.writes[-1][4], 0)

    async def test_backup_profile_obeys_root_limit_and_ups_limits(self) -> None:
        commands = UpsCommandService(self.serial)
        result = await commands.set_backup_time_profile(
            enabled=True,
            seconds=30,
            root_max_seconds=60,
        )
        self.assertTrue(result.enabled)
        self.assertEqual(result.value, 30)

        with self.assertRaises(UpsPreconditionFailed):
            await commands.set_backup_time_profile(
                enabled=True,
                seconds=61,
                root_max_seconds=60,
            )


if __name__ == "__main__":
    unittest.main()