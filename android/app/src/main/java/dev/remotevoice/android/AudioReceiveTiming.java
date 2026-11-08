package dev.remotevoice.android;

import java.util.function.LongSupplier;

/** Debug scalar observations at the WebSocket callback boundary; never accepts audio. */
final class AudioReceiveTiming {
    private final LongSupplier clock;
    private final ReaderThreadTiming reader;
    private final Object publicationLock = new Object();
    private volatile boolean frozen;
    static final long GAP_EVENT_NS = 120_000_000L;
    private long packets, previousEntry, previousCallback, entry, gap, lastReport;
    private volatile long gapEvents;
    private boolean entered, reported;
    private ReaderThreadTiming.Interval readerInterval;
    private volatile Maximum maximum;

    AudioReceiveTiming(LongSupplier clock) { this(clock, null); }

    AudioReceiveTiming(LongSupplier clock, ReaderThreadTiming reader) {
        this.clock = clock; this.reader = reader;
    }

    void begin() {
        long now = clock.getAsLong();
        gap = entered ? Math.max(0, now - previousEntry) : 0;
        entry = now;
        previousEntry = now;
        entered = true;
        packets++;
        readerInterval = reader == null ? null : reader.observe();
        Maximum previousMaximum = maximum;
        Maximum candidate = previousMaximum == null || gap > previousMaximum.gap
                ? new Maximum(packets, entry, gap, readerInterval) : null;
        if (candidate != null) {
            synchronized (publicationLock) {
                if (!frozen && (maximum == null || candidate.gap > maximum.gap)) maximum = candidate;
            }
        }
    }

    // Only publication is serialized. Closing must never wait for a reader probe.
    void freezeMaximum() {
        synchronized (publicationLock) { frozen = true; }
    }

    long packet() { return packets; }
    long entryNanos() { return entry; }

    String maximum() {
        Maximum value = maximum;
        return value == null ? null : "audio_reader_maximum packet=" + value.packet
                + " clock_ns=" + value.at + " gap_ns=" + value.gap
                + " gap_events=" + gapEvents
                + (value.interval == null ? " reader_valid=0" : value.interval.fields());
    }

    private static final class Maximum {
        final long packet, at, gap;
        final ReaderThreadTiming.Interval interval;
        Maximum(long packet, long at, long gap, ReaderThreadTiming.Interval interval) {
            this.packet = packet; this.at = at; this.gap = gap; this.interval = interval;
        }
    }

    String finish() {
        long end = clock.getAsLong();
        long callback = Math.max(0, end - entry);
        long previous = previousCallback;
        previousCallback = callback;
        boolean gapEvent = gap >= GAP_EVENT_NS;
        if (!gapEvent && callback < 30_000_000L) return null;
        // Arrival gaps of 120 ms or more are always reported and numbered, so a reader of the
        // log can verify that none was lost. Only slow-callback-only events stay rate-limited.
        // After closing the count is frozen with the maximum; a late event is marked gap_seq=0.
        if (!gapEvent && reported && end - lastReport < 1_000_000_000L) return null;
        reported = true;
        lastReport = end;
        return "audio_receive_timing packet=" + packets + " clock_ns=" + entry
                + " gap_ms=" + gap / 1_000_000L + " callback_ms=" + callback / 1_000_000L
                + " previous_callback_ms=" + previous / 1_000_000L
                + (gapEvent ? " gap_seq=" + (frozen ? 0 : ++gapEvents) : "")
                + (readerInterval == null ? "" : readerInterval.fields());
    }
}
