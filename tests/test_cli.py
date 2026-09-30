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

    def test_parser_accepts_gpio_and_tls_configuration(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "--gpio-backend",
            "memory",
            "--gpio-chip",
            "1",
            "--tls-certfile",
            "/etc/cert.pem",
            "--tls-keyfile",
            "/etc/key.pem",
        ])
        self.assertEqual(args.gpio_backend, "memory")
        self.assertEqual(args.gpio_chip, 1)
        self.assertEqual(args.tls_certfile, "/etc/cert.pem")
        self.assertEqual(args.tls_keyfile, "/etc/key.pem")

    def test_parser_accepts_snmp_configuration(self) -> None:
        parser = build_parser()
        args = parser.parse_args([
            "--snmp-host",
            "127.0.0.1",
            "--snmp-port",
            "1161",
            "--snmp-community",
            "readonly-community",
            "--snmp-community-user-id",
            "1",
        ])
        self.assertEqual(args.snmp_host, "127.0.0.1")
        self.assertEqual(args.snmp_port, 1161)
        self.assertEqual(args.snmp_community, "readonly-community")
        self.assertEqual(args.snmp_community_user_id, 1)

    def test_parser_accepts_network_backend(self) -> None:
        args = build_parser().parse_args(["--network-backend", "memory"])
        self.assertEqual(args.network_backend, "memory")


if __name__ == "__main__":
    unittest.main()
