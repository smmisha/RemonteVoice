import math
import asyncio
import random
import struct
import unittest
import zlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import numpy as np

from remontevoice.audio_compression import encode_system_frame
from remontevoice.audio import AudioBridge
from remontevoice.protocol import ProtocolError


class LosslessAudioTests(unittest.TestCase):
    def test_float_bits_survive_lane_shuffle_and_inflation(self):
        rng=random.Random(266)
        for values in ([0.,-0.,1e-40,-1e-40,3.4e38,-3.4e38]*160,
                       [math.sin(i/11)*1.25 for i in range(960)],
                       [rng.uniform(-1,1) for _ in range(960)]):
            pcm=struct.pack('<960f',*values)
            packet=encode_system_frame(pcm,compressed=True)
            self.assertLessEqual(len(packet),3841)
            if packet[0]==3:
                decoder=zlib.decompressobj(wbits=-15)
                lanes=decoder.decompress(packet[1:],3841)
                self.assertTrue(decoder.eof);self.assertFalse(decoder.unused_data)
                self.assertEqual(len(lanes),3840)
                restored=bytearray(3840)
                for b in range(4):restored[b::4]=lanes[b*960:(b+1)*960]
                self.assertEqual(bytes(restored),pcm)
            else:self.assertEqual(packet,b'\x02'+pcm)

    def test_raw_is_the_default_and_compressed_silence_is_smaller(self):
        pcm=bytes(3840)
        self.assertEqual(encode_system_frame(pcm),b'\x02'+pcm)
        packet=encode_system_frame(pcm,compressed=True)
        self.assertEqual(packet[0],3);self.assertLess(len(packet),100)

    def test_invalid_lengths_and_nonfinite_values_remain_rejected(self):
        for payload in (bytes(3839),bytes(3844),struct.pack('<f',float('nan'))*960):
            with self.assertRaises(ProtocolError):encode_system_frame(payload,compressed=True)

    def test_capture_uses_the_negotiated_encoder_without_enabling_microphone(self):
        socket=SimpleNamespace(send=AsyncMock())
        bridge=AudioBridge('inert','inert',MagicMock(),socket,asyncio.Lock(),MagicMock())
        bridge.enable_system_compression()
        completed=MagicMock()
        completed.result.side_effect=lambda timeout:bridge._stop.set()
        def dispatch(coroutine,loop):
            asyncio.run(coroutine);return completed
        with patch('remontevoice.audio.sc.get_microphone') as source, patch(
                'remontevoice.audio.asyncio.run_coroutine_threadsafe',side_effect=dispatch):
            source.return_value.recorder.return_value.__enter__.return_value.record.return_value=np.zeros((480,2),dtype='<f4')
            bridge._capture_system_audio()
        packet=socket.send.await_args.args[0]
        self.assertEqual(packet[0],3);self.assertEqual(zlib.decompress(packet[1:],wbits=-15),bytes(3840))
        self.assertFalse(bridge.microphone_enabled)
        bridge.stop()
        with self.assertRaises(RuntimeError):bridge.enable_system_compression()
