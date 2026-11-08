package dev.remotevoice.android;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.Test;

public final class AudioProgressTest {
    @Test public void allowsPlaybackStartupButNotAnEmptyStreamForever() {
        AtomicLong clock = new AtomicLong();
        AudioProgress progress = new AudioProgress(5000, clock::get);
        clock.set(TimeUnit.MILLISECONDS.toNanos(4999));
        assertTrue(progress.isFresh());
        clock.set(TimeUnit.SECONDS.toNanos(5));
        assertFalse(progress.isFresh());
    }

    @Test public void detectsAStalledAudioWriteEvenIfSocketStillReceives() {
        AtomicLong clock = new AtomicLong();
        AudioProgress progress = new AudioProgress(5000, clock::get);
        clock.set(TimeUnit.SECONDS.toNanos(1));
        progress.advanced();
        clock.set(TimeUnit.MILLISECONDS.toNanos(2999));
        assertTrue(progress.isFresh());
        clock.set(TimeUnit.SECONDS.toNanos(3));
        assertFalse(progress.isFresh());
    }

    @Test public void continuingAudioNeverExpires() {
        AtomicLong clock = new AtomicLong();
        AudioProgress progress = new AudioProgress(5000, clock::get);
        for (int second = 1; second <= 600; second++) {
            clock.set(TimeUnit.SECONDS.toNanos(second));
            assertTrue(progress.isFresh());
            progress.advanced();
        }
    }

    @Test public void microphoneMustStartAndKeepCapturing() {
        AtomicLong clock = new AtomicLong();
        AudioProgress progress = new AudioProgress(2000, clock::get);
        clock.set(TimeUnit.SECONDS.toNanos(2));
        assertFalse(progress.isFresh());
        progress.advanced();
        clock.set(TimeUnit.MILLISECONDS.toNanos(3999));
        assertTrue(progress.isFresh());
        clock.set(TimeUnit.SECONDS.toNanos(4));
        assertFalse(progress.isFresh());
    }

    @Test public void retiringWorkerCannotAdvanceItsReplacement() {
        AtomicLong clock = new AtomicLong();
        AudioProgress retiring = new AudioProgress(2000, clock::get);
        AudioProgress replacement = new AudioProgress(2000, clock::get);
        clock.set(TimeUnit.SECONDS.toNanos(3));
        retiring.advanced();
        assertTrue(retiring.isFresh());
        assertFalse(replacement.isFresh());
    }

    @Test public void playbackCannotHideStalledMicrophone() {
        AtomicLong clock = new AtomicLong();
        AudioProgress playback = new AudioProgress(5000, clock::get);
        AudioProgress microphone = new AudioProgress(2000, clock::get);
        clock.set(TimeUnit.SECONDS.toNanos(3));
        playback.advanced();
        assertTrue(playback.isFresh());
        assertFalse(microphone.isFresh());
    }

    @Test public void rejectsMissingDeadline() {
        assertThrows(IllegalArgumentException.class, () -> new AudioProgress(0));
        assertThrows(IllegalArgumentException.class, () -> new AudioProgress(-1));
    }
}
