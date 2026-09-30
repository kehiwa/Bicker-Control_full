"""UPS status and telemetry cache with a low-traffic rotating poller."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Mapping, Protocol

from .protocol import (
    CMD_STATUS_FLAGS,
    INDEX_GENERIC,
    ProtocolError,
    StatusFlag,
    parse_status_flags,
)
from .transport import UpsTransportError

CMD_INPUT_VOLTAGE = 0x41
CMD_INPUT_CURRENT = 0x42
CMD_OUTPUT_VOLTAGE = 0x43
CMD_OUTPUT_CURRENT = 0x44
CMD_BATTERY_VOLTAGE = 0x45
CMD_BATTERY_CURRENT = 0x46
CMD_BATTERY_SOC = 0x47
CMD_BATTERY_TEMPERATURE = 0x4A


class UpsReader(Protocol):
    async def request(self, index: int, command: int, data: bytes = b"") -> bytes: ...


@dataclass(frozen=True, slots=True)
class MeasurementSpec:
    command: int
    name: str
    unit: str
    signed: bool = False
    byte_count: int = 2


MEASUREMENT_SPECS: tuple[MeasurementSpec, ...] = (
    MeasurementSpec(CMD_INPUT_VOLTAGE, "input_voltage", "mV"),
    MeasurementSpec(CMD_INPUT_CURRENT, "input_current", "mA"),
    MeasurementSpec(CMD_OUTPUT_VOLTAGE, "output_voltage", "mV"),
    MeasurementSpec(CMD_OUTPUT_CURRENT, "output_current", "mA"),
    MeasurementSpec(CMD_BATTERY_VOLTAGE, "battery_voltage", "mV"),
    MeasurementSpec(CMD_BATTERY_CURRENT, "battery_current", "mA", signed=True),
    MeasurementSpec(CMD_BATTERY_SOC, "battery_soc", "%", byte_count=1),
    MeasurementSpec(CMD_BATTERY_TEMPERATURE, "battery_temperature", "K"),
)


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    value: int
    unit: str
    observed_at: float


@dataclass(frozen=True, slots=True)
class UpsSnapshot:
    flags: StatusFlag
    status_observed_at: float | None
    measurements: Mapping[str, Measurement]
    consecutive_status_failures: int
    comm_fail_count: int
    communication_error: str | None
    telemetry_error: str | None

    @property
    def status_valid(self) -> bool:
        return self.status_observed_at is not None

    @property
    def communication_ok(self) -> bool:
        return self.consecutive_status_failures < self.comm_fail_count

    @property
    def on_battery(self) -> bool:
        return self.status_valid and not bool(self.flags & StatusFlag.POWER_PRESENT)

    def is_status_fresh(self, max_age: float, *, now: float | None = None) -> bool:
        if max_age < 0:
            raise ValueError("max_age must not be negative")
        if self.status_observed_at is None:
            return False
        current_time = time.monotonic() if now is None else now
        return current_time - self.status_observed_at <= max_age


class UpsMonitor:
    """Poll status frequently and rotate one measurement per successful cycle."""

    def __init__(
        self,
        reader: UpsReader,
        *,
        mains_interval: float = 5.0,
        battery_interval: float = 1.0,
        retry_interval: float = 1.0,
        retry_max_interval: float = 30.0,
        comm_fail_count: int = 3,
        clock=time.monotonic,
    ) -> None:
        if min(mains_interval, battery_interval, retry_interval, retry_max_interval) <= 0:
            raise ValueError("poll intervals must be positive")
        if retry_max_interval < retry_interval:
            raise ValueError("retry_max_interval must be at least retry_interval")
        if comm_fail_count <= 0:
            raise ValueError("comm_fail_count must be positive")
        self._reader = reader
        self._mains_interval = mains_interval
        self._battery_interval = battery_interval
        self._retry_interval = retry_interval
        self._retry_max_interval = retry_max_interval
        self._comm_fail_count = comm_fail_count
        self._clock = clock
        self._flags = StatusFlag(0)
        self._status_observed_at: float | None = None
        self._measurements: dict[str, Measurement] = {}
        self._consecutive_status_failures = 0
        self._communication_error: str | None = None
        self._telemetry_error: str | None = None
        self._measurement_cursor = 0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def snapshot(self) -> UpsSnapshot:
        return UpsSnapshot(
            flags=self._flags,
            status_observed_at=self._status_observed_at,
            measurements=dict(self._measurements),
            consecutive_status_failures=self._consecutive_status_failures,
            comm_fail_count=self._comm_fail_count,
            communication_error=self._communication_error,
            telemetry_error=self._telemetry_error,
        )

    async def poll_once(self) -> UpsSnapshot:
        """Poll status and at most one rotating measurement."""
        try:
            response = await self._reader.request(INDEX_GENERIC, CMD_STATUS_FLAGS)
            flags = parse_status_flags(response)
        except (UpsTransportError, ProtocolError, OSError) as exc:
            self._consecutive_status_failures += 1
            self._communication_error = str(exc)
            return self.snapshot()

        self._flags = flags
        self._status_observed_at = self._clock()
        self._consecutive_status_failures = 0
        self._communication_error = None
        await self._poll_one_measurement()
        return self.snapshot()

    async def start(self, *, poll_immediately: bool = True) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(poll_immediately), name="ups-status-monitor"
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self, poll_immediately: bool) -> None:
        if not poll_immediately:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._next_interval())
                return
            except TimeoutError:
                pass

        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._next_interval())
            except TimeoutError:
                continue

    def _next_interval(self) -> float:
        if self._communication_error is not None:
            exponent = max(0, self._consecutive_status_failures - 1)
            return min(self._retry_interval * (2**exponent), self._retry_max_interval)
        if self._flags & StatusFlag.POWER_PRESENT:
            return self._mains_interval
        return self._battery_interval

    async def _poll_one_measurement(self) -> None:
        spec = MEASUREMENT_SPECS[self._measurement_cursor]
        self._measurement_cursor = (self._measurement_cursor + 1) % len(MEASUREMENT_SPECS)
        try:
            response = await self._reader.request(INDEX_GENERIC, spec.command)
            if len(response) < spec.byte_count:
                raise ProtocolError(
                    f"{spec.name} response must contain {spec.byte_count} bytes"
                )
            value = int.from_bytes(
                response[: spec.byte_count], "little", signed=spec.signed
            )
        except (UpsTransportError, ProtocolError, OSError) as exc:
            self._telemetry_error = str(exc)
            return

        self._measurements[spec.name] = Measurement(
            name=spec.name,
            value=value,
            unit=spec.unit,
            observed_at=self._clock(),
        )
        self._telemetry_error = None