package dev.remotevoice.android;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertThrows;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import org.junit.Test;

public final class AudioProtocolTest {
    @Test public void microphonePacketMatchesWindowsLittleEndianFormat() {
        float[] samples = new float[480];
        samples[0] = 0.5f;
        samples[1] = -0.5f;
        samples[2] = 2.0f;
        samples[3] = -2.0f;
        byte[] packet = AudioProtocol.encodeMicrophone(samples);
        assertEquals(1921, packet.length);
        assertArrayEquals(new byte[] {1, 0, 0, 0, 63, 0, 0, 0, -65},
                java.util.Arrays.copyOf(packet, 9));
        ByteBuffer payload = ByteBuffer.wrap(packet).order(ByteOrder.LITTLE_ENDIAN);
        payload.get();
        assertEquals(0.5f, payload.getFloat(), 0.0f);
        assertEquals(-0.5f, payload.getFloat(), 0.0f);
        assertEquals(1.0f, payload.getFloat(), 0.0f);
        assertEquals(-1.0f, payload.getFloat(), 0.0f);
        assertEquals(2.0f, samples[2], 0.0f);
    }

    @Test public void decodesStereoInterleavedSystemSamplesWithoutChangingOrder() {
        ByteBuffer packet = ByteBuffer.allocate(3841).order(ByteOrder.LITTLE_ENDIAN);
        packet.put((byte) 2);
        float[] expected = new float[960];
        for (int i = 0; i < expected.length; i++) {
            expected[i] = (i % 11 - 5) / 5.0f;
            packet.putFloat(expected[i]);
        }
        assertArrayEquals(expected, AudioProtocol.decodeSystem(packet.array()), 0.0f);
        assertEquals(48000, AudioProtocol.SAMPLE_RATE);
        assertEquals(480, AudioProtocol.FRAME_SAMPLES);
        assertEquals(2, AudioProtocol.SYSTEM_CHANNELS);
    }

    @Test public void rejectsWrongPacketTypesAndLengths() {
        for (int count : new int[] {0, 1, 1921, 3840, 3842, 16000}) {
            assertThrows(IllegalArgumentException.class,
                    () -> AudioProtocol.decodeSystem(new byte[count]));
        }
        assertThrows(IllegalArgumentException.class, () -> AudioProtocol.decodeSystem(null));
        assertThrows(IllegalArgumentException.class, () -> AudioProtocol.decodeSystem(new byte[3841]));
        assertThrows(IllegalArgumentException.class, () -> AudioProtocol.encodeMicrophone(null));
        assertThrows(IllegalArgumentException.class, () -> AudioProtocol.encodeMicrophone(new float[479]));
        assertThrows(IllegalArgumentException.class, () -> AudioProtocol.encodeMicrophone(new float[481]));
    }

    @Test public void rejectsNonFiniteSamplesInBothDirections() {
        for (float invalid : new float[] {Float.NaN, Float.POSITIVE_INFINITY, Float.NEGATIVE_INFINITY}) {
            float[] microphone = new float[480];
            microphone[479] = invalid;
            assertThrows(IllegalArgumentException.class, () -> AudioProtocol.encodeMicrophone(microphone));
            ByteBuffer packet = ByteBuffer.allocate(3841).order(ByteOrder.LITTLE_ENDIAN);
            packet.put((byte) 2);
            packet.putFloat(3837, invalid);
            assertThrows(IllegalArgumentException.class, () -> AudioProtocol.decodeSystem(packet.array()));
        }
    }
}
