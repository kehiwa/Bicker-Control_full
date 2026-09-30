import unittest

from bicker_control.snmp_traps import SnmpTrapSender


class SnmpTrapSenderTests(unittest.TestCase):
    def test_rejects_invalid_configuration(self) -> None:
        with self.assertRaises(ValueError):
            SnmpTrapSender("", 162)
        with self.assertRaises(ValueError):
            SnmpTrapSender("localhost", 0)
        with self.assertRaises(ValueError):
            SnmpTrapSender("localhost", 162, notify_type="broadcast")
        with self.assertRaises(ValueError):
            SnmpTrapSender("localhost", 162, min_severity="verbose")

    def test_filters_below_minimum_severity_and_stops_cleanly(self) -> None:
        sender = SnmpTrapSender("127.0.0.1", 39162, min_severity="warning")
        try:
            sender("ups.measurement", "info", "ups.monitor", {})
        finally:
            sender.close()


if __name__ == "__main__":
    unittest.main()
