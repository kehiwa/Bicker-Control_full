"""Codec for the length-framed Bicker UPS Gen2 serial protocol."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag

SOH = 0x01
EOT = 0x04
INDEX_GENERIC = 0x01
INDEX_PARAMETER = 0x07
CMD_STATUS_FLAGS = 0x40
CMD_UPS_OUTPUT = 0x21
PARAM_MAX_BACKUP_TIME = 0x02

# Keep the same conservative payload limit as the existing ESP32 firmware.
MAX_DATA_LENGTH = 251


class ProtocolError(ValueError):
    """Raised when a frame or response violates the Bicker protocol format."""


class StatusFlag(IntFlag):
    CHARGING = 1 << 0
    DISCHARGING = 1 << 1
    POWER_PRESENT = 1 << 2
    BATTERY_PRESENT = 1 << 3
    SHUTDOWN_RECEIVED = 1 << 4
    OVER_CURRENT = 1 << 5


@dataclass(frozen=True, slots=True)
class Frame:
    index: int
    command: int
    data: bytes = b""


@dataclass(frozen=True, slots=True)
class Parameter:
    parameter_id: int
    minimum: int
    maximum: int
    default: int
    enabled: bool
    value: int


def _check_byte(name: str, value: int) -> None:
    if not 0 <= value <= 0xFF:
        raise ProtocolError(f"{name} must be an unsigned byte")


def encode_frame(frame: Frame) -> bytes:
    """Encode SOH | size | index | command | data | EOT."""
    _check_byte("index", frame.index)
    _check_byte("command", frame.command)
    if len(frame.data) > MAX_DATA_LENGTH:
        raise ProtocolError(f"data exceeds {MAX_DATA_LENGTH} bytes")

    size = 3 + len(frame.data)
    return bytes((SOH, size, frame.index, frame.command)) + frame.data + bytes((EOT,))


class FrameDecoder:
    """Incremental decoder that resynchronizes after noise or malformed frames."""

    def __init__(self, max_data_length: int = MAX_DATA_LENGTH) -> None:
        if not 0 <= max_data_length <= MAX_DATA_LENGTH:
            raise ValueError(f"max_data_length must be between 0 and {MAX_DATA_LENGTH}")
        self._max_data_length = max_data_length
        self._buffer = bytearray()

    def reset(self) -> None:
        self._buffer.clear()

    def feed(self, chunk: bytes | bytearray | memoryview) -> list[Frame]:
        self._buffer.extend(chunk)
        frames: list[Frame] = []

        while self._buffer:
            start = self._buffer.find(SOH)
            if start < 0:
                self._buffer.clear()
                break
            if start:
                del self._buffer[:start]
            if len(self._buffer) < 2:
                break

            size = self._buffer[1]
            if size < 3 or size - 3 > self._max_data_length:
                del self._buffer[0]
                continue

            frame_length = size + 2
            if len(self._buffer) < frame_length:
                break
            if self._buffer[frame_length - 1] != EOT:
                if len(self._buffer) > frame_length and self._buffer[frame_length] == SOH:
                    del self._buffer[:frame_length]
                    continue

                search_from = 1
                first_incomplete: int | None = None
                recovered = False
                while True:
                    next_start = self._buffer.find(SOH, search_from, frame_length)
                    if next_start < 0:
                        break
                    search_from = next_start + 1
                    if len(self._buffer) - next_start < 2:
                        if first_incomplete is None:
                            first_incomplete = next_start
                        continue

                    next_size = self._buffer[next_start + 1]
                    if next_size < 3 or next_size - 3 > self._max_data_length:
                        continue
                    next_length = next_size + 2
                    if len(self._buffer) - next_start < next_length:
                        if first_incomplete is None:
                            first_incomplete = next_start
                        continue
                    if self._buffer[next_start + next_length - 1] == EOT:
                        del self._buffer[:next_start]
                        recovered = True
                        break

                if recovered:
                    continue
                if first_incomplete is not None:
                    del self._buffer[:first_incomplete]
                    break
                del self._buffer[:frame_length]
                continue

            frames.append(
                Frame(
                    index=self._buffer[2],
                    command=self._buffer[3],
                    data=bytes(self._buffer[4 : frame_length - 1]),
                )
            )
            del self._buffer[:frame_length]

        return frames


def status_request() -> bytes:
    return encode_frame(Frame(INDEX_GENERIC, CMD_STATUS_FLAGS))


def parameter_get_request(parameter_id: int) -> bytes:
    _check_byte("parameter_id", parameter_id)
    return encode_frame(Frame(INDEX_PARAMETER, parameter_id))


def parameter_set_request(parameter_id: int, enabled: bool, value: int) -> bytes:
    _check_byte("parameter_id", parameter_id)
    if not 0 <= value <= 0xFFFF:
        raise ProtocolError("parameter value must be an unsigned 16-bit integer")
    data = bytes((int(enabled), value & 0xFF, value >> 8))
    return encode_frame(Frame(INDEX_PARAMETER, parameter_id, data))


def parameter_set_frame(parameter_id: int, enabled: bool, value: int) -> Frame:
    """Create the parameter SET request as a frame for queued transactions."""
    _check_byte("parameter_id", parameter_id)
    if not 0 <= value <= 0xFFFF:
        raise ProtocolError("parameter value must be an unsigned 16-bit integer")
    data = bytes((int(enabled), value & 0xFF, value >> 8))
    return Frame(INDEX_PARAMETER, parameter_id, data)


def ups_output_frame(value: int) -> Frame:
    """Create Generic/UpsOutput command (0=off, 1-254=delayed restart, 255=on)."""
    _check_byte("output value", value)
    return Frame(INDEX_GENERIC, CMD_UPS_OUTPUT, bytes((value,)))


def parse_status_flags(data: bytes) -> StatusFlag:
    if not data:
        raise ProtocolError("status response is empty")
    return StatusFlag(data[0])


def parse_parameter(data: bytes, requested_id: int | None = None) -> Parameter:
    """Parse ID | min16 | max16 | default16 | enabled8 | value16."""
    if len(data) != 10:
        raise ProtocolError(f"parameter response must be 10 bytes, got {len(data)}")
    if requested_id is not None and data[0] != requested_id:
        raise ProtocolError(
            f"parameter response ID 0x{data[0]:02x} does not match "
            f"requested ID 0x{requested_id:02x}"
        )
    if data[7] not in (0, 1):
        raise ProtocolError(f"invalid parameter enabled value {data[7]}")

    return Parameter(
        parameter_id=data[0],
        minimum=int.from_bytes(data[1:3], "little"),
        maximum=int.from_bytes(data[3:5], "little"),
        default=int.from_bytes(data[5:7], "little"),
        enabled=bool(data[7]),
        value=int.from_bytes(data[8:10], "little"),
    )