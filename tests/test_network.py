import unittest

from bicker_control.network import MemoryNetworkBackend, NetworkConfig, NetworkService


class NetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_backend_applies_valid_configuration(self) -> None:
        backend = MemoryNetworkBackend()
        config = NetworkConfig(
            ethernet_mode="static",
            ethernet_address="192.0.2.10/24",
            ethernet_gateway="192.0.2.1",
            wifi_ssid="test-network",
        )
        await NetworkService(backend).apply(config)
        self.assertEqual(backend.current, config)

    def test_static_configuration_requires_address(self) -> None:
        with self.assertRaises(ValueError):
            NetworkConfig(ethernet_mode="static")


if __name__ == "__main__":
    unittest.main()