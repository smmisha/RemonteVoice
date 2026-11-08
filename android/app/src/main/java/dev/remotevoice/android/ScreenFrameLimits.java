package dev.remotevoice.android;

/** Strict JPEG header bounds checked before any Android bitmap allocation. */
final class ScreenFrameLimits {
    static final int MAX_BYTES = 256 * 1024;
    static final int MAX_WIDTH = 1920;
    static final int MAX_HEIGHT = 1200;

    private ScreenFrameLimits() {}

    static int[] dimensions(byte[] jpeg) {
        if (jpeg == null || jpeg.length < 12 || jpeg.length > MAX_BYTES
                || u(jpeg[0]) != 255 || u(jpeg[1]) != 216
                || u(jpeg[jpeg.length - 2]) != 255 || u(jpeg[jpeg.length - 1]) != 217) throw invalid();
        int position = 2;
        while (position + 3 < jpeg.length) {
            if (u(jpeg[position++]) != 255) throw invalid();
            while (position < jpeg.length && u(jpeg[position]) == 255) position++;
            if (position + 2 >= jpeg.length) throw invalid();
            int marker = u(jpeg[position++]);
            if (marker == 0 || marker == 216 || marker == 217 || marker == 218
                    || marker == 1 || (marker >= 208 && marker <= 215)) throw invalid();
            int length = (u(jpeg[position]) << 8) | u(jpeg[position + 1]);
            if (length < 2 || length > jpeg.length - position) throw invalid();
            if (marker == 192 || marker == 193 || marker == 194) {
                if (length < 8 || u(jpeg[position + 2]) != 8) throw invalid();
                int height = (u(jpeg[position + 3]) << 8) | u(jpeg[position + 4]);
                int width = (u(jpeg[position + 5]) << 8) | u(jpeg[position + 6]);
                int channels = u(jpeg[position + 7]);
                if ((channels != 1 && channels != 3) || length != 8 + 3 * channels
                        || width < 1 || height < 1 || width > MAX_WIDTH || height > MAX_HEIGHT
                        || (long) width * height > 2304000) throw invalid();
                return new int[] {width, height};
            }
            position += length;
        }
        throw invalid();
    }

    private static int u(byte value) { return value & 255; }
    private static IllegalArgumentException invalid() {
        return new IllegalArgumentException("Некорректный кадр экрана.");
    }
}
