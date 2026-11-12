package dev.remontevoice.android;

/** A coherent, scalar-only session maximum, independent of periodic log limits. */
final class PlaybackGapMaximum {
    static final class Snapshot {
        final long packet, previousPacket, atNanos, gapNanos;
        Snapshot(long packet, long previousPacket, long atNanos, long gapNanos) {
            this.packet = packet;
            this.previousPacket = previousPacket;
            this.atNanos = atNanos;
            this.gapNanos = gapNanos;
        }
        String fields() {
            return " session_max_offer_gap_ns=" + gapNanos
                    + " session_max_offer_gap_packet=" + packet
                    + " session_max_offer_gap_previous_packet=" + previousPacket
                    + " session_max_offer_gap_at_ns=" + atNanos;
        }
    }

    private volatile Snapshot maximum = new Snapshot(0, 0, 0, 0);
    private long previousPacket;
    private boolean frozen;

    synchronized void record(long packet, long atNanos, long gapNanos) {
        if (frozen || packet < 0 || gapNanos < 0) return;
        if (gapNanos > maximum.gapNanos) {
            maximum = new Snapshot(packet, previousPacket, atNanos, gapNanos);
        }
        previousPacket = packet;
    }

    Snapshot snapshot() { return maximum; }

    synchronized Snapshot freeze() {
        frozen = true;
        return maximum;
    }
}
