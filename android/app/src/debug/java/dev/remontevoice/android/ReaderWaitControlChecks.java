package dev.remontevoice.android;

import android.os.Process;
import android.util.Log;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/** Debug-only, local controls for the reader wait observer. No application session is started. */
final class ReaderWaitControlChecks {
    private static final long INTERVAL_MS = 220;
    private static final long TIMEOUT_MS = 2000;
    private static final String TAG = "RemonteVoiceSession";

    private ReaderWaitControlChecks() {}

    static void run(String runId) throws Exception {
        if (runId == null || !runId.matches("[A-Za-z0-9_-]{1,80}")) {
            throw new IllegalArgumentException("Invalid run id");
        }
        runControl(runId, "sleep");
        runControl(runId, "monitor");
        runControl(runId, "busy");
    }

    private static void runControl(String runId, String phase) throws Exception {
        ReaderWaitSite helper = ReaderWaitSite.create(System::nanoTime, android.os.Debug::threadCpuTimeNanos);
        Object monitor = new Object();
        CountDownLatch baselineReady = new CountDownLatch(1);
        AtomicReference<Throwable> workerFailure = new AtomicReference<>();
        Thread worker = new Thread(() -> {
            try {
                int tid = Process.myTid();
                helper.onEntry(1, System.nanoTime(), tid);
                helper.onExit(1);
                baselineReady.countDown();
                if ("sleep".equals(phase)) {
                    Thread.sleep(INTERVAL_MS);
                } else if ("monitor".equals(phase)) {
                    synchronized (monitor) { /* Wait until the owning control releases it. */ }
                } else {
                    long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(INTERVAL_MS);
                    while (System.nanoTime() < deadline && !Thread.currentThread().isInterrupted()) {
                        // Bounded CPU work; no audio or external activity.
                    }
                    if (Thread.currentThread().isInterrupted()) throw new InterruptedException();
                }
                helper.onEntry(2, System.nanoTime(), tid);
                helper.onExit(2);
            } catch (Throwable error) {
                workerFailure.set(error);
                baselineReady.countDown();
            }
        }, "RemonteVoiceWaitControl");
        try {
            if ("monitor".equals(phase)) {
                synchronized (monitor) {
                    worker.start();
                    if (!baselineReady.await(TIMEOUT_MS, TimeUnit.MILLISECONDS)) {
                        throw new AssertionError("Control baseline timed out");
                    }
                    if (workerFailure.get() != null) throw new AssertionError(workerFailure.get());
                    Thread.sleep(INTERVAL_MS);
                }
            } else {
                worker.start();
            }
            worker.join(TIMEOUT_MS);
            if (worker.isAlive()) throw new AssertionError("Control worker did not finish");
            if (workerFailure.get() != null) throw new AssertionError(workerFailure.get());
        } finally {
            worker.interrupt();
            helper.requestStop();
            boolean interrupted = Thread.interrupted();
            long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(TIMEOUT_MS);
            while (worker.isAlive()) {
                long remaining = deadline - System.nanoTime();
                if (remaining <= 0) break;
                try {
                    worker.join(Math.max(1, TimeUnit.NANOSECONDS.toMillis(remaining)));
                } catch (InterruptedException error) {
                    interrupted = true;
                }
            }
            boolean stopped = helper.awaitStopped(TIMEOUT_MS);
            if (interrupted) Thread.currentThread().interrupt();
            if (worker.isAlive()) throw new AssertionError("Control worker cleanup pending");
            if (!stopped) throw new AssertionError("Control observer cleanup pending");
            if (interrupted) throw new InterruptedException("Control cleanup interrupted");
        }
        String summary = helper.maximum();
        if (summary == null || !summary.contains("wait_confirmed=1")) {
            throw new AssertionError("Control gap not confirmed");
        }
        Log.i(TAG, "run=" + runId + " phase=" + phase + " " + summary);
    }
}
