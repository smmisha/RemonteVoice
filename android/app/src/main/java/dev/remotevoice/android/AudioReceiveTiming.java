package dev.remotevoice.android;

import java.util.function.LongSupplier;

/** Debug scalar observations at the WebSocket callback boundary; never accepts audio. */
final class AudioReceiveTiming {
    private final LongSupplier clock;
    private final ReaderThreadTiming reader;
    private final Object publicationLock = new Object();
    private boolean frozen;
    private long packets, previousEntry, previousCallback, entry, gap, lastReport;
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
        if (gap < 120_000_000L && callback < 30_000_000L) return null;
        if (reported && end - lastReport < 1_000_000_000L) return null;
        reported = true;
        lastReport = end;
        return "audio_receive_timing packet=" + packets + " clock_ns=" + entry
                + " gap_ms=" + gap / 1_000_000L + " callback_ms=" + callback / 1_000_000L
                + " previous_callback_ms=" + previous / 1_000_000L
                + (readerInterval == null ? "" : readerInterval.fields());
    }
}
