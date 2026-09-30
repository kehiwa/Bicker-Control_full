import unittest

from bicker_control.syslog_sink import SyslogSink


class SyslogSinkTests(unittest.TestCase):
    def test_rejects_invalid_destination(self) -> None:
        with self.assertRaises(ValueError):
            SyslogSink("", 514)
        with self.assertRaises(ValueError):
            SyslogSink("localhost", 0)


if __name__ == "__main__":
    unittest.main()