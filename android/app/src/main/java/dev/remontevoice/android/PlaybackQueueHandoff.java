package dev.remontevoice.android;

import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.BooleanSupplier;

/** Queue handoff outside the audio engine's lifecycle monitor. */
final class PlaybackQueueHandoff {
    static boolean offer(BlockingQueue<float[]> queue, float[] samples,
                         BooleanSupplier running, BooleanSupplier progressing)
            throws InterruptedException {
        while (running.getAsBoolean()) {
            if (queue.offer(samples,100,TimeUnit.MILLISECONDS)) return true;
            if (!running.getAsBoolean()) return false;
            // The interval is a cancellation/progress checkpoint, not proof
            // that queued PCM must be discarded. Use the existing watchdog.
            if (!progressing.getAsBoolean()) {
                throw new IllegalStateException("Звук ноутбука перестал воспроизводиться. Сеанс завершён.");
            }
        }
        return false;
    }
}
