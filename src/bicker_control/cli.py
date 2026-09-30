"""Installable CLI for starting the Bicker Control web service."""

from __future__ import annotations

import argparse
import os
import json
from pathlib import Path
from typing import Sequence

import uvicorn

from .api import create_app
from .device_io import InputAction, InputChannel, LgpioBackend, MemoryGpioBackend, StatusLed
from .device_runtime import DeviceRuntime
from .snmp_agent import SnmpAgent, SnmpConfig, SnmpV3User
from .network import MemoryNetworkBackend, NetworkConfig, NetworkService, NmcliBackend
from .syslog_sink import SyslogSink
from .state_store import StateStore
from .ups.commands import UpsCommandService
from .ups.monitor import UpsMonitor
from .ups.transport import PySerialPort, UpsSerialService

DEFAULT_STATE_DB = Path("/var/lib/bicker-control/state.sqlite3")
DEFAULT_SERIAL_PORT = "/dev/ttyAMA1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Bicker Control service and web API.",
    )
    parser.add_argument("--host", default=os.getenv("BICKER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("BICKER_PORT", "8000")))
    parser.add_argument(
        "--state-db",
        default=os.getenv("BICKER_STATE_DB", str(DEFAULT_STATE_DB)),
        help="SQLite database file for users, settings and audit log.",
    )
    parser.add_argument(
        "--serial-port",
        default=os.getenv("BICKER_SERIAL_PORT", DEFAULT_SERIAL_PORT),
        help="Serial device attached to the UPS, such as /dev/ttyAMA1 or COM3.",
    )
    parser.add_argument(
        "--baudrate",
        type=int,
        default=int(os.getenv("BICKER_BAUDRATE", "38400")),
    )
    parser.add_argument(
        "--serial-timeout",
        type=float,
        default=float(os.getenv("BICKER_SERIAL_TIMEOUT", "0.05")),
    )
    parser.add_argument(
        "--session-ttl",
        type=int,
        default=int(os.getenv("BICKER_SESSION_TTL", "28800")),
        help="Bearer session lifetime in seconds.",
    )
    parser.add_argument(
        "--gpio-backend",
        choices=("lgpio", "memory"),
        default=os.getenv("BICKER_GPIO_BACKEND", "lgpio"),
        help="GPIO backend; use memory only for development without CM5 hardware.",
    )
    parser.add_argument(
        "--gpio-chip",
        type=int,
        default=int(os.getenv("BICKER_GPIO_CHIP", "0")),
    )
    parser.add_argument(
        "--network-backend",
        choices=("nmcli", "memory"),
        default=os.getenv("BICKER_NETWORK_BACKEND", "nmcli"),
    )
    parser.add_argument(
        "--tls-certfile",
        default=os.getenv("BICKER_TLS_CERTFILE"),
        help="PEM certificate for HTTPS; must be used together with --tls-keyfile.",
    )
    parser.add_argument(
        "--tls-keyfile",
        default=os.getenv("BICKER_TLS_KEYFILE"),
        help="PEM private key for HTTPS; must be used together with --tls-certfile.",
    )
    parser.add_argument(
        "--snmp-host",
        default=os.getenv("BICKER_SNMP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--snmp-port",
        type=int,
        default=int(os.getenv("BICKER_SNMP_PORT", "1161")),
    )
    parser.add_argument(
        "--snmp-community",
        default=os.getenv("BICKER_SNMP_COMMUNITY"),
        help="Optional SNMPv2c read-only community; disabled when omitted.",
    )
    parser.add_argument(
        "--snmp-community-user-id",
        type=int,
        default=int(os.getenv("BICKER_SNMP_COMMUNITY_USER_ID", "0")) or None,
    )
    parser.add_argument(
        "--snmp-v3-users",
        default=os.getenv("BICKER_SNMP_V3_USERS", "[]"),
        help="JSON list of username/user_id/auth_key/privacy_key objects.",
    )
    return parser


def create_runtime(
    *,
    state_db: str,
    serial_port: str,
    baudrate: int,
    serial_timeout: float,
    session_ttl: int,
    gpio_backend: str = "lgpio",
    gpio_chip: int = 0,
    network_backend: str = "nmcli",
    snmp_host: str = "127.0.0.1",
    snmp_port: int = 1161,
    snmp_community: str | None = None,
    snmp_community_user_id: int | None = None,
    snmp_v3_users: tuple[SnmpV3User, ...] = (),
):
    state_path = Path(state_db).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    syslog_host = os.getenv("BICKER_SYSLOG_HOST", "").strip()
    syslog_sink = SyslogSink(syslog_host, int(os.getenv("BICKER_SYSLOG_PORT", "514"))) if syslog_host else None
    store = StateStore(state_path, event_sink=syslog_sink)
    ups_port = PySerialPort.open(serial_port, baudrate=baudrate, read_timeout=serial_timeout)
    serial = UpsSerialService(ups_port)
    commands = UpsCommandService(serial)
    channels = tuple(
        InputChannel(name, pin, action=InputAction.NONE, active_high=True)
        for name, pin in (("IN1", 5), ("IN2", 6), ("IN3", 13), ("IN4", 16), ("IN5", 26))
    )
    backend = (
        LgpioBackend(
            chip=gpio_chip,
            input_pins=(5, 6, 13, 16, 26, 17),
            output_pins=(22, 23, 24),
            pull_up_inputs=(5, 6, 13, 16, 26, 17),
        )
        if gpio_backend == "lgpio"
        else MemoryGpioBackend()
    )
    led = StatusLed(backend, 22, 23, 24)
    led.set_state("boot")

    def record_snapshot(snapshot) -> None:
        if not snapshot.communication_ok:
            led.set_state("fault")
        elif snapshot.on_battery:
            led.set_state("battery")
        else:
            led.set_state("ready")
        measurements = {
            name: {"value": item.value, "unit": item.unit}
            for name, item in snapshot.measurements.items()
        }
        store.append_event(
            "ups.measurement",
            source="ups.monitor",
            details={"flags": int(snapshot.flags), "measurements": measurements},
        )

    monitor = UpsMonitor(serial, on_snapshot=record_snapshot)
    network = NetworkService(NmcliBackend() if network_backend == "nmcli" else MemoryNetworkBackend())
    async def _network_reset() -> None:
        store.reset_network_settings()
        await network.apply(NetworkConfig(ethernet_mode="dhcp"))

    def _factory_reset() -> None:
        store.factory_reset()

    device = DeviceRuntime(
        store,
        commands,
        backend,
        channels,
        network_reset=_network_reset,
        factory_reset=_factory_reset,
    )
    for channel in channels:
        saved = store.get_setting_value(f"inputs.{channel.name}", {})
        if isinstance(saved, dict):
            try:
                device.configure_input(
                    channel.name,
                    InputAction(saved.get("action", InputAction.NONE.value)),
                    active_high=bool(saved.get("active_high", channel.active_high)),
                )
            except (TypeError, ValueError):
                store.append_event(
                    "input.configuration.invalid",
                    source=f"startup.inputs.{channel.name}",
                    severity="error",
                    details={"configuration": saved},
                )
    app = create_app(
        store,
        monitor,
        commands,
        session_ttl_seconds=session_ttl,
        network=network,
        device=device,
    )
    snmp = None
    if snmp_community is not None or snmp_v3_users:
        snmp = SnmpAgent(
            store,
            app.state.policy,
            SnmpConfig(
                bind_host=snmp_host,
                port=snmp_port,
                community=snmp_community,
                community_user_id=snmp_community_user_id,
                v3_users=snmp_v3_users,
            ),
        )

    async def _startup() -> None:
        await serial.start()
        await monitor.start()
        await device.start()
        if snmp is not None:
            await snmp.start()

    async def _shutdown() -> None:
        await monitor.stop()
        await device.stop()
        if snmp is not None:
            await snmp.stop()
        await serial.close()
        store.close()
        if syslog_sink is not None:
            syslog_sink.close()

    app.add_event_handler("startup", _startup)
    app.add_event_handler("shutdown", _shutdown)
    return app


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if bool(args.tls_certfile) != bool(args.tls_keyfile):
        raise SystemExit("--tls-certfile and --tls-keyfile must be supplied together")
    try:
        v3_values = json.loads(args.snmp_v3_users)
        snmp_v3_users = tuple(SnmpV3User(**value) for value in v3_values)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit("--snmp-v3-users must be a JSON list of SNMPv3 user objects") from exc
    app = create_runtime(
        state_db=args.state_db,
        serial_port=args.serial_port,
        baudrate=args.baudrate,
        serial_timeout=args.serial_timeout,
        session_ttl=args.session_ttl,
        gpio_backend=args.gpio_backend,
        gpio_chip=args.gpio_chip,
        network_backend=args.network_backend,
        snmp_host=args.snmp_host,
        snmp_port=args.snmp_port,
        snmp_community=args.snmp_community,
        snmp_community_user_id=args.snmp_community_user_id,
        snmp_v3_users=snmp_v3_users,
    )
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        ssl_certfile=args.tls_certfile,
        ssl_keyfile=args.tls_keyfile,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
