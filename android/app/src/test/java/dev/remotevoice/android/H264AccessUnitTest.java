package dev.remotevoice.android;

import java.util.Arrays;
import org.junit.Test;
import static org.junit.Assert.*;

public final class H264AccessUnitTest {
    private static byte[] hex(String value) {
        byte[] data = new byte[value.length() / 2];
        for (int i = 0; i < data.length; ++i) data[i] = (byte)Integer.parseInt(value.substring(i*2, i*2+2), 16);
        return data;
    }
    private static byte[] frame() {
        return hex("0000000109f0000000016742c020da0140196c0440000003004000000f23c60ca8"
                + "00000168ce0fc8000001658888");
    }
    private static void reject(byte[] data, int width, int height, boolean initial) {
        try { H264AccessUnit.inspect(data, width, height, initial); fail("Invalid frame accepted"); }
        catch (IllegalArgumentException expected) { assertEquals("Invalid video frame", expected.getMessage()); }
    }
    @Test public void baselineParametersHaveExactDimensionsAndStartCodes() {
        H264AccessUnit.Parameters p = H264AccessUnit.inspect(frame(), 1280, 800, true);
        assertNotNull(p);
        assertEquals(0, p.sps[0]); assertEquals(1, p.sps[3]); assertEquals(0x67, p.sps[4]);
        assertArrayEquals(hex("0000000168ce0fc8"), p.pps);
    }
    @Test public void mismatchedOrOversizedDimensionsAreRejectedBeforeCodecCreation() {
        reject(frame(), 1920, 1200, true); reject(frame(), 1280, 801, true);
        reject(frame(), 4096, 2160, true); reject(frame(), -1, 800, true);
    }
    @Test public void dependentFrameRequiresPreviouslyConfiguredStream() {
        byte[] p = hex("00000109f0000001418888");
        assertNull(H264AccessUnit.inspect(p, 1280, 800, false));
        reject(p, 1280, 800, true);
    }
    @Test public void missingTruncatedOrNonBaselineParametersAreRejected() {
        reject(hex("00000109f0000001658888"), 1280, 800, true);
        byte[] high = frame(); high[11] = 100; reject(high, 1280, 800, true);
        for (int size = 0; size < frame().length - 3; ++size) {
            reject(Arrays.copyOf(frame(), size), 1280, 800, true);
        }
    }
    @Test public void byteLimitAndNalHeaderAreChecked() {
        reject(new byte[H264AccessUnit.MAX_BYTES + 1], 1280, 800, true);
        byte[] invalid = frame(); invalid[4] = (byte)0x89; reject(invalid, 1280, 800, true);
        reject(hex("00000109f0"), 1280, 800, false);
    }
    @Test public void codecConfigurationIsNotRepeatedInQueuedVideoPayload() {
        byte[] f = frame(); H264AccessUnit.inspect(f, 1280, 800, true);
        assertArrayEquals(hex("000001658888"), H264AccessUnit.videoPayload(f));
        byte[] twice = new byte[f.length * 2];
        System.arraycopy(f, 0, twice, 0, f.length);System.arraycopy(f, 0, twice, f.length, f.length);
        reject(twice, 1280, 800, true);
    }
}
