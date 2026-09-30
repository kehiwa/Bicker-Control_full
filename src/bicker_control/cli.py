"""Installable CLI for starting the Bicker Control web service."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Sequence

import uvicorn

from .api import create_app
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
    return parser


def create_runtime(
    *,
    state_db: str,
    serial_port: str,
    baudrate: int,
    serial_timeout: float,
    session_ttl: int,
):
    state_path = Path(state_db).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)

    store = StateStore(state_path)
    ups_port = PySerialPort.open(serial_port, baudrate=baudrate, read_timeout=serial_timeout)
    serial = UpsSerialService(ups_port)
    monitor = UpsMonitor(serial)
    commands = UpsCommandService(serial)
    app = create_app(store, monitor, commands, session_ttl_seconds=session_ttl)

    async def _startup() -> None:
        await serial.start()
        await monitor.start()

    async def _shutdown() -> None:
        await monitor.stop()
        await serial.close()
        store.close()

    app.add_event_handler("startup", _startup)
    app.add_event_handler("shutdown", _shutdown)
    return app


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = create_runtime(
        state_db=args.state_db,
        serial_port=args.serial_port,
        baudrate=args.baudrate,
        serial_timeout=args.serial_timeout,
        session_ttl=args.session_ttl,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
