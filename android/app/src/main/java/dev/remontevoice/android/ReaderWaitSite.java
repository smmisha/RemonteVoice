package dev.remontevoice.android;

import java.io.FileInputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Objects;
import java.util.function.LongSupplier;

/** Opt-in debug observation of one registered reader. Never accepts audio or changes its socket.
 * Java RUNNABLE is not a scheduler state; a Java SSL frame alone is not a socket wait.
 * Sampling is not atomic and cannot observe a VM-wide suspension or guarantee stack API cancellation.
 */
final class ReaderWaitSite {
    static final long TRIGGER_NS = 80_000_000L, GAP_NS = 120_000_000L;
    static final long TICK_NS = 20_000_000L, BIAS_NS = 10_000_000L;
    static final long DEADLINE_NS = 300_000_000_000L;
    static final int MAX_ATTEMPTS = 64, MAX_PER_GAP = 3, MAX_FRAMES = 32;

    enum Path { SSL_READ, MONITOR, SLEEP, WAIT, PARK, OTHER, EMPTY }
    enum Evidence { SSL_POLL_OBSERVED, CALLPATH_ONLY, UNKNOWN }
    enum Problem { NONE, UNSUPPORTED, ZERO_WCHAN, MALFORMED, PROBE_EXCEPTION,
        KERNEL_CHANGED, JAVA_CHANGED, CLOCK, MEASURING_BIAS }
    enum Site { POLL_TIMEOUT, SELECT, DO_POLL, EPOLL, FUTEX, RECEIVE, SLEEP, OTHER, NONE }
    enum Discard { NONE, STALE, STOPPED, BOUNDARY, CLOCK }
    enum CpuProblem { NONE, NOT_MEASURED, UNSUPPORTED, EXCEPTION, RESET, THREAD_CHANGED, CLOCK, OVERSHOOT }

    static final class Kernel {
        final char state;
        final long birth;
        final Site site;
        final Problem problem;
        Kernel(char state, long birth, Site site, Problem problem) {
            this.state = state; this.birth = birth; this.site = site; this.problem = problem;
        }
        static Kernel missing(Problem reason) { return new Kernel('?', 0, Site.NONE, reason); }
        boolean sleeping() { return state == 'S' || state == 'D'; }
    }

    interface Probe { Observation capture(Thread target, int nativeTid); }
    interface KernelSource { Kernel read(int nativeTid); }
    interface StackSource { StackTraceElement[] read(Thread target); }

    static final class Observation {
        final Kernel before, after;
        final Path path;
        final boolean javaStable, truncated;
        final Timings timings;
        final Problem sourceProblem;
        Observation(Kernel before, Kernel after, Path path, boolean javaStable, boolean truncated) {
            this(before, after, path, javaStable, truncated, null, Problem.NONE);
        }
        Observation(Kernel before, Kernel after, Path path, boolean javaStable, boolean truncated,
                Timings timings, Problem sourceProblem) {
            this.before = before; this.after = after; this.path = path;
            this.javaStable = javaStable; this.truncated = truncated;
            this.timings = timings; this.sourceProblem = sourceProblem;
        }
    }

    /** Fixed wall timestamps and pre/post current-capture-thread CPU counters; separate clock domains. */
    static final class Timings {
        private final long[] times = new long[6];
        private final long[] cpuTimes = new long[6];
        private final CpuProblem[] cpuProblems = {CpuProblem.NOT_MEASURED, CpuProblem.NOT_MEASURED,
                CpuProblem.NOT_MEASURED};
        private final Thread captureThread = Thread.currentThread();
        private final LongSupplier cpu;
        private int mask;
        private boolean clockValid = true;
        Timings(LongSupplier cpu) { this.cpu = cpu; }
        private long time(LongSupplier clock) {
            try { return clock.getAsLong(); }
            catch (RuntimeException ignored) { clockValid = false; return 0; }
        }
        private long cpuTime(int stage) {
            if (Thread.currentThread() != captureThread) {
                cpuProblems[stage] = CpuProblem.THREAD_CHANGED; return -1;
            }
            if (cpu == null) { cpuProblems[stage] = CpuProblem.UNSUPPORTED; return -1; }
            try {
                long value = cpu.getAsLong();
                if (value < 0 && cpuProblems[stage] == CpuProblem.NONE)
                    cpuProblems[stage] = CpuProblem.UNSUPPORTED;
                return value;
            } catch (RuntimeException ignored) {
                if (cpuProblems[stage] == CpuProblem.NONE) cpuProblems[stage] = CpuProblem.EXCEPTION;
                return -1;
            }
        }
        void begin(int stage, LongSupplier clock) {
            times[stage*2] = time(clock);
            if (stage != 1) {
                cpuProblems[stage] = CpuProblem.NONE; cpuTimes[stage*2] = cpuTime(stage);
            }
        }
        void end(int stage, LongSupplier clock) {
            if (stage != 1) cpuTimes[stage*2+1] = cpuTime(stage);
            times[stage*2+1] = time(clock); mask |= 1 << stage;
        }
        boolean validWithin(long start, long end) {
            if (!clockValid || end < start) return false;
            long previous = start;
            for (int i = 0; i < 3; i++) if ((mask & 1 << i) != 0) {
                if (times[i*2] < previous || times[i*2+1] < times[i*2] || times[i*2+1] > end) return false;
                previous = times[i*2+1];
            }
            return true;
        }
        private CpuProblem cpuProblem(int stage, long start, long end) {
            if ((mask & 1 << stage) == 0) return CpuProblem.NOT_MEASURED;
            if (!validWithin(start, end) || times[stage*2+1]-times[stage*2] < 0) return CpuProblem.CLOCK;
            if (cpuProblems[stage] != CpuProblem.NONE) return cpuProblems[stage];
            long first = cpuTimes[stage*2], last = cpuTimes[stage*2+1];
            if (first < 0 || last < 0) return CpuProblem.UNSUPPORTED;
            if (last < first) return CpuProblem.RESET;
            return last-first > times[stage*2+1]-times[stage*2] ? CpuProblem.OVERSHOOT : CpuProblem.NONE;
        }
        String fields(long start, long end) {
            String result = " wait_last_stage_mask=" + mask
                    + " wait_last_stage_clock_valid=" + (validWithin(start, end) ? 1 : 0)
                    + " wait_last_observer_java_id=" + captureThread.getId();
            String[] names = {"pre", "stack", "post"};
            for (int i = 0; i < 3; i++) {
                boolean known = clockValid && (mask & 1 << i) != 0;
                result += " wait_last_" + names[i] + "_start_ns=" + (known ? times[i*2] : -1)
                        + " wait_last_" + names[i] + "_end_ns=" + (known ? times[i*2+1] : -1);
                if (i != 1) {
                    CpuProblem problem = cpuProblem(i, start, end);
                    boolean valid = problem == CpuProblem.NONE;
                    result += " wait_last_" + names[i] + "_cpu_ns="
                            + (valid ? cpuTimes[i*2+1]-cpuTimes[i*2] : -1)
                            + " wait_last_" + names[i] + "_cpu_valid=" + (valid ? 1 : 0)
                            + " wait_last_" + names[i] + "_cpu_problem=" + problem;
                }
            }
            return result;
        }
    }

    /** Raw stack elements are inspected transiently and never stored or emitted. */
    static final class OwnProbe implements Probe {
        private final KernelSource kernel;
        private final StackSource stacks;
        private final LongSupplier clock;
        private final LongSupplier cpu;
        OwnProbe(KernelSource kernel, StackSource stacks, LongSupplier clock) {
            this(kernel, stacks, clock, null);
        }
        OwnProbe(KernelSource kernel, StackSource stacks, LongSupplier clock, LongSupplier cpu) {
            this.kernel = kernel; this.stacks = stacks; this.clock = clock; this.cpu = cpu;
        }
        @Override public Observation capture(Thread target, int nativeTid) {
            Timings timings = new Timings(cpu);
            Kernel before = Kernel.missing(Problem.UNSUPPORTED), after = before;
            Thread.State first = null, second = null;
            Path path = Path.EMPTY;
            boolean truncated = false;
            Problem problem = Problem.NONE;
            try {
                timings.begin(0, clock);
                try {
                    if (stacks != null) first = target.getState();
                    before = kernel.read(nativeTid);
                } finally { timings.end(0, clock); }
                if (stacks != null) {
                    timings.begin(1, clock);
                    try {
                        StackTraceElement[] frames = stacks.read(target);
                        second = target.getState();
                        path = classify(frames, first, second);
                        truncated = frames != null && frames.length > MAX_FRAMES;
                    } finally { timings.end(1, clock); }
                }
                timings.begin(2, clock);
                try { after = kernel.read(nativeTid); }
                finally { timings.end(2, clock); }
            } catch (RuntimeException ignored) { problem = Problem.PROBE_EXCEPTION; }
            if (!timings.clockValid) problem = Problem.CLOCK;
            return new Observation(before, after, path, first == second, truncated, timings, problem);
        }
    }

    private static final class Pending {
        final Thread thread;
        final int tid;
        final long packet, entry, epoch;
        final Sample[] samples = new Sample[MAX_PER_GAP];
        int attempts, count;
        long lastAttempt;
        boolean exited;
        Pending(Thread thread, int tid, long packet, long entry, long epoch) {
            this.thread = thread; this.tid = tid; this.packet = packet; this.entry = entry;
            this.epoch = epoch;
        }
    }

    private static final class Sample {
        final long start, end;
        final Evidence evidence;
        final Path path;
        final Site site;
        final Problem problem;
        final boolean inCallback, truncated;
        final Timings timings;
        final int attempt;
        Sample(long start, long end, Observation value, Problem failure, boolean inCallback, int attempt) {
            this.start = start; this.end = end; this.inCallback = inCallback;
            this.attempt = attempt; timings = value == null ? null : value.timings;
            path = value == null ? Path.EMPTY : value.path;
            truncated = value != null && value.truncated;
            Kernel before = value == null ? Kernel.missing(failure) : value.before;
            Kernel after = value == null ? Kernel.missing(failure) : value.after;
            boolean stableKernel = before.problem == Problem.NONE && after.problem == Problem.NONE
                    && before.birth > 0 && before.birth == after.birth
                    && before.state == after.state && before.sleeping() && before.site == after.site;
            site = stableKernel ? before.site : Site.NONE;
            problem = failure != Problem.NONE ? failure
                    : before.problem != Problem.NONE ? before.problem
                    : after.problem != Problem.NONE ? after.problem
                    : !value.javaStable ? Problem.JAVA_CHANGED
                    : !stableKernel ? Problem.KERNEL_CHANGED : Problem.NONE;
            boolean poll = site == Site.POLL_TIMEOUT || site == Site.SELECT
                    || site == Site.DO_POLL || site == Site.EPOLL;
            evidence = problem == Problem.NONE && poll && path == Path.SSL_READ
                    ? Evidence.SSL_POLL_OBSERVED
                    : failure == Problem.NONE && value != null && value.javaStable && path != Path.EMPTY
                    ? Evidence.CALLPATH_ONLY : Evidence.UNKNOWN;
        }
        String fields(int index) {
            String prefix = " wait_sample" + index + "_";
            return prefix + "start_ns=" + start + prefix + "end_ns=" + end
                    + prefix + "probe_ns=" + (end >= start ? end-start : 0)
                    + prefix + "evidence=" + evidence + prefix + "path=" + path
                    + prefix + "kernel=" + site + prefix + "problem=" + problem
                    + prefix + "in_callback=" + (inCallback ? 1 : 0)
                    + prefix + "truncated=" + (truncated ? 1 : 0);
        }
    }

    /** Separate diagnostic channel, never an attribution to a confirmed callback gap. */
    private static final class Receipt {
        final long packet, epoch;
        final int tid;
        final Sample sample;
        final Discard discard;
        Receipt(Pending target, Sample sample, Discard discard) {
            packet = target.packet; epoch = target.epoch; tid = target.tid;
            this.sample = sample; this.discard = discard;
        }
        String fields() {
            return " wait_last_present=1 wait_last_gap_linked=0 wait_last_packet=" + packet
                    + " wait_last_tid=" + tid + " wait_last_epoch=" + epoch
                    + " wait_last_attempt=" + sample.attempt + " wait_last_start_ns=" + sample.start
                    + " wait_last_end_ns=" + sample.end + " wait_last_discard=" + discard
                    + " wait_last_problem=" + sample.problem
                    + " wait_last_clock_valid=" + (sample.problem == Problem.CLOCK ? 0 : 1)
                    + " wait_last_bias=" + (sample.end >= sample.start && sample.end-sample.start > BIAS_NS ? 1 : 0)
                    + " wait_last_stale=" + (discard != Discard.NONE ? 1 : 0)
                    + " wait_last_kernel=" + sample.site
                    + (sample.timings == null ? " wait_last_stage_mask=0 wait_last_stage_clock_valid=0"
                            : sample.timings.fields(sample.start, sample.end));
        }
    }

    private static final class Maximum {
        final long packet, previousPacket, from, to;
        final int tid, unknown;
        final Sample[] samples;
        Maximum(Pending previous, long packet, long to) {
            this.packet = packet; previousPacket = previous.packet;
            from = previous.entry; this.to = to; tid = previous.tid;
            Sample[] bounded = new Sample[previous.count];
            int retained = 0;
            for (int i = 0; i < previous.count; i++) {
                Sample sample = previous.samples[i];
                if (sample.start >= from && sample.end >= sample.start && sample.end <= to)
                    bounded[retained++] = sample;
            }
            samples = Arrays.copyOf(bounded, retained);
            int missing = samples.length == 0 ? 1 : 0;
            for (Sample sample : samples) if (sample.evidence != Evidence.SSL_POLL_OBSERVED) missing++;
            unknown = missing;
        }
    }

    private final Object publication = new Object();
    private final LongSupplier clock;
    private final Probe probe;
    private final long started;
    private final Thread observer;
    private volatile boolean stopped;
    private Pending pending;
    private Maximum maximum;
    private Receipt lastFailure;
    private long epoch;
    private int attempts, stale, invalidIdentity, failed, truncated;
    private boolean biased, expired;

    /** Caller must gate creation with both debug identity and explicit local capture opt-in. */
    static ReaderWaitSite create(LongSupplier clock) {
        return create(clock, null);
    }

    static ReaderWaitSite create(LongSupplier clock, LongSupplier observerCpu) {
        return new ReaderWaitSite(clock, new OwnProbe(new ProcKernel(), Thread::getStackTrace, clock, observerCpu), true);
    }

    /** Proc-only mode has no stack source and never calls getStackTrace or infers an SSL path. */
    static ReaderWaitSite createProcOnly(LongSupplier clock) {
        return createProcOnly(clock, null);
    }

    static ReaderWaitSite createProcOnly(LongSupplier clock, LongSupplier observerCpu) {
        return new ReaderWaitSite(clock, new OwnProbe(new ProcKernel(), null, clock, observerCpu), true);
    }

    ReaderWaitSite(LongSupplier clock, Probe probe, boolean startObserver) {
        this.clock = Objects.requireNonNull(clock); this.probe = Objects.requireNonNull(probe);
        started = clock.getAsLong();
        observer = startObserver ? new Thread(this::run, "RemonteVoiceReaderWait") : null;
        if (observer != null) { observer.setDaemon(true); observer.start(); }
    }

    void onEntry(long packet, long entryNanos, int nativeTid) {
        if (stopped) return;
        Thread thread = Thread.currentThread();
        synchronized (publication) {
            if (stopped) return;
            Pending previous = pending;
            if (packet <= 0 || nativeTid <= 0) {
                invalidIdentity++; pending = null; return;
            }
            boolean same = previous != null && previous.thread == thread && previous.tid == nativeTid;
            if (same) {
                // entryNanos may have been captured before this callback acquired publication.
                // A probe published in that interval still crosses the true callback boundary.
                for (int i = 0; i < previous.count; i++) {
                    Sample sample = previous.samples[i];
                    if (sample.start < previous.entry || sample.end > entryNanos) {
                        stale++; remember(previous, sample, Discard.BOUNDARY);
                    }
                }
            }
            if (same && packet == previous.packet + 1 && entryNanos >= previous.entry
                    && entryNanos-previous.entry >= GAP_NS
                    && (maximum == null || entryNanos-previous.entry > maximum.to-maximum.from)) {
                maximum = new Maximum(previous, packet, entryNanos);
            }
            pending = new Pending(thread, nativeTid, packet, entryNanos, ++epoch);
        }
    }

    void onExit(long packet) {
        synchronized (publication) {
            if (!stopped && pending != null && pending.thread == Thread.currentThread()
                    && pending.packet == packet) pending.exited = true;
        }
    }

    private void run() {
        try {
            while (!stopped) {
                tick();
                if (!stopped) {
                    try { Thread.sleep(20); }
                    catch (InterruptedException ignored) { /* requestStop wakes this observer only. */ }
                }
            }
        } catch (RuntimeException ignored) {
            synchronized (publication) { failed++; }
        } finally { stopped = true; }
    }

    /** Package-private for deterministic tests; production has exactly one observer caller. */
    void tick() {
        if (stopped) return;
        long start = clock.getAsLong();
        Pending target;
        boolean inCallback;
        int attempt;
        synchronized (publication) {
            if (stopped) return;
            if (start < started || start-started >= DEADLINE_NS) {
                expired = true; stopped = true; return;
            }
            target = pending;
            if (target == null || biased || attempts >= MAX_ATTEMPTS || target.attempts >= MAX_PER_GAP
                    || start < target.entry || start-target.entry < TRIGGER_NS
                    || target.attempts > 0 && (start < target.lastAttempt
                        || start-target.lastAttempt < TICK_NS)) return;
            target.attempts++; target.lastAttempt = start; attempts++;
            attempt = attempts;
            inCallback = !target.exited;
        }
        // This API can suspend the target. Never hold publication or a lifecycle lock here.
        Observation result = null;
        Problem failure = Problem.NONE;
        try { result = probe.capture(target.thread, target.tid); }
        catch (RuntimeException ignored) { failure = Problem.PROBE_EXCEPTION; }
        if (result == null || result.before == null || result.after == null || result.path == null) {
            failure = Problem.PROBE_EXCEPTION; result = null;
        }
        long end;
        try { end = clock.getAsLong(); }
        catch (RuntimeException ignored) { end = start; failure = Problem.CLOCK; }
        boolean bias = end >= start && end-start > BIAS_NS;
        if (end < start || result != null && result.timings != null && !result.timings.validWithin(start, end))
            failure = Problem.CLOCK;
        else if (bias) failure = Problem.MEASURING_BIAS;
        else if (failure == Problem.NONE && result != null) failure = result.sourceProblem;
        Sample sample = new Sample(start, end, result, failure, inCallback, attempt);
        synchronized (publication) {
            if (bias) biased = true;
            if (failure != Problem.NONE) failed++;
            if (sample.truncated) truncated++;
            if (stopped || pending != target || end < start || end < target.entry) {
                stale++;
                remember(target, sample, stopped ? Discard.STOPPED
                        : end < start || end < target.entry ? Discard.CLOCK : Discard.STALE);
                return;
            }
            if (sample.problem != Problem.NONE || sample.evidence != Evidence.SSL_POLL_OBSERVED)
                remember(target, sample, Discard.NONE);
            target.samples[target.count++] = sample;
        }
    }

    /** Caller holds publication; fixed-size receipt can outlive stop, confirmed maximum cannot. */
    private void remember(Pending target, Sample sample, Discard discard) {
        if (lastFailure == null || sample.attempt >= lastFailure.sample.attempt)
            lastFailure = new Receipt(target, sample, discard);
    }

    /** Cuts off publication; never waits for getStackTrace, procfs, or an observer join. */
    void requestStop() {
        synchronized (publication) { stopped = true; }
        if (observer != null) observer.interrupt();
    }

    /** Call only after socket/routes cleanup, outside lifecycle locks and off the main/reader thread. */
    boolean awaitStopped(long timeoutMillis) {
        if (observer == null) return true;
        if (Thread.currentThread() == observer) return false;
        if (timeoutMillis > 0) {
            try { observer.join(timeoutMillis); }
            catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
        }
        return !observer.isAlive();
    }

    String maximum() {
        Maximum value;
        Receipt receipt;
        int total, old, invalid, failures, cuts;
        boolean bias, deadline;
        synchronized (publication) {
            value = maximum; total = attempts; old = stale; invalid = invalidIdentity;
            failures = failed; cuts = truncated; bias = biased; deadline = expired;
            receipt = lastFailure;
        }
        boolean alive = observer != null && observer.isAlive();
        String result = "reader_wait_maximum wait_confirmed=" + (value == null ? 0 : 1)
                + " wait_attempts=" + total + " wait_stale=" + old + " wait_invalid_identity=" + invalid
                + " wait_failures=" + failures + " wait_truncated=" + cuts
                + " wait_budget_exhausted=" + (total >= MAX_ATTEMPTS ? 1 : 0)
                + " wait_measuring_bias=" + (bias ? 1 : 0) + " wait_deadline=" + (deadline ? 1 : 0)
                + " wait_stopped=" + (stopped ? 1 : 0) + " observer_alive=" + (alive ? 1 : 0)
                + " cleanup_pending=" + (stopped && alive ? 1 : 0);
        if (value == null) result += " wait_samples=0 wait_unknown=1";
        else {
            result += " wait_packet=" + value.packet + " wait_previous_packet=" + value.previousPacket
                + " wait_from_ns=" + value.from + " wait_to_ns=" + value.to
                + " wait_gap_ns=" + (value.to-value.from) + " wait_native_tid=" + value.tid
                + " wait_samples=" + value.samples.length + " wait_unknown=" + value.unknown;
            for (int i = 0; i < value.samples.length; i++) result += value.samples[i].fields(i);
        }
        return result + (receipt == null ? " wait_last_present=0" : receipt.fields());
    }

    static Path classify(StackTraceElement[] frames, Thread.State first, Thread.State second) {
        if (frames == null || frames.length == 0) return Path.EMPTY;
        boolean ssl = false;
        for (int i = 0; i < Math.min(frames.length, MAX_FRAMES); i++) {
            StackTraceElement frame = frames[i];
            if (frame == null) continue;
            String type = frame.getClassName(), method = frame.getMethodName();
            if (type.equals("java.lang.Thread") && method.equals("sleep")) return Path.SLEEP;
            if (type.equals("java.lang.Object") && method.equals("wait")) return Path.WAIT;
            if ((type.equals("sun.misc.Unsafe") || type.equals("jdk.internal.misc.Unsafe")
                    || type.equals("java.util.concurrent.locks.LockSupport")) && method.startsWith("park"))
                return Path.PARK;
            if ((type.equals("com.android.org.conscrypt.NativeCrypto") || type.equals("org.conscrypt.NativeCrypto"))
                    && method.equals("SSL_read")
                    || (type.equals("com.android.org.conscrypt.NativeSsl") || type.equals("org.conscrypt.NativeSsl"))
                    && method.equals("read")
                    || (type.equals("com.android.org.conscrypt.ConscryptFileDescriptorSocket$SSLInputStream")
                        || type.equals("org.conscrypt.ConscryptFileDescriptorSocket$SSLInputStream"))
                    && method.equals("read")
                    || (type.equals("com.android.org.conscrypt.ConscryptEngineSocket$SSLInputStream")
                        || type.equals("org.conscrypt.ConscryptEngineSocket$SSLInputStream"))
                    && (method.equals("read") || method.equals("readFromSocket")
                        || method.equals("readUntilDataAvailable") || method.equals("processDataFromSocket"))) ssl = true;
        }
        if (first == Thread.State.BLOCKED && second == first) return Path.MONITOR;
        return ssl ? Path.SSL_READ : Path.OTHER;
    }

    /** A positive state and task birth must accompany a whitelisted kernel wait symbol. */
    static Kernel parseKernel(int nativeTid, String stat, String wchan) {
        if (nativeTid <= 0 || stat == null || wchan == null) return Kernel.missing(Problem.UNSUPPORTED);
        try {
            int open = stat.indexOf('('), close = stat.lastIndexOf(')');
            if (open < 1 || close <= open || !stat.substring(0, open).trim().equals(Integer.toString(nativeTid)))
                return Kernel.missing(Problem.MALFORMED);
            String[] fields = stat.substring(close+1).trim().split("\\s+");
            if (fields.length < 20 || fields[0].length() != 1
                    || "RSDTtZXIWPK".indexOf(fields[0].charAt(0)) < 0)
                return Kernel.missing(Problem.MALFORMED);
            String birthText = fields[19]; // proc stat field22: task starttime, relative to boot.
            if (!birthText.matches("[0-9]{1,19}")) return Kernel.missing(Problem.MALFORMED);
            long birth = Long.parseLong(birthText);
            if (birth <= 0) return Kernel.missing(Problem.MALFORMED);
            String symbol = wchan.trim();
            if (symbol.equals("0")) return Kernel.missing(Problem.ZERO_WCHAN);
            if (!symbol.matches("[A-Za-z_][A-Za-z0-9_.]{0,95}")) return Kernel.missing(Problem.MALFORMED);
            Site site;
            switch (symbol) {
                case "poll_schedule_timeout": case "poll_schedule_timeout.constprop.0":
                    site = Site.POLL_TIMEOUT; break;
                case "do_select": site = Site.SELECT; break;
                case "do_sys_poll": case "do_poll.constprop.0": site = Site.DO_POLL; break;
                case "ep_poll": case "ep_poll.constprop.0": site = Site.EPOLL; break;
                case "futex_wait_queue_me": case "futex_wait": site = Site.FUTEX; break;
                case "sk_wait_data": case "__skb_recv_datagram": case "tcp_recvmsg":
                    site = Site.RECEIVE; break;
                case "hrtimer_nanosleep": case "do_nanosleep": site = Site.SLEEP; break;
                default: site = Site.OTHER;
            }
            return new Kernel(fields[0].charAt(0), birth, site, Problem.NONE);
        } catch (RuntimeException ignored) { return Kernel.missing(Problem.MALFORMED); }
    }

    private static final class ProcKernel implements KernelSource {
        @Override public Kernel read(int nativeTid) {
            if (nativeTid <= 0) return Kernel.missing(Problem.UNSUPPORTED);
            String base = "/proc/self/task/" + nativeTid + "/";
            try { return parseKernel(nativeTid, readBounded(base + "stat", 1024), readBounded(base + "wchan", 128)); }
            catch (IOException | RuntimeException ignored) { return Kernel.missing(Problem.UNSUPPORTED); }
        }
        private String readBounded(String path, int limit) throws IOException {
            byte[] bytes = new byte[limit];
            try (FileInputStream stream = new FileInputStream(path)) {
                int length = stream.read(bytes);
                if (length <= 0 || length == limit) return null;
                return new String(bytes, 0, length, StandardCharsets.US_ASCII);
            }
        }
    }
}
