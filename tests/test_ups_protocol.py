import unittest

from bicker_control.ups.protocol import (
    EOT,
    INDEX_GENERIC,
    INDEX_PARAMETER,
    MAX_DATA_LENGTH,
    PARAM_MAX_BACKUP_TIME,
    CMD_STATUS_FLAGS,
    Frame,
    FrameDecoder,
    Parameter,
    ProtocolError,
    StatusFlag,
    encode_frame,
    parameter_get_request,
    parameter_set_request,
    parse_parameter,
    parse_status_flags,
    status_request,
)


class FrameCodecTests(unittest.TestCase):
    def test_known_status_request(self) -> None:
        self.assertEqual(status_request(), bytes((0x01, 0x03, 0x01, 0x40, 0x04)))

    def test_parameter_requests_match_protocol_examples(self) -> None:
        self.assertEqual(
            parameter_get_request(PARAM_MAX_BACKUP_TIME),
            bytes((0x01, 0x03, INDEX_PARAMETER, PARAM_MAX_BACKUP_TIME, EOT)),
        )
        self.assertEqual(
            parameter_set_request(PARAM_MAX_BACKUP_TIME, True, 1),
            bytes((0x01, 0x06, INDEX_PARAMETER, PARAM_MAX_BACKUP_TIME, 1, 1, 0, EOT)),
        )

    def test_payload_may_contain_soh_and_eot(self) -> None:
        frame = Frame(0x01, 0x60, bytes((0x04, 0x01, 0x7F)))
        encoded = encode_frame(frame)
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(encoded), [frame])

    def test_decoder_handles_partial_input(self) -> None:
        frame = encode_frame(Frame(INDEX_GENERIC, CMD_STATUS_FLAGS, b"\x0c"))
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(frame[:2]), [])
        self.assertEqual(decoder.feed(frame[2:4]), [])
        self.assertEqual(decoder.feed(frame[4:]), [Frame(INDEX_GENERIC, CMD_STATUS_FLAGS, b"\x0c")])

    def test_decoder_discards_noise_and_resynchronizes(self) -> None:
        frame = encode_frame(Frame(INDEX_GENERIC, CMD_STATUS_FLAGS, b"\x04"))
        self.assertEqual(FrameDecoder().feed(b"\xff\x00" + frame), [Frame(INDEX_GENERIC, CMD_STATUS_FLAGS, b"\x04")])

    def test_invalid_size_and_terminator_do_not_block_next_frame(self) -> None:
        valid = encode_frame(Frame(INDEX_GENERIC, CMD_STATUS_FLAGS, b"\x0c"))
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(b"\x01\x02\x99" + b"\x01\x03\x01\x40\x00" + valid), [
            Frame(INDEX_GENERIC, CMD_STATUS_FLAGS, b"\x0c")
        ])

    def test_missing_terminator_preserves_next_frame_start(self) -> None:
        damaged_header = bytes((0x01, 0x03, INDEX_GENERIC, CMD_STATUS_FLAGS))
        valid = encode_frame(Frame(INDEX_GENERIC, CMD_STATUS_FLAGS, b"\x0c"))
        self.assertEqual(
            FrameDecoder().feed(damaged_header + valid),
            [Frame(INDEX_GENERIC, CMD_STATUS_FLAGS, b"\x0c")],
        )

    def test_maximum_payload_is_accepted(self) -> None:
        frame = Frame(0x01, 0x60, bytes(range(256))[:MAX_DATA_LENGTH])
        decoder = FrameDecoder()
        self.assertEqual(decoder.feed(encode_frame(frame)), [frame])

    def test_oversized_payload_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_frame(Frame(0x01, 0x60, bytes(MAX_DATA_LENGTH + 1)))


class ResponseParserTests(unittest.TestCase):
    def test_status_flags(self) -> None:
        flags = parse_status_flags(bytes((0x0D,)))
        self.assertEqual(flags, StatusFlag.CHARGING | StatusFlag.POWER_PRESENT | StatusFlag.BATTERY_PRESENT)

    def test_parameter_little_endian(self) -> None:
        data = bytes((
            PARAM_MAX_BACKUP_TIME,
            1, 0,
            0xFF, 0xFF,
            60, 0,
            1,
            1, 0,
        ))
        self.assertEqual(
            parse_parameter(data, PARAM_MAX_BACKUP_TIME),
            Parameter(PARAM_MAX_BACKUP_TIME, 1, 65535, 60, True, 1),
        )

    def test_parameter_id_must_match(self) -> None:
        data = bytes((1, 1, 0, 2, 0, 1, 0, 0, 1, 0))
        with self.assertRaises(ProtocolError):
            parse_parameter(data, PARAM_MAX_BACKUP_TIME)

    def test_parameter_enabled_field_must_be_boolean(self) -> None:
        data = bytes((2, 1, 0, 2, 0, 1, 0, 2, 1, 0))
        with self.assertRaises(ProtocolError):
            parse_parameter(data, PARAM_MAX_BACKUP_TIME)


if __name__ == "__main__":
    unittest.main()