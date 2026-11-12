package dev.remontevoice.android;

/** Bounded network-jitter reserve; independent of PCM contents and session lifetime. */
final class PlaybackBufferPolicy {
    private static final int EXTRA_RESERVE_PACKETS=16; // 160ms of 10ms wire frames.
    static final int QUEUE_FRAMES=24+EXTRA_RESERVE_PACKETS;
    private final int capacity,step;
    private int frames,lastUnderruns;
    PlaybackBufferPolicy(int sampleRate,int minimumFrames) {
        if (sampleRate<=0 || sampleRate>384000 || minimumFrames<0) throw new IllegalArgumentException();
        // Keep the smaller native track after overflow in the native320ms trial.
        // Hold the extra reserve in the bounded app queue instead, without
        // increasing the combined maximum beyond the same added 160ms budget.
        frames=Math.max(minimumFrames,(int)Math.ceil(sampleRate*.160));
        capacity=Math.max(minimumFrames,(int)Math.ceil(sampleRate*.240));
        step=(int)Math.ceil(sampleRate*.040);
    }
    int frames() { return frames; }
    int capacityFrames() { return capacity; }
    int primingPackets(int packetFrames,int nativeFrames) {
        if (packetFrames<=0 || nativeFrames<=0) throw new IllegalArgumentException();
        long nativePackets=((long)nativeFrames+packetFrames-1)/packetFrames;
        return (int)Math.min(QUEUE_FRAMES,nativePackets+EXTRA_RESERVE_PACKETS);
    }
    /** The freshly filled reserve already covers events observed before play(). */
    int acknowledgeUnderruns(int count) {
        if (count<0) throw new IllegalArgumentException();
        lastUnderruns=Math.max(lastUnderruns,count);
        return lastUnderruns;
    }
    int observeUnderruns(int count) {
        if (count>lastUnderruns) {
            frames=(int)Math.min(capacity,frames+(long)(count-lastUnderruns)*step);
            lastUnderruns=count;
        }
        return frames;
    }
}
