package dev.remontevoice.android;

/** Never block a paused track by writing across its priming boundary. */
final class PlaybackWriteBudget {
    private PlaybackWriteBudget() {}

    static int samples(int remaining, int channels, boolean playing, int primedFrames, int targetFrames) {
        if (channels <= 0 || remaining < 0 || remaining % channels != 0
                || primedFrames < 0 || targetFrames <= 0) throw new IllegalArgumentException();
        if (playing) return remaining;
        return (int) Math.min(remaining, Math.max(0L, (long) targetFrames - primedFrames) * channels);
    }

    static int acceptedSamples(int written, int requested, int channels) {
        if (channels <= 0 || requested <= 0 || requested % channels != 0) throw new IllegalArgumentException();
        if (written < 0 || written > requested || written % channels != 0) {
            throw new IllegalStateException("Аудиовыход недоступен.");
        }
        return written; // AudioTrack permits zero; it does not advance the packet.
    }
}
