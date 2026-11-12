package dev.remontevoice.android;

import static org.junit.Assert.*;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;

public final class ReaderThreadTimingTest {
    private static ReaderThreadTiming.Counters parse(String value) {
        byte[] data = new byte[128];
        byte[] text = value.getBytes(StandardCharsets.US_ASCII);
        System.arraycopy(text, 0, data, 0, text.length);
        return ReaderThreadTiming.parse(data, text.length);
    }

    @Test public void parsesOnlySupportedThreeCounterFormat() {
        ReaderThreadTiming.Counters result = parse("75209998\t303692 27\n");
        assertNotNull(result);
        assertEquals(75209998, result.cpu);
        assertEquals(303692, result.runqueue);
        assertEquals(27, result.slices);
        assertNotNull(parse("1 0 1\n"));
        for (String value : new String[] {"", "0 0 0", "1 2", "1 2 3 4", "-1 0 1",
                "1 x 3", "1 2 3junk", "9223372036854775808 0 1"}) assertNull(parse(value));
        assertNull(ReaderThreadTiming.parse(new byte[128], 128));
    }

    @Test public void separatesCpuFromRunnableWaitAndUnattributedRemainder() {
        AtomicLong now = new AtomicLong();
        AtomicReference<ReaderThreadTiming.Counters> counters = new AtomicReference<>(
                new ReaderThreadTiming.Counters(10_000_000, 1_000_000, 2));
        ReaderThreadTiming timing = new ReaderThreadTiming(now::get, counters::get);
        assertFalse(timing.observe().valid);
        now.set(146_000_000);
        counters.set(new ReaderThreadTiming.Counters(13_000_000, 3_000_000, 5));
        ReaderThreadTiming.Interval interval = timing.observe();
        assertTrue(interval.valid);
        assertEquals(146_000_000, interval.span);
        assertEquals(3_000_000, interval.cpu);
        assertEquals(2_000_000, interval.runqueue);
        assertEquals(3, interval.slices);
        assertTrue(interval.fields().contains("reader_residual_ns=141000000"));
        assertFalse(interval.fields().contains("socket"));
    }

    @Test public void includesMeasuredProbeCostAndKeepsPublishedIntervalImmutable() {
        AtomicLong now = new AtomicLong();
        ReaderThreadTiming timing = new ReaderThreadTiming(now::get, () -> {
            now.addAndGet(200_000);
            return new ReaderThreadTiming.Counters(now.get(), 0, now.get());
        });
        timing.observe(); now.addAndGet(10_000_000);
        ReaderThreadTiming.Interval interval = timing.observe();
        assertEquals(200_000, interval.probe);
        assertEquals(200_000, interval.previousProbe);
        assertEquals(10_200_000, interval.span);
        timing.observe();
        assertEquals(10_200_000, interval.span);
    }

    @Test public void missingOrResetCountersInvalidateInsteadOfClaimingZeroWait() {
        AtomicLong now = new AtomicLong();
        AtomicReference<ReaderThreadTiming.Counters> counters = new AtomicReference<>(
                new ReaderThreadTiming.Counters(100, 50, 5));
        ReaderThreadTiming timing = new ReaderThreadTiming(now::get, counters::get);
        timing.observe(); now.set(1000);
        counters.set(new ReaderThreadTiming.Counters(90, 40, 4));
        assertFalse(timing.observe().valid);
        counters.set(null); assertFalse(timing.observe().valid);
        counters.set(new ReaderThreadTiming.Counters(200, 60, 6));
        assertFalse(timing.observe().valid);
        now.set(2000); assertTrue(timing.observe().valid);
    }

    @Test public void sourceFailureDoesNotEscapeTheObserver() {
        ReaderThreadTiming timing = new ReaderThreadTiming(System::nanoTime,
                () -> { throw new SecurityException("synthetic proc denial"); });
        assertFalse(timing.observe().valid);
        assertFalse(timing.observe().valid);
    }

    @Test public void counterOvershootsAreUnknownAndTheNextIntervalCanRecover() {
        AtomicLong now = new AtomicLong();
        AtomicReference<ReaderThreadTiming.Counters> counters = new AtomicReference<>(
                new ReaderThreadTiming.Counters(1, 0, 1));
        ReaderThreadTiming timing = new ReaderThreadTiming(now::get, counters::get);
        timing.observe();
        now.set(1_000_000);
        counters.set(new ReaderThreadTiming.Counters(3_000_001, 0, 1));
        ReaderThreadTiming.Interval cpuOvershoot = timing.observe();
        assertFalse(cpuOvershoot.valid);
        assertTrue(cpuOvershoot.fields().contains("reader_valid=0"));
        assertTrue(cpuOvershoot.fields().contains("reader_residual_ns=0"));
        now.set(2_000_000);
        counters.set(new ReaderThreadTiming.Counters(3_100_001, 100_000, 2));
        assertTrue(timing.observe().valid);
        now.set(3_000_000);
        counters.set(new ReaderThreadTiming.Counters(3_700_001, 600_000, 3));
        assertFalse(timing.observe().valid);
        now.set(4_000_000);
        counters.set(new ReaderThreadTiming.Counters(3_800_001, 700_000, 4));
        assertTrue(timing.observe().valid);
    }

    @Test public void checksCounterPartitionWithoutOverflowAndAcceptsExactBoundaries() {
        AtomicLong now = new AtomicLong();
        AtomicReference<ReaderThreadTiming.Counters> counters = new AtomicReference<>(
                new ReaderThreadTiming.Counters(1, 0, 1));
        ReaderThreadTiming timing = new ReaderThreadTiming(now::get, counters::get);
        timing.observe();
        assertTrue(timing.observe().valid);
        now.set(Long.MAX_VALUE);
        counters.set(new ReaderThreadTiming.Counters(Long.MAX_VALUE, Long.MAX_VALUE-1, 2));
        assertFalse(timing.observe().valid);

        now.set(0);
        counters.set(new ReaderThreadTiming.Counters(1, 0, 1));
        timing = new ReaderThreadTiming(now::get, counters::get);
        timing.observe();
        now.set(100);
        counters.set(new ReaderThreadTiming.Counters(61, 40, 2));
        ReaderThreadTiming.Interval exact = timing.observe();
        assertTrue(exact.valid);
        assertTrue(exact.fields().contains("reader_residual_ns=0"));
    }

    @Test public void doesNotCompareDifferentReaderThreads() throws Exception {
        ReaderThreadTiming timing = new ReaderThreadTiming(System::nanoTime,
                () -> new ReaderThreadTiming.Counters(100, 0, 1));
        timing.observe();
        AtomicReference<ReaderThreadTiming.Interval> observed = new AtomicReference<>();
        Thread other = new Thread(() -> observed.set(timing.observe()));
        other.start(); other.join(2000);
        assertFalse(other.isAlive()); assertNotNull(observed.get());
        assertFalse(observed.get().valid);
        assertFalse(timing.observe().valid);
    }
}
