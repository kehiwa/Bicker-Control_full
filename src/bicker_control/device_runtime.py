"""Runtime orchestration for GPIO events, reset actions, UPS commands, and logs."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from .device_io import InputAction, InputChannel, InputEvent, InputService, ResetAction, ResetButton, ResetEvent
from .state_store import StateStore
from .ups.commands import UpsCommandService


@dataclass(frozen=True, slots=True)
class RuntimeLimits:
    restart_delay_seconds: int = 30
    root_max_backup_seconds: int = 300

    def __post_init__(self) -> None:
        if not 1 <= self.restart_delay_seconds <= 254:
            raise ValueError("restart delay must be between 1 and 254 seconds")
        if not 1 <= self.root_max_backup_seconds <= 0xFFFF:
            raise ValueError("root backup limit must be between 1 and 65535 seconds")


class DeviceActionExecutor:
    """Execute configured input actions through the existing UPS command owner."""

    def __init__(
        self,
        store: StateStore,
        commands: UpsCommandService,
        *,
        limits: RuntimeLimits = RuntimeLimits(),
    ) -> None:
        self._store = store
        self._commands = commands
        self._limits = limits

    async def handle_input(self, event: InputEvent) -> None:
        self._store.append_event(
            "input.activated",
            source=f"gpio.{event.channel}",
            details={"action": event.action.value},
        )
        try:
            if event.action is InputAction.SHUTDOWN:
                await self._commands.shutdown_output()
            elif event.action is InputAction.RESTART:
                await self._commands.restart_output(self._limits.restart_delay_seconds)
            elif event.action is InputAction.BACKUP_PROFILE:
                await self._commands.set_backup_time_profile(
                    enabled=True,
                    seconds=self._limits.restart_delay_seconds,
                    root_max_seconds=self._limits.root_max_backup_seconds,
                )
            elif event.action is InputAction.INHIBIT:
                self._store.append_event(
                    "ups.inhibit.requested",
                    source=f"gpio.{event.channel}",
                )
            elif event.action is InputAction.EVENT:
                self._store.append_event(
                    "input.event",
                    source=f"gpio.{event.channel}",
                )
        except Exception as exc:
            self._store.append_event(
                "input.action.failed",
                source=f"gpio.{event.channel}",
                severity="error",
                details={"action": event.action.value, "error": str(exc)},
            )


class DeviceRuntime:
    """Own the input poller and reset state machine for one controller process."""

    def __init__(
        self,
        store: StateStore,
        commands: UpsCommandService,
        backend,
        channels: tuple[InputChannel, ...],
        *,
        reset_pin: int = 17,
        network_reset: Callable[[], Awaitable[None] | None] | None = None,
        factory_reset: Callable[[], Awaitable[None] | None] | None = None,
    ) -> None:
        self._store = store
        self._backend = backend
        self._executor = DeviceActionExecutor(store, commands)
        self._inputs = InputService(backend, channels, self._executor.handle_input)
        self._reset_pin = reset_pin
        self._reset = ResetButton()
        self._network_reset = network_reset
        self._factory_reset = factory_reset
        self._reset_task: asyncio.Task[None] | None = None

    def configure_input(self, name: str, action: InputAction, *, active_high: bool = True) -> None:
        current = next((channel for channel in self._inputs.channels() if channel.name == name), None)
        if current is None:
            raise ValueError(f"unknown input channel {name!r}")
        self._inputs.update_channel(
            InputChannel(
                current.name,
                current.pin,
                action=action,
                active_high=active_high,
                debounce_seconds=current.debounce_seconds,
                rate_limit_seconds=current.rate_limit_seconds,
            )
        )

    def input_configuration(self) -> list[dict[str, object]]:
        return [
            {
                "name": channel.name,
                "pin": channel.pin,
                "action": channel.action.value,
                "active_high": channel.active_high,
                "debounce_seconds": channel.debounce_seconds,
                "rate_limit_seconds": channel.rate_limit_seconds,
            }
            for channel in self._inputs.channels()
        ]

    async def start(self) -> None:
        await self._inputs.start()
        self._reset_task = asyncio.create_task(self._run_reset(), name="reset-button-service")

    async def stop(self) -> None:
        if self._reset_task is not None:
            self._reset_task.cancel()
            try:
                await self._reset_task
            except asyncio.CancelledError:
                pass
            self._reset_task = None
        await self._inputs.stop()
        self._backend.close()

    async def process_reset_once(self, now: float | None = None) -> ResetEvent | None:
        event = self._reset.update(not self._backend.read(self._reset_pin), now=now)
        if event is None or event.action is ResetAction.NONE:
            return event
        self._store.append_event(
            f"reset.{event.action.value}",
            source="gpio.reset",
            severity="warning",
            details={"held_seconds": event.held_seconds},
        )
        callback = self._network_reset if event.action is ResetAction.NETWORK else self._factory_reset
        if callback is not None:
            result = callback()
            if hasattr(result, "__await__"):
                await result
        return event

    async def _run_reset(self) -> None:
        while True:
            await self.process_reset_once()
            await asyncio.sleep(0.02)