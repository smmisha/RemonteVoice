package dev.remontevoice.android;

import org.junit.Test;
import static org.junit.Assert.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public final class PlaybackPrimingGateTest {
    @Test public void partialAndSpuriousArrivalsCannotStartPlaybackEarly() throws Exception {
        PlaybackPrimingGate gate=new PlaybackPrimingGate();
        AtomicInteger queued=new AtomicInteger();
        CountDownLatch entered=new CountDownLatch(1);
        ExecutorService worker=Executors.newSingleThreadExecutor();
        try {
            Future<?> ready=worker.submit(()->{
                try { gate.await(32,()->true,()->{ entered.countDown();return queued.get(); }); }
                catch (InterruptedException error) { Thread.currentThread().interrupt(); }
            });
            assertTrue(entered.await(2,TimeUnit.SECONDS));
            gate.signal();
            queued.set(31);gate.signal();
            assertThrows(TimeoutException.class,()->ready.get(30,TimeUnit.MILLISECONDS));
            queued.set(32);gate.signal();
            ready.get(2,TimeUnit.SECONDS);
        } finally { worker.shutdownNow();assertTrue(worker.awaitTermination(2,TimeUnit.SECONDS)); }
    }

    @Test public void stopSignalReleasesPendingPrimingWithoutMorePackets() throws Exception {
        PlaybackPrimingGate gate=new PlaybackPrimingGate();
        AtomicBoolean running=new AtomicBoolean(true);
        CountDownLatch entered=new CountDownLatch(1),finished=new CountDownLatch(1);
        Thread worker=new Thread(()->{
            try { gate.await(32,running::get,()->{ entered.countDown();return 0; }); }
            catch (InterruptedException error) { Thread.currentThread().interrupt(); }
            finally { finished.countDown(); }
        });
        worker.start();
        try {
            assertTrue(entered.await(2,TimeUnit.SECONDS));
            running.set(false);gate.signal();
            assertTrue(finished.await(2,TimeUnit.SECONDS));
        } finally { worker.interrupt();worker.join(2000);assertFalse(worker.isAlive()); }
    }

    @Test public void interruptionPropagatesForRealPlaybackShutdown() throws Exception {
        PlaybackPrimingGate gate=new PlaybackPrimingGate();
        CountDownLatch entered=new CountDownLatch(1),interrupted=new CountDownLatch(1);
        Thread worker=new Thread(()->{
            try { gate.await(32,()->true,()->{ entered.countDown();return 0; }); }
            catch (InterruptedException expected) { interrupted.countDown(); }
        });
        worker.start();
        try {
            assertTrue(entered.await(2,TimeUnit.SECONDS));worker.interrupt();
            assertTrue(interrupted.await(2,TimeUnit.SECONDS));
        } finally { worker.interrupt();worker.join(2000);assertFalse(worker.isAlive()); }
    }

    @Test public void alreadyFilledOrStoppedSessionDoesNotWait() throws Exception {
        PlaybackPrimingGate gate=new PlaybackPrimingGate();
        gate.await(32,()->true,()->32);
        gate.await(32,()->false,()->0);
        assertThrows(IllegalArgumentException.class,()->gate.await(0,()->true,()->0));
        assertThrows(IllegalArgumentException.class,()->gate.await(41,()->true,()->0));
    }
}
