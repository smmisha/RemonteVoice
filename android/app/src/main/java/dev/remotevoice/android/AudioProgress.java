package dev.remotevoice.android;

import java.util.concurrent.TimeUnit;
import java.util.function.LongSupplier;

/** One audio worker's progress; a new worker always owns a new monitor. */
final class AudioProgress {
    private static final long RUNNING_TIMEOUT = TimeUnit.SECONDS.toNanos(2);
    private final LongSupplier clock;
    private final long initialTimeout;
    private long lastProgress;
    private boolean progressed;

    AudioProgress(long initialTimeoutMillis) {
        this(initialTimeoutMillis, System::nanoTime);
    }

    AudioProgress(long initialTimeoutMillis, LongSupplier clock) {
        if (initialTimeoutMillis <= 0) throw new IllegalArgumentException("Positive timeout required");
        this.clock = clock;
        initialTimeout = TimeUnit.MILLISECONDS.toNanos(initialTimeoutMillis);
        lastProgress = clock.getAsLong();
    }

    synchronized void advanced() {
        lastProgress = clock.getAsLong();
        progressed = true;
    }

    synchronized boolean isFresh() {
        return clock.getAsLong() - lastProgress < (progressed ? RUNNING_TIMEOUT : initialTimeout);
    }
}
