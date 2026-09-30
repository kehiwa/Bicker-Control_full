import unittest

from bicker_control.cli import build_parser


class CliTests(unittest.TestCase):
    def test_parser_accepts_runtime_configuration(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "--host",
            "0.0.0.0",
            "--port",
            "8080",
            "--state-db",
            "/tmp/bicker-control.sqlite3",
            "--serial-port",
            "/dev/ttyUSB0",
            "--baudrate",
            "115200",
        ])
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 8080)
        self.assertEqual(args.state_db, "/tmp/bicker-control.sqlite3")
        self.assertEqual(args.serial_port, "/dev/ttyUSB0")
        self.assertEqual(args.baudrate, 115200)


if __name__ == "__main__":
    unittest.main()
