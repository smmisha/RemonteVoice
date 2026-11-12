package dev.remontevoice.android;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;
import static org.junit.Assert.*;

public final class PlaybackQueueHandoffTest {
    private static void waiting(Thread producer) throws Exception {
        long deadline=System.nanoTime()+1_000_000_000L;
        while(producer.getState()!=Thread.State.TIMED_WAITING && System.nanoTime()<deadline) Thread.sleep(1);
        assertEquals(Thread.State.TIMED_WAITING,producer.getState());
    }

    @Test public void liveWriterMayTakeLongerThanOneWaitSliceWithoutDiscardingPcm() throws Exception {
        ArrayBlockingQueue<float[]> queue=new ArrayBlockingQueue<>(1);
        float[] first={1},second={2};queue.add(first);
        AtomicReference<Boolean> accepted=new AtomicReference<>();
        AtomicReference<Throwable> error=new AtomicReference<>();
        Thread producer=new Thread(()-> {
            try { accepted.set(PlaybackQueueHandoff.offer(queue,second,()->true,()->true)); }
            catch(Throwable failure) { error.set(failure); }
        });
        producer.start();
        try {
            waiting(producer);
            Thread.sleep(170); // Reproduces measured native writes109..127ms beyond the old100ms handoff.
            assertTrue("A healthy slow writer must not trigger the discard fallback",producer.isAlive());
            assertSame(first,queue.poll());
            producer.join(1000);
            assertFalse(producer.isAlive());assertNull(error.get());assertEquals(Boolean.TRUE,accepted.get());
            assertSame(second,queue.poll());
        } finally { producer.interrupt();producer.join(1000); }
    }

    @Test public void cancellationStopsWaitingAndPreservesExistingPacket() throws Exception {
        ArrayBlockingQueue<float[]> queue=new ArrayBlockingQueue<>(1);float[] first={1};queue.add(first);
        AtomicBoolean running=new AtomicBoolean(true);AtomicReference<Boolean> accepted=new AtomicReference<>();
        Thread producer=new Thread(()-> {
            try { accepted.set(PlaybackQueueHandoff.offer(queue,new float[]{2},running::get,()->true)); }
            catch(InterruptedException error) { Thread.currentThread().interrupt(); }
        });
        producer.start();
        try {
            waiting(producer);running.set(false);producer.join(500);
            assertFalse(producer.isAlive());assertEquals(Boolean.FALSE,accepted.get());assertSame(first,queue.peek());
        } finally { producer.interrupt();producer.join(1000); }
    }

    @Test public void staleWriterRaisesInsteadOfDiscardingBufferedPcm() {
        ArrayBlockingQueue<float[]> queue=new ArrayBlockingQueue<>(1);float[] first={1};queue.add(first);
        assertThrows(IllegalStateException.class,()->PlaybackQueueHandoff.offer(queue,new float[]{2},()->true,()->false));
        assertSame(first,queue.peek());assertEquals(1,queue.size());
    }

    @Test public void interruptionPropagatesWithoutRemovingBufferedPcm() throws Exception {
        ArrayBlockingQueue<float[]> queue=new ArrayBlockingQueue<>(1);float[] first={1};queue.add(first);
        AtomicReference<Throwable> error=new AtomicReference<>();
        Thread producer=new Thread(()-> {
            try { PlaybackQueueHandoff.offer(queue,new float[]{2},()->true,()->true); }
            catch(Throwable failure) { error.set(failure); }
        });
        producer.start();
        try {
            waiting(producer);producer.interrupt();producer.join(500);
            assertFalse(producer.isAlive());assertTrue(error.get() instanceof InterruptedException);assertSame(first,queue.peek());
        } finally { producer.interrupt();producer.join(1000); }
    }
}
