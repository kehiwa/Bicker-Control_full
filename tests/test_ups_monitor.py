import unittest

from bicker_control.ups.monitor import (
    CMD_BATTERY_CURRENT,
    CMD_INPUT_VOLTAGE,
    UpsMonitor,
)
from bicker_control.ups.protocol import INDEX_GENERIC, StatusFlag, ProtocolError
from bicker_control.ups.transport import UpsResponseTimeout


class FakeReader:
    def __init__(self) -> None:
        self.responses: dict[int, bytes | Exception] = {}
        self.requests: list[tuple[int, int]] = []

    async def request(self, index: int, command: int, data: bytes = b"") -> bytes:
        self.requests.append((index, command))
        response = self.responses[command]
        if isinstance(response, Exception):
            raise response
        return response


class UpsMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_and_measurements_are_cached_and_rotate(self) -> None:
        now = [100.0]
        reader = FakeReader()
        reader.responses[0x40] = bytes((int(StatusFlag.POWER_PRESENT | StatusFlag.BATTERY_PRESENT),))
        reader.responses[CMD_INPUT_VOLTAGE] = bytes((0x10, 0x27))
        reader.responses[0x42] = bytes((0xE8, 0x03))
        monitor = UpsMonitor(reader, clock=lambda: now[0])

        first = await monitor.poll_once()
        self.assertTrue(first.status_valid)
        self.assertTrue(first.communication_ok)
        self.assertTrue(first.is_status_fresh(0.1, now=100.05))
        self.assertFalse(first.on_battery)
        self.assertEqual(first.measurements["input_voltage"].value, 10000)
        self.assertEqual(first.measurements["input_voltage"].unit, "mV")

        now[0] = 105.0
        second = await monitor.poll_once()
        self.assertEqual(second.measurements["input_current"].value, 1000)
        self.assertEqual(reader.requests[:4], [(INDEX_GENERIC, 0x40), (INDEX_GENERIC, CMD_INPUT_VOLTAGE), (INDEX_GENERIC, 0x40), (INDEX_GENERIC, 0x42)])

    async def test_battery_state_selects_fast_poll_interval(self) -> None:
        reader = FakeReader()
        reader.responses[0x40] = bytes((int(StatusFlag.BATTERY_PRESENT),))
        reader.responses[CMD_INPUT_VOLTAGE] = bytes((0, 0))
        monitor = UpsMonitor(reader, mains_interval=5.0, battery_interval=1.0)
        snapshot = await monitor.poll_once()
        self.assertTrue(snapshot.on_battery)
        self.assertEqual(monitor._next_interval(), 1.0)

    async def test_three_status_failures_mark_communication_down(self) -> None:
        reader = FakeReader()
        reader.responses[0x40] = UpsResponseTimeout("no status")
        monitor = UpsMonitor(reader, comm_fail_count=3)

        first = await monitor.poll_once()
        second = await monitor.poll_once()
        third = await monitor.poll_once()

        self.assertTrue(first.communication_ok)
        self.assertTrue(second.communication_ok)
        self.assertFalse(third.communication_ok)
        self.assertFalse(third.status_valid)
        self.assertEqual(third.consecutive_status_failures, 3)
        self.assertEqual(monitor._next_interval(), 4.0)

    async def test_custom_failure_threshold_is_used_by_snapshot(self) -> None:
        reader = FakeReader()
        reader.responses[0x40] = UpsResponseTimeout("no status")
        monitor = UpsMonitor(reader, comm_fail_count=2)

        await monitor.poll_once()
        second = await monitor.poll_once()

        self.assertFalse(second.communication_ok)
        self.assertEqual(second.comm_fail_count, 2)

    async def test_status_retry_interval_uses_capped_exponential_backoff(self) -> None:
        reader = FakeReader()
        reader.responses[0x40] = UpsResponseTimeout("no status")
        monitor = UpsMonitor(reader, retry_interval=0.5, retry_max_interval=2.0)

        self.assertEqual(monitor._next_interval(), 1.0)
        await monitor.poll_once()
        self.assertEqual(monitor._next_interval(), 0.5)
        await monitor.poll_once()
        self.assertEqual(monitor._next_interval(), 1.0)
        await monitor.poll_once()
        self.assertEqual(monitor._next_interval(), 2.0)
        await monitor.poll_once()
        self.assertEqual(monitor._next_interval(), 2.0)

    async def test_telemetry_failure_does_not_invalidate_successful_status(self) -> None:
        reader = FakeReader()
        reader.responses[0x40] = bytes((int(StatusFlag.POWER_PRESENT),))
        reader.responses[CMD_INPUT_VOLTAGE] = ProtocolError("short reading")
        monitor = UpsMonitor(reader)

        snapshot = await monitor.poll_once()
        self.assertTrue(snapshot.communication_ok)
        self.assertTrue(snapshot.status_valid)
        self.assertIsNone(snapshot.communication_error)
        self.assertEqual(snapshot.telemetry_error, "short reading")
        self.assertEqual(monitor._next_interval(), 5.0)

    async def test_signed_battery_current_is_decoded_little_endian(self) -> None:
        reader = FakeReader()
        reader.responses[0x40] = bytes((int(StatusFlag.POWER_PRESENT),))
        reader.responses[CMD_INPUT_VOLTAGE] = bytes((0, 0))
        reader.responses[0x42] = bytes((0, 0))
        reader.responses[0x43] = bytes((0, 0))
        reader.responses[0x44] = bytes((0, 0))
        reader.responses[0x45] = bytes((0, 0))
        reader.responses[CMD_BATTERY_CURRENT] = bytes((0xF6, 0xFF))
        monitor = UpsMonitor(reader)

        snapshot = None
        for _ in range(6):
            snapshot = await monitor.poll_once()

        assert snapshot is not None
        self.assertEqual(snapshot.measurements["battery_current"].value, -10)
        self.assertEqual(snapshot.measurements["battery_current"].unit, "mA")


if __name__ == "__main__":
    unittest.main()