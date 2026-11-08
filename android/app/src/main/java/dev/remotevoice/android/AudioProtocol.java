package dev.remotevoice.android;

import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.util.zip.DataFormatException;
import java.util.zip.Inflater;

/** Ten millisecond, little-endian float32 frames shared with the Windows host. */
public final class AudioProtocol {
    public static final int SAMPLE_RATE = 48000;
    public static final int FRAME_SAMPLES = 480;
    public static final int SYSTEM_CHANNELS = 2;

    private AudioProtocol() {}

    public static byte[] encodeMicrophone(float[] samples) {
        if (samples == null || samples.length != FRAME_SAMPLES) {
            throw invalid();
        }
        ByteBuffer packet = ByteBuffer.allocate(1 + FRAME_SAMPLES * Float.BYTES)
                .order(ByteOrder.LITTLE_ENDIAN);
        packet.put((byte) 1);
        for (float sample : samples) {
            if (!Float.isFinite(sample)) {
                throw invalid();
            }
            packet.putFloat(Math.max(-1.0f, Math.min(1.0f, sample)));
        }
        return packet.array();
    }

    public static float[] decodeSystem(byte[] packet) {
        int count = FRAME_SAMPLES * SYSTEM_CHANNELS;
        if (packet != null && packet.length > 1 && packet.length <= 3841 && packet[0] == 3) {
            byte[] lanes = new byte[count * Float.BYTES];
            Inflater inflater = new Inflater(true);
            try {
                inflater.setInput(packet, 1, packet.length - 1);
                int size = 0;
                while (size < lanes.length) {
                    int written = inflater.inflate(lanes, size, lanes.length - size);
                    if (written == 0) break;
                    size += written;
                }
                if (size != lanes.length || !inflater.finished() || inflater.getRemaining() != 0) throw invalid();
                float[] samples = new float[count];
                for (int i = 0; i < count; i++) {
                    int bits = (lanes[i] & 255) | ((lanes[count+i] & 255) << 8)
                            | ((lanes[count*2+i] & 255) << 16) | ((lanes[count*3+i] & 255) << 24);
                    samples[i] = Float.intBitsToFloat(bits);
                    if (!Float.isFinite(samples[i])) throw invalid();
                }
                return samples;
            } catch (DataFormatException error) {
                throw invalid();
            } finally { inflater.end(); }
        }
        if (packet == null || packet.length != 1 + count * Float.BYTES || packet[0] != 2) {
            throw invalid();
        }
        ByteBuffer payload = ByteBuffer.wrap(packet).order(ByteOrder.LITTLE_ENDIAN);
        payload.get();
        float[] samples = new float[count];
        for (int i = 0; i < count; i++) {
            samples[i] = payload.getFloat();
            if (!Float.isFinite(samples[i])) {
                throw invalid();
            }
        }
        return samples;
    }

    private static IllegalArgumentException invalid() {
        return new IllegalArgumentException("Получен аудиокадр неподдерживаемого формата.");
    }
}
