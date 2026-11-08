package dev.remotevoice.android;

import org.junit.Test;
import static org.junit.Assert.*;

public final class PlaybackWriteBudgetTest {
    @Test public void pausedWriteStopsExactlyAtNonPacketAlignedTarget() {
        assertEquals(2,PlaybackWriteBudget.samples(960,2,false,7680,7681));
        assertEquals(0,PlaybackWriteBudget.samples(960,2,false,7681,7681));
        assertEquals(960,PlaybackWriteBudget.samples(960,2,true,0,7681));
    }
    @Test public void partialWritesAndReprimeCannotCrossPausedCapacity() {
        for (int target:new int[]{1,479,480,481,7681,16000}) {
            for (int restart=0;restart<2;restart++) {
                int primed=0;
                while(primed<target) {
                    int samples=PlaybackWriteBudget.samples(960,2,false,primed,target);
                    int partial=Math.min(samples,34); // Simulated channel-aligned partial native write.
                    assertTrue(partial>0);primed+=partial/2;assertTrue(primed<=target);
                }
                assertEquals(target,primed);
            }
        }
    }
    @Test public void rejectsUnalignedOrInvalidRequests() {
        assertThrows(IllegalArgumentException.class,()->PlaybackWriteBudget.samples(3,2,false,0,10));
        assertThrows(IllegalArgumentException.class,()->PlaybackWriteBudget.samples(960,0,false,0,10));
        assertThrows(IllegalArgumentException.class,()->PlaybackWriteBudget.samples(960,2,false,-1,10));
    }

    @Test public void zeroAndPartialNativeWritesPreserveRemainingSamples() {
        int offset=0;
        for (int result:new int[]{0,120,0,360,480}) {
            int accepted=PlaybackWriteBudget.acceptedSamples(result,960-offset,2);
            offset+=accepted;
        }
        assertEquals(960,offset);
    }

    @Test public void nativeErrorsAndInvalidCountsAreRejected() {
        for (int result:new int[]{-1,-3,-6,962,3}) {
            assertThrows(IllegalStateException.class,()->PlaybackWriteBudget.acceptedSamples(result,960,2));
        }
        assertThrows(IllegalArgumentException.class,()->PlaybackWriteBudget.acceptedSamples(0,0,2));
    }

    @Test public void zeroWritesDoNotExtendAudioProgressDeadline() {
        long[] clock={0};
        AudioProgress progress=new AudioProgress(5000,()->clock[0]);
        progress.advanced();
        for(int retry=0;retry<1000;retry++) {
            assertEquals(0,PlaybackWriteBudget.acceptedSamples(0,960,2));
            clock[0]+=2_000_000;
        }
        assertFalse(progress.isFresh());
    }
}
