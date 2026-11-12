package dev.remontevoice.android;

import static org.junit.Assert.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;

public final class AudioReceiveTimingTest {
    @Test public void normalBatchesAndFirstPacketAreQuiet() {
        AtomicLong now = new AtomicLong();
        AudioReceiveTiming timing = new AudioReceiveTiming(now::get);
        timing.begin(); now.set(1_000_000); assertNull(timing.finish());
        now.set(100_000_000); timing.begin(); now.set(101_000_000); assertNull(timing.finish());
    }

    @Test public void separatesArrivalGapFromConsumerWork() {
        AtomicLong now = new AtomicLong();
        AudioReceiveTiming timing = new AudioReceiveTiming(now::get);
        timing.begin(); now.set(1_000_000); timing.finish();
        now.set(330_000_000); timing.begin(); now.set(332_000_000);
        String event = timing.finish();
        assertTrue(event.contains("packet=2 clock_ns=330000000 gap_ms=330 callback_ms=2 previous_callback_ms=1"));
    }

    @Test public void showsWhenPreviousConsumerExplainsTheGap() {
        AtomicLong now = new AtomicLong();
        AudioReceiveTiming timing = new AudioReceiveTiming(now::get);
        timing.begin(); now.set(200_000_000);
        assertTrue(timing.finish().contains("callback_ms=200"));
        now.set(210_000_000); timing.begin(); now.set(211_000_000);
        assertTrue(timing.finish().contains("gap_ms=210 callback_ms=1 previous_callback_ms=200 gap_seq=1"));
        now.set(1_300_000_000); timing.begin(); now.set(1_301_000_000);
        assertTrue(timing.finish().contains("previous_callback_ms=1 gap_seq=2"));
    }

    @Test public void everyArrivalGapAtThresholdIsReportedAndNumberedWithoutRateLimit() {
        AtomicLong now = new AtomicLong();
        AudioReceiveTiming timing = new AudioReceiveTiming(now::get);
        timing.begin(); now.set(1_000_000); assertNull(timing.finish());
        now.set(120_000_000); timing.begin(); now.set(121_000_000);
        assertTrue(timing.finish().contains("packet=2 clock_ns=120000000 gap_ms=120 callback_ms=1 previous_callback_ms=1 gap_seq=1"));
        now.set(239_999_999); timing.begin(); now.set(240_000_000);
        assertNull(timing.finish());
        now.set(400_000_000); timing.begin(); now.set(401_000_000);
        assertTrue(timing.finish().contains("packet=4 clock_ns=400000000 gap_ms=160 callback_ms=1 previous_callback_ms=0 gap_seq=2"));
        now.set(530_000_000); timing.begin(); now.set(531_000_000);
        assertTrue(timing.finish().contains("packet=5 clock_ns=530000000 gap_ms=130 callback_ms=1 previous_callback_ms=1 gap_seq=3"));
        assertTrue(timing.maximum().contains("gap_events=3"));
    }

    @Test public void slowCallbackWithoutArrivalGapStaysRateLimitedAndUnnumbered() {
        AtomicLong now = new AtomicLong();
        AudioReceiveTiming timing = new AudioReceiveTiming(now::get);
        timing.begin(); now.set(40_000_000);
        String first = timing.finish();
        assertTrue(first.contains("callback_ms=40"));
        assertFalse(first.contains("gap_seq="));
        now.set(50_000_000); timing.begin(); now.set(90_000_000);
        assertNull(timing.finish());
        now.set(300_000_000); timing.begin(); now.set(301_000_000);
        assertTrue(timing.finish().contains("gap_ms=250 callback_ms=1 previous_callback_ms=40 gap_seq=1"));
        assertTrue(timing.maximum().contains("gap_events=1"));
    }

    @Test public void preservesMaximumAndItsReaderIntervalWhenLoggingIsSuppressed() {
        AtomicLong now = new AtomicLong();
        AtomicLong cpu = new AtomicLong(1), queue = new AtomicLong();
        ReaderThreadTiming reader = new ReaderThreadTiming(now::get,
                () -> new ReaderThreadTiming.Counters(cpu.get(), queue.get(), 1));
        AudioReceiveTiming timing = new AudioReceiveTiming(now::get, reader);
        assertNull(timing.maximum());
        timing.begin(); timing.finish();
        now.set(150_000_000); cpu.set(1_000_001); timing.begin();
        assertNotNull(timing.finish());
        now.set(515_000_000); cpu.set(3_000_001); queue.set(2_000_000);
        timing.begin(); assertTrue(timing.finish().contains("gap_seq=2"));
        String maximum = timing.maximum();
        assertTrue(maximum.contains("packet=3 clock_ns=515000000 gap_ns=365000000 gap_events=2"));
        assertTrue(maximum.contains("reader_valid=1 reader_span_ns=365000000 reader_cpu_ns=2000000"));
        assertTrue(maximum.contains("reader_runqueue_ns=2000000 reader_residual_ns=361000000"));
        now.set(525_000_000); timing.begin(); timing.finish();
        assertEquals(maximum, timing.maximum());
    }

    @Test public void frozenMaximumCannotChangeOrAppearAfterClosing() {
        AtomicLong now = new AtomicLong();
        AudioReceiveTiming empty = new AudioReceiveTiming(now::get);
        empty.freezeMaximum(); empty.freezeMaximum();
        empty.begin(); empty.finish();
        assertNull(empty.maximum());

        AudioReceiveTiming timing = new AudioReceiveTiming(now::get);
        timing.begin(); timing.finish();
        now.set(150_000_000); timing.begin(); timing.finish();
        String atClose = timing.maximum();
        timing.freezeMaximum(); timing.freezeMaximum();
        assertTrue(atClose.contains("gap_events=1"));
        now.set(1_000_000_000); timing.begin();
        assertTrue(timing.finish().contains("gap_seq=0"));
        assertEquals(atClose, timing.maximum());
    }

    @Test public void freezingMaximumDoesNotWaitForInflightReaderProbe() throws Exception {
        AtomicLong now = new AtomicLong();
        AtomicInteger reads = new AtomicInteger();
        CountDownLatch sourceEntered = new CountDownLatch(1);
        CountDownLatch releaseSource = new CountDownLatch(1);
        CountDownLatch freezeDone = new CountDownLatch(1);
        AtomicReference<Throwable> failed = new AtomicReference<>();
        ReaderThreadTiming reader = new ReaderThreadTiming(now::get, () -> {
            if (reads.incrementAndGet() == 2) {
                sourceEntered.countDown();
                try {
                    if (!releaseSource.await(3, TimeUnit.SECONDS))
                        throw new AssertionError("source release timeout");
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt();
                    throw new AssertionError(error);
                }
            }
            return new ReaderThreadTiming.Counters(1, 0, 1);
        });
        AudioReceiveTiming timing = new AudioReceiveTiming(now::get, reader);
        timing.begin(); timing.finish();
        String before = timing.maximum();
        now.set(150_000_000);
        Thread callback = new Thread(() -> {
            try { timing.begin(); timing.finish(); }
            catch (Throwable error) { failed.set(error); }
        });
        Thread freezer = new Thread(() -> {
            try { timing.freezeMaximum(); }
            catch (Throwable error) { failed.set(error); }
            finally { freezeDone.countDown(); }
        });
        try {
            callback.start();
            assertTrue(sourceEntered.await(1, TimeUnit.SECONDS));
            freezer.start();
            assertTrue(freezeDone.await(1, TimeUnit.SECONDS));
            assertEquals(before, timing.maximum());
        } finally {
            releaseSource.countDown();
            callback.join(2000);
            if (freezer.getState() != Thread.State.NEW) freezer.join(2000);
        }
        assertFalse(callback.isAlive());
        assertFalse(freezer.isAlive());
        assertNull(failed.get());
        assertEquals(before, timing.maximum());
    }
}
