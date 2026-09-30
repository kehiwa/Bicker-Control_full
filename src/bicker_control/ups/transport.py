"""Single-owner asynchronous transport for the Bicker UPS serial link."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from .protocol import (
    CMD_STATUS_FLAGS,
    INDEX_GENERIC,
    INDEX_PARAMETER,
    Frame,
    FrameDecoder,
    Parameter,
    StatusFlag,
    encode_frame,
    parameter_set_frame,
    parse_parameter,
    parse_status_flags,
)


class BytePort(Protocol):
    """Minimal blocking byte-port interface owned by the worker thread."""

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def reset_input_buffer(self) -> None: ...

    def close(self) -> None: ...


class UpsTransportError(RuntimeError):
    """Base exception for serial transport failures."""


class UpsResponseTimeout(UpsTransportError, TimeoutError):
    """The UPS did not return a matching response before the deadline."""


class UpsParameterMismatch(UpsTransportError):
    """The UPS acknowledged a parameter write but read-back did not match."""


class UpsPreconditionFailed(UpsTransportError):
    """A guarded command was rejected because its preceding response failed policy."""


@dataclass(frozen=True, slots=True)
class _Request:
    index: int
    command: int
    data: bytes = b""


@dataclass(slots=True)
class _Job:
    requests: tuple[_Request, ...]
    result: asyncio.Future[tuple[bytes, ...]]
    guard_before_last: Callable[[tuple[bytes, ...]], None] | None = None


class UpsSerialService:
    """Serialize all UPS transactions through one queue and one port owner.

    Calls to ``request_many`` are atomic with respect to other callers. This is
    important for operations such as parameter SET followed by read-back.
    """

    def __init__(
        self,
        port: BytePort,
        *,
        response_timeout: float = 1.0,
        read_chunk_size: int = 64,
    ) -> None:
        if response_timeout <= 0:
            raise ValueError("response_timeout must be positive")
        if read_chunk_size <= 0:
            raise ValueError("read_chunk_size must be positive")
        self._port = port
        self._response_timeout = response_timeout
        self._read_chunk_size = read_chunk_size
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._started = False
        self._closing = False

    async def start(self) -> None:
        if self._closing:
            raise RuntimeError("UPS serial service is closed")
        if self._started:
            return
        self._started = True
        self._worker = asyncio.create_task(self._run(), name="ups-serial-owner")

    async def close(self) -> None:
        if self._closing:
            if self._worker is not None:
                await self._worker
            return
        self._closing = True
        if self._worker is not None:
            await self._queue.put(None)
            try:
                await self._worker
            finally:
                await asyncio.to_thread(self._port.close)
        else:
            await asyncio.to_thread(self._port.close)

    async def __aenter__(self) -> UpsSerialService:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def request(self, index: int, command: int, data: bytes = b"") -> bytes:
        results = await self.request_many((_Request(index, command, data),))
        return results[0]

    async def request_many(self, requests: Sequence[_Request | Frame]) -> tuple[bytes, ...]:
        return await self._enqueue(requests)

    async def request_many_guarded(
        self,
        requests: Sequence[_Request | Frame],
        *,
        guard_before_last: Callable[[tuple[bytes, ...]], None],
    ) -> tuple[bytes, ...]:
        """Queue a sequence and run a synchronous check before its last frame.

        The worker owns the port throughout the sequence, so no other queued
        status poll or command can interleave between the guard and final write.
        """
        if len(requests) < 2:
            raise ValueError("a guarded request sequence needs a guard response and final request")
        return await self._enqueue(requests, guard_before_last=guard_before_last)

    async def _enqueue(
        self,
        requests: Sequence[_Request | Frame],
        *,
        guard_before_last: Callable[[tuple[bytes, ...]], None] | None = None,
    ) -> tuple[bytes, ...]:
        if not self._started:
            raise RuntimeError("UPS serial service must be started before use")
        if self._closing:
            raise RuntimeError("UPS serial service is closing")
        if not requests:
            return ()

        normalized = tuple(
            request if isinstance(request, _Request) else _Request(request.index, request.command, request.data)
            for request in requests
        )
        loop = asyncio.get_running_loop()
        result: asyncio.Future[tuple[bytes, ...]] = loop.create_future()
        await self._queue.put(_Job(normalized, result, guard_before_last))
        return await result

    async def read_status_flags(self) -> StatusFlag:
        data = await self.request(INDEX_GENERIC, CMD_STATUS_FLAGS)
        return parse_status_flags(data)

    async def get_parameter(self, parameter_id: int) -> Parameter:
        data = await self.request(INDEX_PARAMETER, parameter_id)
        return parse_parameter(data, parameter_id)

    async def set_parameter_verified(
        self,
        parameter_id: int,
        *,
        enabled: bool,
        value: int,
    ) -> Parameter:
        set_request = parameter_set_frame(parameter_id, enabled, value)
        get_request = _Request(INDEX_PARAMETER, parameter_id)
        _, response = await self.request_many((set_request, get_request))
        current = parse_parameter(response, parameter_id)
        matches = current.enabled == enabled and (not enabled or current.value == value)
        if not matches:
            raise UpsParameterMismatch(
                f"parameter 0x{parameter_id:02x} read-back does not match requested value"
            )
        return current

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            if job is None:
                return
            if job.result.cancelled():
                continue
            try:
                result = await asyncio.to_thread(
                    self._execute_job, job.requests, job.guard_before_last
                )
            except Exception as exc:
                if not job.result.done():
                    job.result.set_exception(self._copy_exception(exc))
            else:
                if not job.result.done():
                    job.result.set_result(result)

    @staticmethod
    def _copy_exception(exc: Exception) -> Exception:
        """Do not leak the worker coroutine's traceback through the request Future."""
        if isinstance(exc, UpsResponseTimeout):
            return UpsResponseTimeout(str(exc))
        if isinstance(exc, UpsTransportError):
            return type(exc)(str(exc))
        return UpsTransportError(str(exc))

    def _execute_job(
        self,
        requests: tuple[_Request, ...],
        guard_before_last: Callable[[tuple[bytes, ...]], None] | None = None,
    ) -> tuple[bytes, ...]:
        results: list[bytes] = []
        for index, request in enumerate(requests):
            if guard_before_last is not None and index == len(requests) - 1:
                guard_before_last(tuple(results))
            results.append(self._transact(request))
        return tuple(results)

    def _transact(self, request: _Request) -> bytes:
        frame = encode_frame(Frame(request.index, request.command, request.data))
        self._port.reset_input_buffer()
        written = self._port.write(frame)
        if written != len(frame):
            raise UpsTransportError(f"short serial write: {written} of {len(frame)} bytes")

        deadline = time.monotonic() + self._response_timeout
        decoder = FrameDecoder()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise UpsResponseTimeout(
                    f"timeout waiting for UPS response 0x{request.index:02x}/0x{request.command:02x}"
                )
            chunk = self._port.read(self._read_chunk_size)
            if not chunk:
                continue
            for response in decoder.feed(chunk):
                if response.index == request.index and response.command == request.command:
                    return response.data


class PySerialPort:
    """Lazy pyserial adapter; import pyserial only when opening real hardware."""

    def __init__(self, serial_port: object) -> None:
        self._serial = serial_port

    @classmethod
    def open(
        cls,
        device: str,
        *,
        baudrate: int = 38400,
        read_timeout: float = 0.05,
    ) -> PySerialPort:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("pyserial is required for a real UPS serial connection") from exc

        serial_port = serial.Serial(
            port=device,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=read_timeout,
            write_timeout=read_timeout,
        )
        return cls(serial_port)

    def read(self, size: int = 1) -> bytes:
        return self._serial.read(size)  # type: ignore[attr-defined,no-any-return]

    def write(self, data: bytes) -> int:
        return self._serial.write(data)  # type: ignore[attr-defined,no-any-return]

    def reset_input_buffer(self) -> None:
        self._serial.reset_input_buffer()  # type: ignore[attr-defined]

    def close(self) -> None:
        self._serial.close()  # type: ignore[attr-defined]