import struct
import unittest

from remotevoice.protocol import (
    FRAME_SAMPLES,
    MIC_FRAME,
    SYSTEM_FRAME,
    ProtocolError,
    decode_audio_frame,
    decode_control,
    encode_audio_frame,
    encode_control,
)


class ProtocolTests(unittest.TestCase):
    def test_control_round_trip(self):
        control = {"type": "mic_enabled", "enabled": True}
        self.assertEqual(decode_control(encode_control(control)), control)

    def test_audio_frame_round_trip_for_both_directions(self):
        mic = bytes(FRAME_SAMPLES * 4)
        system = bytes(FRAME_SAMPLES * 2 * 4)
        self.assertEqual(decode_audio_frame(encode_audio_frame(MIC_FRAME, mic)), (MIC_FRAME, memoryview(mic)))
        self.assertEqual(
            decode_audio_frame(encode_audio_frame(SYSTEM_FRAME, system)),
            (SYSTEM_FRAME, memoryview(system)),
        )

    def test_rejects_invalid_audio_frames(self):
        for frame in (b"", b"\x03" + bytes(4), b"\x01" + bytes(8)):
            with self.subTest(frame_length=len(frame)), self.assertRaises(ProtocolError):
                decode_audio_frame(frame)

    def test_rejects_malformed_control_message(self):
        with self.assertRaises(ProtocolError):
            decode_control("[]")
        with self.assertRaises(ProtocolError):
            decode_control("not-json")

    def test_rejects_nonfinite_samples_in_both_directions(self):
        for frame_type, channels in ((MIC_FRAME, 1), (SYSTEM_FRAME, 2)):
            for value in (float("nan"), float("inf"), float("-inf")):
                for sample_index in (0, FRAME_SAMPLES * channels // 2, FRAME_SAMPLES * channels - 1):
                    with self.subTest(frame_type=frame_type, value=value, sample_index=sample_index):
                        payload = bytearray(FRAME_SAMPLES * channels * 4)
                        struct.pack_into("<f", payload, sample_index * 4, value)
                        with self.assertRaisesRegex(ProtocolError, "finite"):
                            decode_audio_frame(bytes((frame_type,)) + payload)
                        with self.assertRaisesRegex(ProtocolError, "finite"):
                            encode_audio_frame(frame_type, payload)

    def test_finite_headroom_round_trip_does_not_clip_or_normalize(self):
        for frame_type, channels in ((MIC_FRAME, 1), (SYSTEM_FRAME, 2)):
            with self.subTest(frame_type=frame_type):
                payload = struct.pack("<ff", -2.0, 2.0) * (FRAME_SAMPLES * channels // 2)
                packet = encode_audio_frame(frame_type, payload)
                self.assertEqual(decode_audio_frame(packet), (frame_type, memoryview(payload)))


if __name__ == "__main__":
    unittest.main()
