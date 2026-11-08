package dev.remotevoice.android;

import java.util.Arrays;
import java.io.ByteArrayOutputStream;

/** Bounded Annex-B baseline frames. Validate dimensions before configuring MediaCodec. */
final class H264AccessUnit {
    static final int MAX_BYTES = 512 * 1024;
    private H264AccessUnit() {}

    static final class Parameters {
        final byte[] sps, pps;
        Parameters(byte[] sps, byte[] pps) { this.sps = sps; this.pps = pps; }
    }

    static Parameters inspect(byte[] data, int width, int height, boolean initial) {
        if (data == null || data.length < 8 || data.length > MAX_BYTES
                || width < 16 || height < 16 || width > 1920 || height > 1200
                || (width & 1) != 0 || (height & 1) != 0) throw invalid();
        byte[] sps = null, pps = null;
        boolean vcl = false, idr = false;
        int start = 0, ordinal = 0;
        while (start < data.length) {
            int prefix = prefix(data, start);
            if (prefix == 0 || start + prefix >= data.length) throw invalid();
            int nal = start + prefix, end = nal + 1;
            while (end < data.length && prefix(data, end) == 0) ++end;
            if ((data[nal] & 128) != 0) throw invalid();
            int type = data[nal] & 31;
            if ((ordinal == 0 && type != 9) || (ordinal > 0 && type == 9)) throw invalid();
            ++ordinal;
            if (type == 1 || type == 5) vcl = true;
            if (type == 5) idr = true;
            if (type == 7 || type == 8) {
                if (end - nal > 256 || end - nal < 2) throw invalid();
                byte[] unit = Arrays.copyOfRange(data, nal, end);
                byte[] csd = new byte[4 + unit.length];
                csd[3] = 1;
                System.arraycopy(unit, 0, csd, 4, unit.length);
                if (type == 7) {
                    validateSps(unit, width, height);
                    sps = csd;
                } else pps = csd;
            } else if (type != 1 && type != 5 && type != 6 && type != 9 && type != 12) throw invalid();
            start = end;
        }
        if (!vcl || (initial && (!idr || sps == null || pps == null))
                || (sps == null) != (pps == null)) throw invalid();
        return sps == null ? null : new Parameters(sps, pps);
    }

    static byte[] videoPayload(byte[] validated) {
        ByteArrayOutputStream output = new ByteArrayOutputStream(validated.length);
        int start = 0;
        while (start < validated.length) {
            int nal = start + prefix(validated, start), end = nal + 1;
            while (end < validated.length && prefix(validated, end) == 0) ++end;
            int type = validated[nal] & 31;
            // CSD is supplied once through MediaFormat. Keep only SEI/VCL/filler
            // here, rather than submitting SPS/PPS again as codec-config data.
            if (type != 7 && type != 8 && type != 9) output.write(validated, start, end - start);
            start = end;
        }
        return output.toByteArray();
    }

    private static int prefix(byte[] data, int offset) {
        if (offset + 3 <= data.length && data[offset] == 0 && data[offset + 1] == 0) {
            if (data[offset + 2] == 1) return 3;
            if (offset + 4 <= data.length && data[offset + 2] == 0 && data[offset + 3] == 1) return 4;
        }
        return 0;
    }

    private static void validateSps(byte[] nal, int expectedWidth, int expectedHeight) {
        byte[] rbsp = new byte[nal.length - 1];
        int count = 0, zeros = 0;
        for (int i = 1; i < nal.length; ++i) {
            int value = nal[i] & 255;
            if (zeros >= 2 && value == 3) {
                if (i + 1 == nal.length || (nal[i + 1] & 255) > 3) throw invalid();
                zeros = 0;
                continue;
            }
            rbsp[count++] = nal[i];
            zeros = value == 0 ? zeros + 1 : 0;
        }
        Bits bits = new Bits(rbsp, count);
        if (bits.read(8) != 66) throw invalid(); // Only the negotiated baseline encoder profile.
        bits.read(8);
        if (bits.read(8) > 42 || bits.ue() > 31 || bits.ue() > 12) throw invalid();
        int poc = bits.ue();
        if (poc == 0) {
            if (bits.ue() > 12) throw invalid();
        } else if (poc == 1) {
            bits.read(1); bits.ue(); bits.ue();
            int cycle = bits.ue();
            if (cycle > 256) throw invalid();
            for (int i = 0; i < cycle; ++i) bits.ue();
        } else if (poc != 2) throw invalid();
        if (bits.ue() > 16) throw invalid();
        bits.read(1);
        int macroWidth = bits.ue() + 1, macroHeight = bits.ue() + 1;
        if (macroWidth > 120 || macroHeight > 75 || bits.read(1) != 1) throw invalid();
        bits.read(1);
        int left = 0, right = 0, top = 0, bottom = 0;
        if (bits.read(1) != 0) {
            left = bits.ue(); right = bits.ue(); top = bits.ue(); bottom = bits.ue();
            if (left > 960 || right > 960 || top > 600 || bottom > 600) throw invalid();
        }
        if (macroWidth * 16 - (left + right) * 2 != expectedWidth
                || macroHeight * 16 - (top + bottom) * 2 != expectedHeight) throw invalid();
    }

    private static IllegalArgumentException invalid() { return new IllegalArgumentException("Invalid video frame"); }

    private static final class Bits {
        private final byte[] data;
        private final int limit;
        private int position;
        Bits(byte[] data, int length) { this.data = data; this.limit = length * 8; }
        int read(int count) {
            if (count < 0 || count > 30 || position > limit - count) throw invalid();
            int value = 0;
            while (count-- > 0) {
                value = (value << 1) | ((data[position >> 3] >> (7 - (position & 7))) & 1);
                ++position;
            }
            return value;
        }
        int ue() {
            int zeros = 0;
            while (read(1) == 0) if (++zeros > 20) throw invalid();
            return ((1 << zeros) - 1) + read(zeros);
        }
    }
}
