"""Hardware-neutral GPIO services for contacts, reset handling, and status LEDs."""

from __future__ import annotations

import asyncio
import inspect
import time
from dataclasses import dataclass
from enum import Enum
from typing import Awaitable, Callable, Protocol


class GpioBackend(Protocol):
    def read(self, pin: int) -> bool: ...

    def write(self, pin: int, value: bool) -> None: ...

    def close(self) -> None: ...


class InputAction(str, Enum):
    NONE = "none"
    INHIBIT = "inhibit"
    BACKUP_PROFILE = "backup_profile"
    SHUTDOWN = "shutdown"
    RESTART = "restart"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class InputChannel:
    name: str
    pin: int
    action: InputAction = InputAction.NONE
    active_high: bool = True
    debounce_seconds: float = 0.05
    rate_limit_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("input name must not be empty")
        if self.pin < 0:
            raise ValueError("GPIO pin must not be negative")
        if self.debounce_seconds < 0 or self.rate_limit_seconds < 0:
            raise ValueError("input timing values must not be negative")


@dataclass(frozen=True, slots=True)
class InputEvent:
    channel: str
    action: InputAction
    active: bool
    observed_at: float


class MemoryGpioBackend:
    """Deterministic GPIO backend for tests and development without CM5 hardware."""

    def __init__(self) -> None:
        self.inputs: dict[int, bool] = {}
        self.outputs: dict[int, bool] = {}

    def read(self, pin: int) -> bool:
        return self.inputs.get(pin, False)

    def write(self, pin: int, value: bool) -> None:
        self.outputs[pin] = bool(value)

    def close(self) -> None:
        return None


class LgpioBackend:
    """Raspberry Pi GPIO backend using the Linux ``lgpio`` character-device API."""

    def __init__(
        self,
        *,
        chip: int = 0,
        input_pins: tuple[int, ...] = (),
        output_pins: tuple[int, ...] = (),
        pull_up_inputs: tuple[int, ...] = (),
    ) -> None:
        try:
            import lgpio
        except ImportError as exc:
            raise RuntimeError("lgpio is required for the CM5 GPIO backend") from exc
        self._lgpio = lgpio
        self._handle = lgpio.gpiochip_open(chip)
        self._claimed = set(input_pins) | set(output_pins)
        try:
            for pin in input_pins:
                pull = lgpio.SET_PULL_UP if pin in pull_up_inputs else lgpio.SET_PULL_NONE
                lgpio.gpio_claim_input(self._handle, pin, pull)
            for pin in output_pins:
                lgpio.gpio_claim_output(self._handle, pin, 0)
        except Exception:
            lgpio.gpiochip_close(self._handle)
            raise

    def read(self, pin: int) -> bool:
        return bool(self._lgpio.gpio_read(self._handle, pin))

    def write(self, pin: int, value: bool) -> None:
        self._lgpio.gpio_write(self._handle, pin, int(value))

    def close(self) -> None:
        if self._handle is not None:
            self._lgpio.gpiochip_close(self._handle)
            self._handle = None


class InputService:
    """Poll dry-contact inputs and emit debounced, rate-limited edge events."""

    def __init__(
        self,
        backend: GpioBackend,
        channels: tuple[InputChannel, ...],
        handler: Callable[[InputEvent], Awaitable[None] | None],
        *,
        poll_interval: float = 0.02,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll interval must be positive")
        if len({channel.name for channel in channels}) != len(channels):
            raise ValueError("input channel names must be unique")
        self._backend = backend
        self._channels = channels
        self._handler = handler
        self._poll_interval = poll_interval
        self._clock = clock
        self._stable: dict[str, bool] = {}
        self._candidate: dict[str, tuple[bool, float]] = {}
        self._last_event: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def update_channel(self, channel: InputChannel) -> None:
        for index, current in enumerate(self._channels):
            if current.name == channel.name:
                self._channels = self._channels[:index] + (channel,) + self._channels[index + 1 :]
                return
        raise ValueError(f"unknown input channel {channel.name!r}")

    def channels(self) -> tuple[InputChannel, ...]:
        return self._channels

    async def process_once(self, now: float | None = None) -> tuple[InputEvent, ...]:
        observed_at = self._clock() if now is None else now
        events: list[InputEvent] = []
        for channel in self._channels:
            raw = self._backend.read(channel.pin)
            active = raw if channel.active_high else not raw
            event = self._process_channel(channel, active, observed_at)
            if event is not None:
                events.append(event)
                result = self._handler(event)
                if inspect.isawaitable(result):
                    await result
        return tuple(events)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="gpio-input-service")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    def _process_channel(
        self,
        channel: InputChannel,
        active: bool,
        observed_at: float,
    ) -> InputEvent | None:
        previous = self._stable.get(channel.name)
        if previous is None:
            self._stable[channel.name] = active
            return None
        if active == previous:
            self._candidate.pop(channel.name, None)
            return None

        candidate = self._candidate.get(channel.name)
        if candidate is None or candidate[0] != active:
            self._candidate[channel.name] = (active, observed_at)
            return None
        if observed_at - candidate[1] < channel.debounce_seconds:
            return None

        self._stable[channel.name] = active
        self._candidate.pop(channel.name, None)
        if not active:
            return None
        last_event = self._last_event.get(channel.name)
        if last_event is not None and observed_at - last_event < channel.rate_limit_seconds:
            return None
        self._last_event[channel.name] = observed_at
        return InputEvent(channel.name, channel.action, True, observed_at)

    async def _run(self) -> None:
        while not self._stop.is_set():
            await self.process_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except TimeoutError:
                continue


class ResetAction(str, Enum):
    NONE = "none"
    NETWORK = "network"
    FACTORY = "factory"


@dataclass(frozen=True, slots=True)
class ResetEvent:
    action: ResetAction
    held_seconds: float
    observed_at: float


class ResetButton:
    """Release-triggered reset state machine with the documented hold windows."""

    def __init__(
        self,
        *,
        network_after: float = 3.0,
        factory_after: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0 < network_after < factory_after:
            raise ValueError("reset thresholds must be positive and ordered")
        self._network_after = network_after
        self._factory_after = factory_after
        self._clock = clock
        self._pressed_at: float | None = None

    def update(self, pressed: bool, now: float | None = None) -> ResetEvent | None:
        observed_at = self._clock() if now is None else now
        if pressed:
            if self._pressed_at is None:
                self._pressed_at = observed_at
            return None
        if self._pressed_at is None:
            return None
        held_seconds = max(0.0, observed_at - self._pressed_at)
        self._pressed_at = None
        action = (
            ResetAction.FACTORY
            if held_seconds >= self._factory_after
            else ResetAction.NETWORK
            if held_seconds >= self._network_after
            else ResetAction.NONE
        )
        return ResetEvent(action, held_seconds, observed_at)


class StatusLed:
    """RGB status output with explicit state mapping and no hardware assumptions."""

    STATES = {
        "boot": (True, False, False),
        "ready": (False, True, False),
        "battery": (True, True, False),
        "fault": (True, False, True),
        "reset-network": (True, True, True),
    }

    def __init__(self, backend: GpioBackend, red: int, green: int, blue: int) -> None:
        self._backend = backend
        self._pins = (red, green, blue)

    def set_state(self, state: str) -> None:
        try:
            values = self.STATES[state]
        except KeyError as exc:
            raise ValueError(f"unknown LED state {state!r}") from exc
        for pin, value in zip(self._pins, values):
            self._backend.write(pin, value)