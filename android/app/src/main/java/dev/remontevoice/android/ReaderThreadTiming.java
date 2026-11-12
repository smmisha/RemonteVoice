package dev.remontevoice.android;

import java.io.FileInputStream;
import java.io.IOException;
import java.util.function.LongSupplier;
import java.util.function.Supplier;

/** Debug-only counters of this reader thread. No sockets, payloads or thread sampling. */
final class ReaderThreadTiming {
    static final class Counters {
        final long cpu, runqueue, slices;
        Counters(long cpu, long runqueue, long slices) {
            this.cpu = cpu; this.runqueue = runqueue; this.slices = slices;
        }
    }

    static final class Interval {
        final boolean valid;
        final long span, cpu, runqueue, slices, probe, previousProbe;
        Interval(boolean valid, long span, long cpu, long runqueue, long slices,
                 long probe, long previousProbe) {
            this.valid = valid; this.span = span; this.cpu = cpu; this.runqueue = runqueue;
            this.slices = slices; this.probe = probe; this.previousProbe = previousProbe;
        }
        String fields() {
            return " reader_valid=" + (valid ? 1 : 0)
                    + " reader_span_ns=" + span + " reader_cpu_ns=" + cpu
                    + " reader_runqueue_ns=" + runqueue
                    // Raw remainder, including accounting uncertainty; NOT socket wait alone.
                    + " reader_residual_ns=" + (valid ? span-cpu-runqueue : 0)
                    + " reader_slices=" + slices + " reader_probe_ns=" + probe
                    + " reader_previous_probe_ns=" + previousProbe;
        }
    }

    private final LongSupplier clock;
    private final Supplier<Counters> source;
    private Counters previous;
    private long previousAt, previousThread, previousProbe;

    ReaderThreadTiming(LongSupplier clock, Supplier<Counters> source) {
        this.clock = clock; this.source = source;
    }

    static ReaderThreadTiming linux(LongSupplier clock) {
        return new ReaderThreadTiming(clock, new ProcCounters());
    }

    Interval observe() {
        long start = clock.getAsLong();
        Counters current;
        try { current = source.get(); }
        catch (RuntimeException ignored) { current = null; }
        long end = clock.getAsLong();
        long probe = Math.max(0, end-start);
        long thread = Thread.currentThread().getId();
        long span = previous == null ? 0 : Math.max(0, end-previousAt);
        boolean valid = previous != null && current != null && thread == previousThread
                && end >= previousAt && current.cpu >= previous.cpu
                && current.runqueue >= previous.runqueue && current.slices >= previous.slices;
        long cpu = valid ? current.cpu-previous.cpu : 0;
        long runqueue = valid ? current.runqueue-previous.runqueue : 0;
        // schedstat CPU may lag scheduler updates at either boundary. An overshoot
        // is unknown accounting, not a valid negative remainder. Avoid summing counters.
        valid = valid && cpu <= span && runqueue <= span-cpu;
        Interval result = new Interval(valid, span,
                valid ? cpu : 0,
                valid ? runqueue : 0,
                valid ? current.slices-previous.slices : 0, probe, previousProbe);
        previous = current; previousAt = end; previousThread = thread; previousProbe = probe;
        return result;
    }

    /** Parse exactly the three nonnegative schedstat counters with bounded input. */
    static Counters parse(byte[] bytes, int length) {
        if (length <= 0 || length >= bytes.length) return null;
        long[] values = new long[3];
        int count = 0, index = 0;
        while (index < length) {
            while (index < length && whitespace(bytes[index])) index++;
            if (index == length) break;
            if (count == 3) return null;
            long value = 0; int digits = 0;
            while (index < length && bytes[index] >= '0' && bytes[index] <= '9') {
                int digit = bytes[index++]-'0';
                if (value > (Long.MAX_VALUE-digit)/10) return null;
                value = value*10+digit; digits++;
            }
            if (digits == 0 || index < length && !whitespace(bytes[index])) return null;
            values[count++] = value;
        }
        // All-zero counters may mean unsupported accounting; never report them as no wait.
        return count == 3 && values[0] > 0 && values[2] > 0
                ? new Counters(values[0], values[1], values[2]) : null;
    }

    private static boolean whitespace(byte value) {
        return value == ' ' || value == '\t' || value == '\n' || value == '\r';
    }

    private static final class ProcCounters implements Supplier<Counters> {
        private final byte[] bytes = new byte[128];
        private boolean unavailable;
        @Override public Counters get() {
            if (unavailable) return null;
            // thread-self is resolved by the kernel for the caller, not the app's main thread.
            try (FileInputStream stream = new FileInputStream("/proc/thread-self/schedstat")) {
                return parse(bytes, stream.read(bytes));
            } catch (IOException | RuntimeException ignored) {
                unavailable = true;
                return null;
            }
        }
    }
}
