import tempfile
import unittest
from pathlib import Path

from bicker_control.api import DevicePolicy
from bicker_control.snmp_agent import SnmpAgent, SnmpConfig
from bicker_control.state_store import StateStore
from bicker_control.ups.monitor import UpsMonitor


class FakeReader:
    async def request(self, index: int, command: int, data: bytes = b"") -> bytes:
        return b"\x04" if command == 0x40 else b"\x00\x00"


class FakeCommands:
    async def shutdown_output(self) -> None:
        return None


class SnmpAgentTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_registers_dynamic_mib_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3")
            root = store.create_initial_root("root-owner", "root-password-long-1")
            policy = DevicePolicy(store, UpsMonitor(FakeReader()), FakeCommands())
            agent = SnmpAgent(
                store,
                policy,
                SnmpConfig(
                    port=1161,
                    community="readonly-community",
                    community_user_id=root.user_id,
                ),
            )
            await agent.start()
            try:
                self.assertEqual(agent._snapshot({"securityName": "bicker-v2c"})["flags"], 0)
                name, value = agent._instances["bickerUpsStatus"].readGet(
                    (1, 3, 6, 1, 4, 1, 53864, 1, 1, 0), None, securityName="bicker-v2c"
                )
                self.assertEqual(int(value), 0)
            finally:
                await agent.stop()
                store.close()


if __name__ == "__main__":
    unittest.main()