import tempfile
import unittest
from pathlib import Path

from bicker_control.device_io import InputAction, InputEvent
from bicker_control.device_runtime import DeviceActionExecutor, RuntimeLimits
from bicker_control.state_store import StateStore


class FakeCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, int]] = []

    async def set_backup_time_profile(self, *, enabled: bool, seconds: int, root_max_seconds: int) -> None:
        self.calls.append((enabled, seconds))


class BackupProfileArbitrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "runtime.sqlite3")
        self.commands = FakeCommands()
        self.executor = DeviceActionExecutor(
            self.store,
            self.commands,
            limits=RuntimeLimits(root_max_backup_seconds=300, default_backup_seconds=60),
        )

    async def asyncTearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    async def test_shortest_active_profile_wins_and_release_restores(self) -> None:
        await self.executor.handle_input(
            InputEvent("IN1", InputAction.BACKUP_PROFILE, True, 1.0, profile_seconds=30)
        )
        await self.executor.handle_input(
            InputEvent("IN2", InputAction.BACKUP_PROFILE, True, 2.0, profile_seconds=20)
        )
        await self.executor.handle_input(
            InputEvent("IN2", InputAction.BACKUP_PROFILE, False, 3.0, profile_seconds=20)
        )
        await self.executor.handle_input(
            InputEvent("IN1", InputAction.BACKUP_PROFILE, False, 4.0, profile_seconds=30)
        )
        self.assertEqual(
            self.commands.calls,
            [(True, 30), (True, 20), (True, 30), (False, 60)],
        )

    async def test_release_is_ignored_for_edge_triggered_actions(self) -> None:
        await self.executor.handle_input(InputEvent("IN3", InputAction.RESTART, False, 1.0))
        self.assertEqual(self.commands.calls, [])
        self.assertEqual(self.store.device_events(), ())


if __name__ == "__main__":
    unittest.main()
