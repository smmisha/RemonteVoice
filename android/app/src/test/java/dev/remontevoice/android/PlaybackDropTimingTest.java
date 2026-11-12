package dev.remontevoice.android;

import static org.junit.Assert.*;

import org.junit.Test;

public final class PlaybackDropTimingTest {
    @Test public void reportsNoEventBeforeTheFirstDrop() {
        PlaybackDropTiming timing = new PlaybackDropTiming();
        assertEquals(" first_drop_observed=0", timing.fields());
    }

    @Test public void keepsTheFirstDropAndItsPrecedingPlaybackSnapshot() {
        PlaybackDropTiming timing = new PlaybackDropTiming();
        timing.capture(1_250_000_000L, 250_000_000L, 80, 3_000_000L,
                140_000_000L, 1_200_000_000L, 120_000, 52_000, 7_200,
                52_000_000L, 1_100_000_000L);
        timing.capture(2_000_000_000L, 250_000_000L, 81, 0, 0, 0, 0, 0, 0, 0, 0);
        String fields = timing.fields();
        assertTrue(fields.contains("first_drop_observed=1 first_drop_at_ns=1250000000"));
        assertTrue(fields.contains("first_drop_uptime_ms=1000 first_drop_offer=80"));
        assertTrue(fields.contains("first_drop_gap_ms=3 first_drop_max_gap_ms=140"));
        assertTrue(fields.contains("first_drop_max_gap_at_ns=1200000000"));
        assertTrue(fields.contains("first_drop_written_samples=120000 first_drop_played_frames=52000"));
        assertTrue(fields.contains("first_drop_native_queued_frames=7200 first_drop_max_write_ms=52"));
        assertTrue(fields.contains("first_drop_max_write_at_ns=1100000000"));
    }

    @Test public void keepsABoundedOrderedWindowAroundTheFirstOverflow() {
        PlaybackDropTiming timing = new PlaybackDropTiming();
        for (int i = 1; i <= 40; i++) {
            timing.sample(i * 10_000_000L, i, i % 25, i * 960L, i * 480L, 7200);
        }
        timing.capture(400_000_000L, 0, 40, 10_000_000L,
                120_000_000L, 300_000_000L, 38400, 19200, 7200, 0, 0);
        for (int i = 41; i <= 60; i++) {
            timing.sample(i * 10_000_000L, i, i % 25, i * 960L, i * 480L, 7200);
        }
        assertFalse(timing.wantsSample());
        String window = timing.fields().split(" drop_window=")[1];
        String[] entries = window.split(",");
        assertEquals(48, entries.length);
        assertEquals("-310:9:9:8640:4320:7200", entries[0]);
        assertEquals("0:40:15:38400:19200:7200", entries[31]);
        assertEquals("10:41:16:39360:19680:7200", entries[32]);
        assertEquals("160:56:6:53760:26880:7200", entries[47]);
    }
}
