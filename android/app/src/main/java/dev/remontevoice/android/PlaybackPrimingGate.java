package dev.remontevoice.android;

import java.util.function.BooleanSupplier;
import java.util.function.IntSupplier;

/** Wait only on the audio writer; network, controls and microphone never wait here. */
final class PlaybackPrimingGate {
    synchronized void await(int packets,BooleanSupplier running,IntSupplier available)
            throws InterruptedException {
        if (packets<=0 || packets>PlaybackBufferPolicy.QUEUE_FRAMES) throw new IllegalArgumentException();
        while (running.getAsBoolean() && available.getAsInt()<packets) wait();
    }

    synchronized void signal() { notifyAll(); }
}
