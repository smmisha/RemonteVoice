package dev.remontevoice.android;

import org.junit.Test;
import static org.junit.Assert.*;

public final class PlaybackBufferPolicyTest {
    @Test public void coordinatedReserveAdds160msWithinTheSameTotalCapacity() {
        PlaybackBufferPolicy p=new PlaybackBufferPolicy(48000,1920);
        assertEquals(7680,p.frames());assertEquals(11520,p.capacityFrames());
        assertEquals(40,PlaybackBufferPolicy.QUEUE_FRAMES);
        assertEquals(32,p.primingPackets(480,p.frames()));
        assertEquals(40,p.primingPackets(480,p.capacityFrames()));
        // Same 640ms maximum as the rejected native400ms + queue240ms profile.
        assertEquals(640, p.capacityFrames()/48+PlaybackBufferPolicy.QUEUE_FRAMES*10);
    }
    @Test public void underrunsIncreaseBufferInBoundedSteps() {
        PlaybackBufferPolicy p=new PlaybackBufferPolicy(48000,1920);
        assertEquals(7680,p.observeUnderruns(0));
        assertEquals(9600,p.observeUnderruns(1));
        assertEquals(9600,p.observeUnderruns(1));
        assertEquals(11520,p.observeUnderruns(2));
        assertEquals(11520,p.observeUnderruns(10000));
    }
    @Test public void deviceMinimumIsRespectedWithoutUnboundedGrowth() {
        PlaybackBufferPolicy p=new PlaybackBufferPolicy(48000,24000);
        assertEquals(24000,p.frames());assertEquals(24000,p.capacityFrames());
        assertEquals(24000,p.observeUnderruns(500));
    }
    @Test public void reprimingAcknowledgesLateUnderrunsWithoutAnotherFlushOrGrowth() {
        PlaybackBufferPolicy p=new PlaybackBufferPolicy(48000,1920);
        assertEquals(9600,p.observeUnderruns(1));
        // A second underrun arrives before pause/flush completes. The filled
        // reserve covers both events when playback is about to resume.
        int handledUnderruns=p.acknowledgeUnderruns(2);
        assertEquals(2,handledUnderruns);
        assertEquals(9600,p.frames());
        assertEquals(9600,p.observeUnderruns(2));
        assertFalse(2>handledUnderruns);
        // An event after resuming still requests one growth/rebuffer step.
        assertTrue(3>handledUnderruns);
        assertEquals(11520,p.observeUnderruns(3));
        assertEquals(11520,p.observeUnderruns(3));
    }
    @Test public void repeatedOrBackwardAcknowledgementCannotResetTheBaseline() {
        PlaybackBufferPolicy p=new PlaybackBufferPolicy(48000,1920);
        assertEquals(4,p.acknowledgeUnderruns(4));
        assertEquals(4,p.acknowledgeUnderruns(4));
        assertEquals(4,p.acknowledgeUnderruns(2));
        assertEquals(7680,p.frames());
        assertEquals(7680,p.observeUnderruns(4));
        assertEquals(9600,p.observeUnderruns(5));
    }
    @Test public void negativeAcknowledgementIsRejectedWithoutChangingTheBaseline() {
        PlaybackBufferPolicy p=new PlaybackBufferPolicy(48000,1920);
        assertEquals(2,p.acknowledgeUnderruns(2));
        assertThrows(IllegalArgumentException.class,()->p.acknowledgeUnderruns(-1));
        assertEquals(7680,p.observeUnderruns(2));
        assertEquals(9600,p.observeUnderruns(3));
    }
    @Test public void nonPacketAlignedDeviceMinimumKeepsPrimingBounded() {
        PlaybackBufferPolicy p=new PlaybackBufferPolicy(48000,10001);
        assertEquals(10001,p.frames());
        assertEquals(11520,p.capacityFrames());
        assertEquals(37,p.primingPackets(480,p.frames()));
        assertEquals(11520,p.observeUnderruns(1));
        assertEquals(40,p.primingPackets(480,p.frames()));
        assertEquals(11520,p.observeUnderruns(Integer.MAX_VALUE));
    }
    @Test public void coordinatedReserveScalesWithRateAndRespectsQueueCapacity() {
        PlaybackBufferPolicy p=new PlaybackBufferPolicy(44100,1);
        assertEquals(7056,p.frames());
        assertEquals(10584,p.capacityFrames());
        assertEquals(32,p.primingPackets(441,p.frames()));
        assertEquals(8820,p.observeUnderruns(1));
        assertEquals(36,p.primingPackets(441,p.frames()));
        assertEquals(40,p.primingPackets(441,10584));
        assertEquals(40,p.primingPackets(441,Integer.MAX_VALUE));
        assertThrows(IllegalArgumentException.class,()->p.primingPackets(0,100));
        assertThrows(IllegalArgumentException.class,()->p.primingPackets(480,0));
        assertThrows(IllegalArgumentException.class,()->new PlaybackBufferPolicy(384001,0));
    }
    @Test public void rejectsInvalidDeviceParameters() {
        assertThrows(IllegalArgumentException.class,()->new PlaybackBufferPolicy(0,1920));
        assertThrows(IllegalArgumentException.class,()->new PlaybackBufferPolicy(48000,-1));
    }
}
