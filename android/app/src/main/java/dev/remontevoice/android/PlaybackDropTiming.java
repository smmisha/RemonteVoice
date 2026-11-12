package dev.remontevoice.android;

/** First queue overflow only; stores scalar debug observations, never audio. */
final class PlaybackDropTiming {
    private static final int BEFORE = 32, AFTER = 16, FIELDS = 6;
    private boolean observed;
    private long at, uptime, offer, gap, maxGap, maxGapAt;
    private long writtenSamples, playedFrames, nativeQueuedFrames, maxWrite, maxWriteAt;
    private final long[] before = new long[BEFORE * FIELDS], after = new long[AFTER * FIELDS];
    private int nextBefore, countBefore, countAfter;

    boolean wantsSample() { return !observed || countAfter < AFTER; }

    void sample(long now, long offered, long queue, long written, long played, long nativeQueued) {
        if (!observed) {
            store(before, nextBefore, now, offered, queue, written, played, nativeQueued);
            nextBefore = (nextBefore + 1) % BEFORE;
            countBefore = Math.min(BEFORE, countBefore + 1);
        } else if (countAfter < AFTER) {
            store(after, countAfter++, now, offered, queue, written, played, nativeQueued);
        }
    }

    private static void store(long[] target, int index, long now, long offered, long queue,
                              long written, long played, long nativeQueued) {
        int offset = index * FIELDS;
        target[offset] = now; target[offset + 1] = offered; target[offset + 2] = queue;
        target[offset + 3] = written; target[offset + 4] = played; target[offset + 5] = nativeQueued;
    }

    void capture(long now, long started, long offered, long previousGap,
                 long maximumGap, long maximumGapAt, long written, long played,
                 long nativeQueued, long maximumWrite, long maximumWriteAt) {
        if (observed) return;
        observed = true;
        at = now;
        uptime = Math.max(0, now - started);
        offer = offered;
        gap = previousGap;
        maxGap = maximumGap;
        maxGapAt = maximumGapAt;
        writtenSamples = written;
        playedFrames = played;
        nativeQueuedFrames = nativeQueued;
        maxWrite = maximumWrite;
        maxWriteAt = maximumWriteAt;
    }

    String fields() {
        if (!observed) return " first_drop_observed=0";
        String summary = " first_drop_observed=1 first_drop_at_ns=" + at
                + " first_drop_uptime_ms=" + uptime / 1_000_000L
                + " first_drop_offer=" + offer + " first_drop_gap_ms=" + gap / 1_000_000L
                + " first_drop_max_gap_ms=" + maxGap / 1_000_000L
                + " first_drop_max_gap_at_ns=" + maxGapAt
                + " first_drop_written_samples=" + writtenSamples
                + " first_drop_played_frames=" + playedFrames
                + " first_drop_native_queued_frames=" + nativeQueuedFrames
                + " first_drop_max_write_ms=" + maxWrite / 1_000_000L
                + " first_drop_max_write_at_ns=" + maxWriteAt;
        if (countBefore == 0 && countAfter == 0) return summary;
        StringBuilder window = new StringBuilder(summary).append(" drop_window=");
        int oldest = (nextBefore - countBefore + BEFORE) % BEFORE;
        for (int i = 0; i < countBefore; i++) append(window, before, (oldest + i) % BEFORE);
        for (int i = 0; i < countAfter; i++) append(window, after, i);
        return window.toString();
    }

    private void append(StringBuilder out, long[] data, int index) {
        if (out.charAt(out.length() - 1) != '=') out.append(',');
        int offset = index * FIELDS;
        out.append((data[offset] - at) / 1_000_000L);
        for (int i = 1; i < FIELDS; i++) out.append(':').append(data[offset + i]);
    }
}
