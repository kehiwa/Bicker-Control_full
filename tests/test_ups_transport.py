import asyncio
import queue
import threading
import unittest
from collections.abc import Callable

from bicker_control.ups.protocol import (
    CMD_STATUS_FLAGS,
    EOT,
    INDEX_GENERIC,
    INDEX_PARAMETER,
    PARAM_MAX_BACKUP_TIME,
    Frame,
    encode_frame,
)
from bicker_control.ups.transport import (
    UpsParameterMismatch,
    UpsResponseTimeout,
    UpsSerialService,
)


Responder = Callable[[bytes], list[bytes]]


class FakePort:
    def __init__(self, responder: Responder) -> None:
        self._responder = responder
        self._incoming: queue.Queue[bytes] = queue.Queue()
        self._pending = bytearray()
        self._lock = threading.Lock()
        self.writes: list[bytes] = []
        self.closed = False

    def read(self, size: int = 1) -> bytes:
        if not self._pending:
            try:
                self._pending.extend(self._incoming.get(timeout=0.005))
            except queue.Empty:
                return b""
        result = bytes(self._pending[:size])
        del self._pending[:size]
        return result

    def write(self, data: bytes) -> int:
        with self._lock:
            self.writes.append(bytes(data))
        for response in self._responder(bytes(data)):
            self._incoming.put(response)
        return len(data)

    def reset_input_buffer(self) -> None:
        self._pending.clear()
        while True:
            try:
                self._incoming.get_nowait()
            except queue.Empty:
                break

    def close(self) -> None:
        self.closed = True


def request_parts(frame: bytes) -> tuple[int, int]:
    return frame[2], frame[3]


def parameter_data(enabled: bool, value: int) -> bytes:
    return bytes((PARAM_MAX_BACKUP_TIME, 1, 0, 0xFF, 0xFF, 60, 0, int(enabled))) + value.to_bytes(2, "little")


class UpsSerialServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_requests_are_serialized_and_match_responses(self) -> None:
        def responder(request: bytes) -> list[bytes]:
            index, command = request_parts(request)
            if command == 0x61:
                return [encode_frame(Frame(index, 0x7F, b"foreign")), encode_frame(Frame(index, command, b"first"))]
            return [encode_frame(Frame(index, command, b"second"))]

        port = FakePort(responder)
        service = UpsSerialService(port, response_timeout=0.2, read_chunk_size=3)
        await service.start()
        try:
            first = asyncio.create_task(service.request(INDEX_GENERIC, 0x61))
            second = asyncio.create_task(service.request(INDEX_GENERIC, 0x62))
            self.assertEqual(await asyncio.gather(first, second), [b"first", b"second"])
            self.assertEqual([request_parts(frame)[1] for frame in port.writes], [0x61, 0x62])
        finally:
            await service.close()
        self.assertTrue(port.closed)

    async def test_set_and_readback_are_one_atomic_queue_job(self) -> None:
        def responder(request: bytes) -> list[bytes]:
            index, command = request_parts(request)
            if index == INDEX_PARAMETER and command == PARAM_MAX_BACKUP_TIME:
                if request[1] == 6:
                    return [encode_frame(Frame(index, command))]
                return [encode_frame(Frame(index, command, parameter_data(True, 5)))]
            return [encode_frame(Frame(index, command, b"ok"))]

        port = FakePort(responder)
        service = UpsSerialService(port, response_timeout=0.2)
        await service.start()
        try:
            setting = asyncio.create_task(
                service.set_parameter_verified(PARAM_MAX_BACKUP_TIME, enabled=True, value=5)
            )
            status = asyncio.create_task(service.request(INDEX_GENERIC, CMD_STATUS_FLAGS))
            parameter, status_data = await asyncio.gather(setting, status)
            self.assertTrue(parameter.enabled)
            self.assertEqual(parameter.value, 5)
            self.assertEqual(status_data, b"ok")
            self.assertEqual(
                [request_parts(frame) for frame in port.writes],
                [
                    (INDEX_PARAMETER, PARAM_MAX_BACKUP_TIME),
                    (INDEX_PARAMETER, PARAM_MAX_BACKUP_TIME),
                    (INDEX_GENERIC, CMD_STATUS_FLAGS),
                ],
            )
        finally:
            await service.close()

    async def test_set_readback_mismatch_raises(self) -> None:
        def responder(request: bytes) -> list[bytes]:
            index, command = request_parts(request)
            if request[1] == 6:
                return [encode_frame(Frame(index, command))]
            return [encode_frame(Frame(index, command, parameter_data(True, 4)))]

        service = UpsSerialService(FakePort(responder), response_timeout=0.2)
        await service.start()
        try:
            with self.assertRaises(UpsParameterMismatch):
                await service.set_parameter_verified(PARAM_MAX_BACKUP_TIME, enabled=True, value=5)
        finally:
            await service.close()

    async def test_response_timeout_is_reported(self) -> None:
        service = UpsSerialService(FakePort(lambda request: []), response_timeout=0.03)
        await service.start()
        try:
            with self.assertRaises(UpsResponseTimeout):
                await service.request(INDEX_GENERIC, CMD_STATUS_FLAGS)
        finally:
            await service.close()


if __name__ == "__main__":
    unittest.main()