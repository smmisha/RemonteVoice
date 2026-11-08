package dev.remotevoice.android;

import static org.junit.Assert.*;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;

public final class ReaderWaitSiteTest {
    private static final int TID = 123;
    private static ReaderWaitSite.Kernel kernel(char state, ReaderWaitSite.Site site) {
        return new ReaderWaitSite.Kernel(state, 100, site, ReaderWaitSite.Problem.NONE);
    }
    private static ReaderWaitSite.Observation observation(ReaderWaitSite.Path path, ReaderWaitSite.Kernel value) {
        return new ReaderWaitSite.Observation(value, value, path, true, false);
    }
    private static ReaderWaitSite.Observation sslPoll() {
        return observation(ReaderWaitSite.Path.SSL_READ, kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT));
    }
    private static ReaderWaitSite create(AtomicLong now, ReaderWaitSite.Probe probe) {
        return new ReaderWaitSite(now::get, probe, false);
    }
    private static void confirm(ReaderWaitSite observer, AtomicLong now, long packet, long at) {
        now.set(at); observer.onEntry(packet, at, TID); observer.onExit(packet);
    }
    private static void await(CountDownLatch latch) throws InterruptedException {
        assertTrue("latch timeout", latch.await(3, TimeUnit.SECONDS));
    }

    @Test public void triggerAndConfirmationBoundariesAreIndependent() {
        AtomicLong now = new AtomicLong(); AtomicInteger reads = new AtomicInteger();
        ReaderWaitSite observer = create(now, (thread, tid) -> { reads.incrementAndGet(); return sslPoll(); });
        confirm(observer, now, 1, 0);
        now.set(79_999_999); observer.tick(); assertEquals(0, reads.get());
        now.set(80_000_000); observer.tick(); assertEquals(1, reads.get());
        assertTrue(observer.maximum().contains("wait_confirmed=0"));
        confirm(observer, now, 2, 119_999_999); // pre-trigger sample belongs to an unqualified gap.
        assertTrue(observer.maximum().contains("wait_confirmed=0"));
        now.set(200_000_000); observer.tick();
        confirm(observer, now, 3, 239_999_999);
        String result = observer.maximum();
        assertTrue(result.contains("wait_gap_ns=120000000"));
        assertTrue(result.contains("wait_packet=3 wait_previous_packet=2"));
        assertTrue(result.contains("wait_sample0_start_ns=200000000"));
        assertTrue(result.contains("wait_sample0_evidence=SSL_POLL_OBSERVED"));
        assertTrue(result.contains("wait_unknown=0"));
    }

    @Test public void onlyStablePositiveKernelPollPlusSslPathCanBeAttributed() {
        ReaderWaitSite.Kernel poll = kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
        ReaderWaitSite.Observation[] cases = {
            observation(ReaderWaitSite.Path.SSL_READ, kernel('R', ReaderWaitSite.Site.POLL_TIMEOUT)),
            observation(ReaderWaitSite.Path.SSL_READ, kernel('S', ReaderWaitSite.Site.FUTEX)),
            observation(ReaderWaitSite.Path.SSL_READ, ReaderWaitSite.Kernel.missing(ReaderWaitSite.Problem.UNSUPPORTED)),
            new ReaderWaitSite.Observation(poll, kernel('S', ReaderWaitSite.Site.SELECT), ReaderWaitSite.Path.SSL_READ, true, false),
            new ReaderWaitSite.Observation(poll, new ReaderWaitSite.Kernel('S', 101, ReaderWaitSite.Site.POLL_TIMEOUT,
                    ReaderWaitSite.Problem.NONE), ReaderWaitSite.Path.SSL_READ, true, false),
            new ReaderWaitSite.Observation(poll, poll, ReaderWaitSite.Path.SSL_READ, false, false),
            observation(ReaderWaitSite.Path.OTHER, poll)
        };
        for (ReaderWaitSite.Observation value : cases) {
            AtomicLong now = new AtomicLong();
            ReaderWaitSite observer = create(now, (thread, tid) -> value);
            confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
            confirm(observer, now, 2, 120_000_000);
            assertFalse(observer.maximum(), observer.maximum().contains("evidence=SSL_POLL_OBSERVED"));
            assertTrue(observer.maximum().contains("wait_unknown=1"));
        }
    }

    @Test public void capturesExactRegisteredThreadAndPassedNativeTid() {
        AtomicLong now = new AtomicLong(); Thread registered = Thread.currentThread();
        AtomicReference<Thread> observed = new AtomicReference<>(); AtomicInteger tid = new AtomicInteger();
        ReaderWaitSite observer = create(now, (target, id) -> { observed.set(target); tid.set(id); return sslPoll(); });
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        confirm(observer, now, 2, 150_000_000);
        assertSame(registered, observed.get()); assertEquals(TID, tid.get());
        assertTrue(observer.maximum().contains("wait_native_tid=123"));
        assertFalse(observer.maximum().contains("java.lang"));
    }

    @Test public void nativeRebindInvalidIdPacketDiscontinuityAndClockRegressionDoNotLinkGaps() {
        for (int variant = 0; variant < 4; variant++) {
            AtomicLong now = new AtomicLong(); ReaderWaitSite observer = create(now, (thread, tid) -> sslPoll());
            confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
            if (variant == 0) observer.onEntry(2, 150_000_000, TID+1);
            if (variant == 1) observer.onEntry(2, 150_000_000, 0);
            if (variant == 2) observer.onEntry(3, 150_000_000, TID);
            if (variant == 3) observer.onEntry(2, -1, TID);
            assertTrue(observer.maximum().contains("wait_confirmed=0"));
        }
    }

    @Test public void differentJavaThreadNeverReusesEvenTheSameNativeTid() throws Exception {
        AtomicLong now = new AtomicLong(); ReaderWaitSite observer = create(now, (thread, tid) -> sslPoll());
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        Thread replacement = new Thread(() -> observer.onEntry(2, 150_000_000, TID));
        replacement.start(); replacement.join(2000); assertFalse(replacement.isAlive());
        assertTrue(observer.maximum().contains("wait_confirmed=0"));
    }

    @Test public void callbackCrossingProbeIsStaleAndOnEntryDoesNotWaitForProbe() throws Exception {
        AtomicLong now = new AtomicLong(); CountDownLatch entered = new CountDownLatch(1), release = new CountDownLatch(1);
        ReaderWaitSite observer = create(now, (thread, tid) -> {
            entered.countDown();
            try { await(release); } catch (InterruptedException error) { throw new AssertionError(error); }
            return sslPoll();
        });
        confirm(observer, now, 1, 0); now.set(80_000_000);
        Thread probe = new Thread(observer::tick); probe.start();
        try {
            await(entered);
            confirm(observer, now, 2, 150_000_000);
            assertTrue(observer.maximum().contains("wait_samples=0 wait_unknown=1"));
        } finally { release.countDown(); probe.join(2000); }
        assertFalse(probe.isAlive());
        assertTrue(observer.maximum().contains("wait_stale=1"));
        assertFalse(observer.maximum().contains("evidence=SSL_POLL_OBSERVED"));
    }

    @Test public void limitsAttemptsPerPendingEpochAndAcrossCapture() {
        AtomicLong now = new AtomicLong(); AtomicInteger calls = new AtomicInteger();
        ReaderWaitSite observer = create(now, (thread, tid) -> { calls.incrementAndGet(); return sslPoll(); });
        confirm(observer, now, 1, 0);
        for (long time : new long[] {80, 80, 99, 100, 120, 140, 160}) {
            now.set(time*1_000_000); observer.tick();
        }
        assertEquals(3, calls.get());
        confirm(observer, now, 2, 200_000_000);
        for (int packet = 2; packet < 35; packet++) {
            long entry = (packet-1)*200_000_000L;
            for (int sample = 0; sample < 3; sample++) { now.set(entry+80_000_000+sample*20_000_000); observer.tick(); }
            confirm(observer, now, packet+1, entry+200_000_000);
        }
        assertEquals(64, calls.get());
        String result = observer.maximum();
        assertTrue(result.contains("wait_budget_exhausted=1"));
        assertTrue(result.contains("wait_samples=3"));
        assertTrue(result.length() < 3000);
    }

    @Test public void callbackTimestampBeforePublicationStillRejectsCrossBoundarySample() {
        AtomicLong now = new AtomicLong();
        ReaderWaitSite observer = create(now, (thread, tid) -> { now.addAndGet(5_000_000); return sslPoll(); });
        confirm(observer, now, 1, 0);
        now.set(125_000_000); observer.tick(); //125..130ms; callback's entry was already captured at128ms.
        observer.onEntry(2, 128_000_000, TID);
        assertTrue(observer.maximum().contains("wait_gap_ns=128000000"));
        assertTrue(observer.maximum().contains("wait_samples=0 wait_unknown=1"));
        assertTrue(observer.maximum().contains("wait_stale=1"));
        assertTrue(observer.maximum().contains("wait_last_discard=BOUNDARY"));
    }

    @Test public void longProbeDisablesAllFurtherStackAttemptsAndNeverAttributesIt() {
        AtomicLong now = new AtomicLong(); AtomicInteger calls = new AtomicInteger();
        ReaderWaitSite observer = create(now, (thread, tid) -> {
            calls.incrementAndGet(); now.addAndGet(10_000_001); return sslPoll();
        });
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        now.set(110_000_000); observer.tick(); confirm(observer, now, 2, 150_000_000);
        now.set(250_000_000); observer.tick();
        assertEquals(1, calls.get());
        assertTrue(observer.maximum().contains("wait_measuring_bias=1"));
        assertTrue(observer.maximum().contains("problem=MEASURING_BIAS"));
        assertFalse(observer.maximum().contains("evidence=SSL_POLL_OBSERVED"));
    }

    @Test public void exceptionsAndEmptySourcesAreUnknownAndDoNotLeakMessages() {
        AtomicLong now = new AtomicLong();
        ReaderWaitSite observer = create(now, (thread, tid) -> { throw new SecurityException("https://secret/token"); });
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick(); confirm(observer, now, 2, 120_000_000);
        assertTrue(observer.maximum().contains("problem=PROBE_EXCEPTION"));
        assertFalse(observer.maximum().contains("secret"));
        assertTrue(observer.maximum().contains("wait_unknown=1"));
        ReaderWaitSite missing = create(now, (thread, tid) -> null);
        confirm(missing, now, 1, 120_000_000); now.set(200_000_000); missing.tick();
        confirm(missing, now, 2, 240_000_000);
        assertTrue(missing.maximum().contains("problem=PROBE_EXCEPTION"));
    }

    @Test public void deadlineStopsCaptureAndMissingSampleDoesNotMeanNoWait() {
        AtomicLong now = new AtomicLong(); AtomicInteger calls = new AtomicInteger();
        ReaderWaitSite observer = create(now, (thread, tid) -> { calls.incrementAndGet(); return sslPoll(); });
        confirm(observer, now, 1, 0); confirm(observer, now, 2, 150_000_000);
        assertTrue(observer.maximum().contains("wait_samples=0 wait_unknown=1"));
        now.set(ReaderWaitSite.DEADLINE_NS); observer.tick();
        assertEquals(0, calls.get()); assertTrue(observer.maximum().contains("wait_deadline=1"));
        assertTrue(observer.maximum().contains("wait_stopped=1"));
    }

    @Test public void stopAndNonpositiveAwaitNeverWaitForBlockedProbeOrPublishLate() throws Exception {
        AtomicLong now = new AtomicLong(); CountDownLatch entered = new CountDownLatch(1), release = new CountDownLatch(1);
        ReaderWaitSite observer = new ReaderWaitSite(now::get, (thread, tid) -> {
            entered.countDown();
            boolean done = false;
            while (!done) {
                try { done = release.await(3, TimeUnit.SECONDS); }
                catch (InterruptedException ignored) { /* Model a stack API with no cancellation guarantee. */ }
            }
            return sslPoll();
        }, true);
        try {
            confirm(observer, now, 1, 0); now.set(80_000_000); await(entered);
            long started = System.nanoTime(); observer.requestStop();
            assertFalse(observer.awaitStopped(0)); assertFalse(observer.awaitStopped(-1));
            assertTrue(System.nanoTime()-started < 100_000_000);
            assertFalse(observer.awaitStopped(1));
            assertTrue(observer.maximum().contains("cleanup_pending=1"));
            observer.onEntry(2, 150_000_000, TID);
        } finally { release.countDown(); observer.requestStop(); assertTrue(observer.awaitStopped(2000)); }
        assertTrue(observer.maximum().contains("observer_alive=0 cleanup_pending=0"));
        assertTrue(observer.maximum().contains("wait_confirmed=0"));
        assertTrue(observer.maximum().contains("wait_stale=1"));
    }

    @Test public void stoppedMaximumCannotChangeOnLaterEntryExitOrTick() {
        AtomicLong now = new AtomicLong(); AtomicInteger captures = new AtomicInteger();
        ReaderWaitSite observer = create(now, (thread, tid) -> { captures.incrementAndGet(); return sslPoll(); });
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        confirm(observer, now, 2, 150_000_000);
        observer.requestStop(); String frozen = observer.maximum();
        assertTrue(frozen.contains("wait_packet=2"));
        assertTrue(frozen.contains("wait_sample0_evidence=SSL_POLL_OBSERVED"));
        now.set(1_000_000_000); observer.onEntry(3, now.get(), TID); observer.onExit(3); observer.tick();
        observer.requestStop();
        assertEquals(frozen, observer.maximum());
        assertEquals(1, captures.get()); assertTrue(observer.awaitStopped(0));
    }

    private static String stat(int tid, String state, String birth) {
        return tid + " (name with ) spaces) " + state + " 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 " + birth + " 0\n";
    }

    @Test public void procParserChecksIdentityStateBirthZeroMalformedAndWhitelistsOnlySymbols() {
        ReaderWaitSite.Kernel good = ReaderWaitSite.parseKernel(TID, stat(TID, "S", "100"), "poll_schedule_timeout\n");
        assertEquals(ReaderWaitSite.Site.POLL_TIMEOUT, good.site); assertEquals(100, good.birth);
        assertEquals(ReaderWaitSite.Problem.NONE, good.problem);
        assertEquals(ReaderWaitSite.Problem.ZERO_WCHAN,
                ReaderWaitSite.parseKernel(TID, stat(TID, "S", "100"), "0\n").problem);
        for (String value : new String[] {"", "http://token", "poll timeout", "poll\nsecret", "1234"})
            assertEquals(ReaderWaitSite.Problem.MALFORMED,
                    ReaderWaitSite.parseKernel(TID, stat(TID, "S", "100"), value).problem);
        for (String value : new String[] {"", "123 (x) S 0", stat(TID+1, "S", "100"), stat(TID, "?", "100"),
                stat(TID, "S", "-1"), stat(TID, "S", "9223372036854775808")})
            assertEquals(ReaderWaitSite.Problem.MALFORMED, ReaderWaitSite.parseKernel(TID, value, "ep_poll").problem);
        assertEquals(ReaderWaitSite.Problem.UNSUPPORTED, ReaderWaitSite.parseKernel(0, null, null).problem);
        assertEquals(ReaderWaitSite.Site.OTHER,
                ReaderWaitSite.parseKernel(TID, stat(TID, "S", "100"), "unrecognized_symbol").site);
    }

    @Test public void javaClassifierIsBoundedAndNeverPreservesArbitraryFrames() {
        StackTraceElement ssl = new StackTraceElement("com.android.org.conscrypt.NativeCrypto", "SSL_read", "secret", -2);
        assertEquals(ReaderWaitSite.Path.SSL_READ,
                ReaderWaitSite.classify(new StackTraceElement[] {ssl}, Thread.State.RUNNABLE, Thread.State.RUNNABLE));
        assertEquals(ReaderWaitSite.Path.MONITOR,
                ReaderWaitSite.classify(new StackTraceElement[] {ssl}, Thread.State.BLOCKED, Thread.State.BLOCKED));
        StackTraceElement[] tooDeep = new StackTraceElement[33]; tooDeep[32] = ssl;
        assertEquals(ReaderWaitSite.Path.OTHER,
                ReaderWaitSite.classify(tooDeep, Thread.State.RUNNABLE, Thread.State.RUNNABLE));
        assertEquals(ReaderWaitSite.Path.EMPTY, ReaderWaitSite.classify(null, Thread.State.NEW, Thread.State.NEW));
    }

    @Test public void reportsTruncationButDoesNotDumpFrames() {
        AtomicLong now = new AtomicLong();
        ReaderWaitSite observer = create(now, (thread, tid) -> {
            ReaderWaitSite.Kernel poll = kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
            return new ReaderWaitSite.Observation(poll, poll, ReaderWaitSite.Path.OTHER, true, true);
        });
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick(); confirm(observer, now, 2, 120_000_000);
        assertTrue(observer.maximum().contains("wait_truncated=1"));
        assertTrue(observer.maximum().contains("wait_sample0_truncated=1"));
    }

    @Test public void engineSocketInputReadPathsUseExactClassesAndMethodsOnly() {
        for (String prefix : new String[] {"org.conscrypt.", "com.android.org.conscrypt."}) {
            String input = prefix + "ConscryptEngineSocket$SSLInputStream";
            for (String method : new String[] {"read", "readFromSocket", "readUntilDataAvailable", "processDataFromSocket"}) {
                StackTraceElement frame = new StackTraceElement(input, method, "ignored", 1);
                assertEquals(input+"."+method, ReaderWaitSite.Path.SSL_READ,
                        ReaderWaitSite.classify(new StackTraceElement[] {frame}, Thread.State.RUNNABLE, Thread.State.RUNNABLE));
            }
            for (String method : new String[] {"write", "writeInternal", "flush", "unknownRead", "readFromSocketExtra"}) {
                StackTraceElement frame = new StackTraceElement(input, method, "ignored", 1);
                assertEquals(ReaderWaitSite.Path.OTHER,
                        ReaderWaitSite.classify(new StackTraceElement[] {frame}, Thread.State.RUNNABLE, Thread.State.RUNNABLE));
            }
            for (String type : new String[] {prefix+"ConscryptEngineSocket$SSLOutputStream",
                    prefix+"UnknownConscryptEngineSocket$SSLInputStream", prefix+"ConscryptEngineSocket$SSLInputStreamExtra"}) {
                StackTraceElement frame = new StackTraceElement(type, "read", "ignored", 1);
                assertEquals(ReaderWaitSite.Path.OTHER,
                        ReaderWaitSite.classify(new StackTraceElement[] {frame}, Thread.State.RUNNABLE, Thread.State.RUNNABLE));
            }
        }
    }

    @Test public void realOwnThreadsDistinguishSleepMonitorAndCpuWithoutInventingKernelEvidence() throws Exception {
        for (int mode = 0; mode < 3; mode++) checkRealThread(mode);
    }

    @Test public void discardedBiasedAttemptRetainsItsOwnStageReceiptWithoutGapAttribution() {
        AtomicLong now = new AtomicLong(); AtomicInteger kernels = new AtomicInteger();
        AtomicReference<ReaderWaitSite> reference = new AtomicReference<>();
        ReaderWaitSite.Probe probe = new ReaderWaitSite.OwnProbe(tid -> {
            now.addAndGet(kernels.incrementAndGet() == 1 ? 1_000_000 : 2_000_000);
            return kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
        }, thread -> {
            now.addAndGet(11_000_000);
            reference.get().onEntry(2, 90_000_000, TID); // The capture crossed this entry.
            return new StackTraceElement[] {
                new StackTraceElement("org.conscrypt.NativeCrypto", "SSL_read", "never-retained", -2)
            };
        }, now::get);
        ReaderWaitSite observer = create(now, probe); reference.set(observer);
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        String result = observer.maximum();
        assertTrue(result, result.contains("wait_confirmed=0"));
        assertTrue(result.contains("wait_samples=0 wait_unknown=1"));
        assertTrue(result.contains("wait_last_present=1 wait_last_gap_linked=0 wait_last_packet=1"));
        assertTrue(result.contains("wait_last_tid=123 wait_last_epoch=1"));
        assertTrue(result.contains("wait_last_start_ns=80000000 wait_last_end_ns=94000000"));
        assertTrue(result.contains("wait_last_discard=STALE wait_last_problem=MEASURING_BIAS"));
        assertTrue(result.contains("wait_last_bias=1 wait_last_stale=1"));
        assertTrue(result.contains("wait_last_stage_mask=7 wait_last_stage_clock_valid=1"));
        assertTrue(result.contains("wait_last_pre_start_ns=80000000 wait_last_pre_end_ns=81000000"));
        assertTrue(result.contains("wait_last_stack_start_ns=81000000 wait_last_stack_end_ns=92000000"));
        assertTrue(result.contains("wait_last_post_start_ns=92000000 wait_last_post_end_ns=94000000"));
        assertFalse(result.contains("SSL_POLL_OBSERVED")); assertFalse(result.contains("never-retained"));
        now.set(200_000_000); observer.tick(); assertEquals(2, kernels.get());
    }

    @Test public void procOnlyKeepsKernelChannelButCannotInventAnSslPath() {
        AtomicLong now = new AtomicLong(); AtomicInteger calls = new AtomicInteger();
        ReaderWaitSite observer = create(now, new ReaderWaitSite.OwnProbe(tid -> {
            assertEquals(TID, tid); calls.incrementAndGet(); now.addAndGet(1_000_000);
            return kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
        }, null, now::get));
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        confirm(observer, now, 2, 120_000_000);
        String result = observer.maximum();
        assertEquals(2, calls.get());
        assertTrue(result.contains("wait_sample0_evidence=UNKNOWN wait_sample0_path=EMPTY"));
        assertTrue(result.contains("wait_sample0_kernel=POLL_TIMEOUT"));
        assertTrue(result.contains("wait_last_kernel=POLL_TIMEOUT"));
        assertTrue(result.contains("wait_last_stage_mask=5 wait_last_stage_clock_valid=1"));
        assertTrue(result.contains("wait_last_stack_start_ns=-1 wait_last_stack_end_ns=-1"));
        assertFalse(result.contains("SSL_POLL_OBSERVED")); assertTrue(result.length() < 3000);
    }

    @Test public void procOnlyFactoryNeverAsksRegisteredReaderForStack() throws Exception {
        AtomicLong now = new AtomicLong(); AtomicInteger stackReads = new AtomicInteger();
        CountDownLatch entered = new CountDownLatch(1), release = new CountDownLatch(1);
        ReaderWaitSite observer = ReaderWaitSite.createProcOnly(now::get);
        Thread reader = new Thread(() -> {
            observer.onEntry(1, 0, TID); observer.onExit(1); entered.countDown();
            try { await(release); } catch (InterruptedException error) { Thread.currentThread().interrupt(); }
            observer.onEntry(2, 150_000_000, TID);
        }) {
            @Override public StackTraceElement[] getStackTrace() {
                stackReads.incrementAndGet(); throw new AssertionError("proc-only called stack API");
            }
        };
        try {
            reader.start(); await(entered); now.set(80_000_000);
            long deadline = System.nanoTime()+2_000_000_000L;
            while (!observer.maximum().contains("wait_last_present=1") && System.nanoTime() < deadline) Thread.yield();
            assertTrue(observer.maximum().contains("wait_attempts=1"));
            assertTrue(observer.maximum().contains("wait_last_present=1"));
        } finally {
            release.countDown(); reader.join(2000); observer.requestStop(); assertTrue(observer.awaitStopped(2000));
        }
        assertFalse(reader.isAlive()); assertEquals(0, stackReads.get());
        assertFalse(observer.maximum().contains("SSL_POLL_OBSERVED"));
    }

    @Test public void sourceExceptionAndClockFailureRetainPartialExplicitUnknownStages() {
        AtomicLong now = new AtomicLong();
        ReaderWaitSite observer = create(now, new ReaderWaitSite.OwnProbe(tid -> {
            now.addAndGet(1_000_000); return kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
        }, thread -> { now.addAndGet(2_000_000); throw new SecurityException("not-retained"); }, now::get));
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        assertTrue(observer.maximum().contains("wait_last_problem=PROBE_EXCEPTION"));
        assertTrue(observer.maximum().contains("wait_last_stage_mask=3 wait_last_stage_clock_valid=1"));
        assertTrue(observer.maximum().contains("wait_last_post_start_ns=-1 wait_last_post_end_ns=-1"));
        assertFalse(observer.maximum().contains("not-retained"));
        AtomicInteger clockCalls = new AtomicInteger();
        ReaderWaitSite clockFailure = create(now, new ReaderWaitSite.OwnProbe(
                tid -> kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT), null,
                () -> { if (clockCalls.incrementAndGet() == 2) throw new IllegalStateException(); return now.get(); }));
        confirm(clockFailure, now, 1, now.get()); now.addAndGet(80_000_000); clockFailure.tick();
        assertTrue(clockFailure.maximum().contains("wait_last_problem=CLOCK"));
        assertTrue(clockFailure.maximum().contains("wait_last_stage_clock_valid=0"));
        assertFalse(clockFailure.maximum().contains("SSL_POLL_OBSERVED"));
    }

    private static void checkRealThread(int mode) throws Exception {
        AtomicLong now = new AtomicLong(); Object monitor = new Object();
        CountDownLatch registered = new CountDownLatch(1);
        AtomicBoolean released = new AtomicBoolean(); AtomicReference<Throwable> failure = new AtomicReference<>();
        ReaderWaitSite observer = create(now, new ReaderWaitSite.OwnProbe(
                tid -> ReaderWaitSite.Kernel.missing(ReaderWaitSite.Problem.UNSUPPORTED), Thread::getStackTrace, now::get));
        Thread reader = new Thread(() -> {
            try {
                observer.onEntry(1, 0, TID); observer.onExit(1); registered.countDown();
                if (mode == 0) {
                    while (!released.get()) Thread.sleep(5);
                } else if (mode == 1) {
                    synchronized (monitor) { /* Deliberate monitor contention controlled by the test. */ }
                } else {
                    while (!released.get()) Thread.onSpinWait();
                }
                now.set(150_000_000); observer.onEntry(2, now.get(), TID); observer.onExit(2);
            } catch (Throwable error) { failure.set(error); }
        });
        try {
            synchronized (monitor) {
                reader.start(); await(registered);
                Thread.State expected = mode == 0 ? Thread.State.TIMED_WAITING
                        : mode == 1 ? Thread.State.BLOCKED : Thread.State.RUNNABLE;
                long deadline = System.nanoTime()+2_000_000_000L;
                while (reader.getState() != expected && System.nanoTime() < deadline) Thread.yield();
                assertEquals(expected, reader.getState());
                now.set(80_000_000); observer.tick(); released.set(true);
            }
        } finally { released.set(true); reader.join(2000); observer.requestStop(); }
        assertFalse(reader.isAlive()); assertNull(failure.get());
        String result = observer.maximum();
        String expectedPath = mode == 0 ? "SLEEP" : mode == 1 ? "MONITOR" : "OTHER";
        assertTrue(result, result.contains("wait_sample0_path="+expectedPath));
        assertTrue(result.contains("wait_unknown=1"));
        assertFalse(result.contains("evidence=SSL_POLL_OBSERVED"));
    }

    @Test public void cpuCountersBracketOnlyPreAndPostOnTheCurrentCaptureThread() {
        AtomicLong now = new AtomicLong(); AtomicInteger cpuCalls = new AtomicInteger(), stacks = new AtomicInteger();
        long[] cpu = {100, 300, 400, 900}; Thread capture = Thread.currentThread();
        ReaderWaitSite observer = create(now, new ReaderWaitSite.OwnProbe(tid -> {
            now.addAndGet(1000); return kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
        }, target -> { stacks.incrementAndGet(); now.addAndGet(5000); return new StackTraceElement[0]; },
                now::get, () -> { assertSame(capture, Thread.currentThread()); return cpu[cpuCalls.getAndIncrement()]; }));
        confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        String result = observer.maximum();
        assertEquals(4, cpuCalls.get()); assertEquals(1, stacks.get());
        assertTrue(result.contains("wait_last_observer_java_id="+capture.getId()));
        assertTrue(result.contains("wait_last_pre_cpu_ns=200 wait_last_pre_cpu_valid=1 wait_last_pre_cpu_problem=NONE"));
        assertTrue(result.contains("wait_last_post_cpu_ns=500 wait_last_post_cpu_valid=1 wait_last_post_cpu_problem=NONE"));
        assertFalse(result.contains("stack_cpu"));
    }

    @Test public void cpuFailuresStayIndependentOfCaptureAndZeroIsAValidMeasurement() {
        for (int mode = 0; mode < 6; mode++) {
            final int selected = mode;
            AtomicLong now = new AtomicLong(); AtomicInteger calls = new AtomicInteger();
            java.util.function.LongSupplier cpu = mode == 0 ? null : () -> {
                int call = calls.getAndIncrement();
                if (selected == 1) return -5;
                if (selected == 2) throw new IllegalStateException("secret");
                if (selected == 3) return call % 2 == 0 ? 100 : 99;
                if (selected == 4) return call*10_000L;
                return 0;
            };
            ReaderWaitSite observer = create(now, new ReaderWaitSite.OwnProbe(tid -> {
                now.addAndGet(1000); return kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
            }, null, now::get, cpu));
            confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
            confirm(observer, now, 2, 120_000_000);
            String result = observer.maximum();
            String expected = mode <= 1 ? "UNSUPPORTED" : mode == 2 ? "EXCEPTION"
                    : mode == 3 ? "RESET" : mode == 4 ? "OVERSHOOT" : "NONE";
            assertTrue(result, result.contains("wait_last_pre_cpu_problem="+expected));
            assertTrue(result.contains("wait_last_post_cpu_problem="+expected));
            assertTrue(result.contains("wait_samples=1"));
            assertTrue(result.contains("wait_measuring_bias=0"));
            assertFalse(result.contains("secret"));
            if (mode == 5) assertTrue(result.contains("wait_last_pre_cpu_ns=0 wait_last_pre_cpu_valid=1"));
            else assertTrue(result.contains("wait_last_pre_cpu_ns=-1 wait_last_pre_cpu_valid=0"));
        }
    }

    @Test public void slowStaleWallProbeStillDisablesCaptureDespiteSmallCpuDelta() {
        AtomicLong now = new AtomicLong(), cpu = new AtomicLong();
        AtomicInteger calls = new AtomicInteger(); AtomicReference<ReaderWaitSite> ref = new AtomicReference<>();
        ReaderWaitSite observer = create(now, new ReaderWaitSite.OwnProbe(tid -> {
            now.addAndGet(6_000_000);
            if (calls.incrementAndGet() == 2) ref.get().onEntry(2, 90_000_000, TID);
            return kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
        }, null, now::get, () -> cpu.addAndGet(100)));
        ref.set(observer); confirm(observer, now, 1, 0); now.set(80_000_000); observer.tick();
        String result = observer.maximum();
        assertTrue(result.contains("wait_measuring_bias=1"));
        assertTrue(result.contains("wait_last_discard=STALE"));
        assertTrue(result.contains("wait_last_pre_cpu_ns=100 wait_last_pre_cpu_valid=1"));
        assertTrue(result.contains("wait_last_post_cpu_ns=100 wait_last_post_cpu_valid=1"));
        assertTrue(result.contains("wait_samples=0 wait_unknown=1"));
        now.set(200_000_000); observer.tick(); assertEquals(2, calls.get());
    }

    @Test public void cpuIdentityAndWallClockFailuresAreExplicit() throws Exception {
        AtomicLong now = new AtomicLong();
        ReaderWaitSite.Timings timing = new ReaderWaitSite.Timings(() -> 10);
        timing.begin(0, now::get);
        Thread other = new Thread(() -> { now.set(1000); timing.end(0, now::get); });
        other.start(); other.join(2000); assertFalse(other.isAlive());
        assertTrue(timing.fields(0, 1000).contains("wait_last_pre_cpu_problem=THREAD_CHANGED"));
        ReaderWaitSite.Timings regression = new ReaderWaitSite.Timings(() -> 10);
        now.set(1000); regression.begin(0, now::get); now.set(999); regression.end(0, now::get);
        assertTrue(regression.fields(0, 2000).contains("wait_last_pre_cpu_problem=CLOCK"));
    }

    @Test public void threeSamplesPlusCpuReceiptFitBoundedScalarLog() {
        AtomicLong now = new AtomicLong(), cpu = new AtomicLong();
        ReaderWaitSite observer = create(now, new ReaderWaitSite.OwnProbe(tid -> {
            now.addAndGet(1000); return kernel('S', ReaderWaitSite.Site.POLL_TIMEOUT);
        }, null, now::get, () -> cpu.addAndGet(100)));
        confirm(observer, now, 1, 0);
        for (int i = 0; i < 3; i++) { now.set(80_000_000+i*20_000_000L); observer.tick(); }
        confirm(observer, now, 2, 150_000_000); observer.requestStop();
        String result = observer.maximum();
        assertTrue(result.contains("wait_samples=3"));
        assertTrue(result.contains("wait_last_post_cpu_ns=100"));
        assertTrue("summary bytes="+result.getBytes(java.nio.charset.StandardCharsets.US_ASCII).length,
                result.getBytes(java.nio.charset.StandardCharsets.US_ASCII).length < 3000);
    }
}
