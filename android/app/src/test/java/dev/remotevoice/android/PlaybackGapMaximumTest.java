package dev.remotevoice.android;

import static org.junit.Assert.*;
import org.junit.Test;

public final class PlaybackGapMaximumTest {
    @Test public void retainsExactTupleAcrossLargerAndSmallerGaps() {
        PlaybackGapMaximum maximum = new PlaybackGapMaximum();
        maximum.record(58078, 100_000_000L, 10_000_000L);
        maximum.record(58079, 465_000_000L, 365_000_000L);
        PlaybackGapMaximum.Snapshot peak = maximum.snapshot();
        maximum.record(58080, 475_000_000L, 10_000_000L);
        assertSame(peak, maximum.snapshot());
        assertEquals(58079, peak.packet);
        assertEquals(58078, peak.previousPacket);
        assertEquals(465_000_000L, peak.atNanos);
        assertEquals(365_000_000L, peak.gapNanos);
    }

    @Test public void recordsNewPeakWithinOneSecondWithoutLoggingIt() {
        PlaybackGapMaximum maximum = new PlaybackGapMaximum();
        maximum.record(1, 0, 0);
        maximum.record(2, 150_000_000L, 150_000_000L);
        maximum.record(3, 515_000_000L, 365_000_000L);
        assertEquals(3, maximum.snapshot().packet);
        assertEquals(2, maximum.snapshot().previousPacket);
        assertEquals(365_000_000L, maximum.snapshot().gapNanos);
    }

    @Test public void firstEqualMaximumWinsAndFreezeIgnoresLateOffers() {
        PlaybackGapMaximum maximum = new PlaybackGapMaximum();
        maximum.record(1, -100, 0);
        maximum.record(2, -50, 50);
        maximum.record(3, 0, 50);
        PlaybackGapMaximum.Snapshot frozen = maximum.freeze();
        maximum.record(4, 1000, 1000);
        assertSame(frozen, maximum.freeze());
        assertEquals(2, frozen.packet);
        assertEquals(-50, frozen.atNanos);
        assertEquals(0, new PlaybackGapMaximum().snapshot().gapNanos);
    }

    @Test public void doesNotInventPacketOrdinalsForLegacyOffers() {
        PlaybackGapMaximum maximum = new PlaybackGapMaximum();
        maximum.record(0, 10, 10);
        maximum.record(-1, 100, 90);
        maximum.record(5, 200, -1);
        assertEquals(" session_max_offer_gap_ns=10 session_max_offer_gap_packet=0"
                + " session_max_offer_gap_previous_packet=0 session_max_offer_gap_at_ns=10",
                maximum.snapshot().fields());
    }
}
