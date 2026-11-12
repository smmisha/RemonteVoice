package dev.remontevoice.android;

import android.media.MediaCodec;
import android.media.MediaFormat;
import android.os.Build;
import android.view.Surface;
import java.nio.ByteBuffer;
import java.util.Arrays;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

/** One owned codec worker, independent of audio and UI rendering. */
final class H264Decoder implements AutoCloseable {
    interface Listener {
        void onInputAccepted();
        void onFrameDecoded(long count);
        void onClosed(String reason);
    }
    private static final AtomicBoolean OWNER = new AtomicBoolean();
    private final int width, height;
    private final Surface initialSurface;
    private volatile Surface desiredSurface;
    private final Listener listener;
    private final boolean diagnostics;
    private final ArrayBlockingQueue<byte[]> pending = new ArrayBlockingQueue<>(2);
    private final AtomicBoolean closed = new AtomicBoolean();
    private final AtomicBoolean started = new AtomicBoolean();
    private final Thread worker;

    H264Decoder(int width, int height, Surface surface, Listener listener) {
        this(width, height, surface, listener, false);
    }

    H264Decoder(int width, int height, Surface surface, Listener listener, boolean diagnostics) {
        if (width < 16 || width > 1920 || height < 16 || height > 1200
                || (width & 1) != 0 || (height & 1) != 0 || listener == null) throw new IllegalArgumentException();
        this.width = width; this.height = height;
        this.diagnostics = diagnostics;
        this.initialSurface = surface; this.desiredSurface = surface; this.listener = listener;
        worker = new Thread(this::decode, "remontevoice-video-decode");
        worker.setDaemon(true);
    }

    boolean start() {
        if (!started.compareAndSet(false, true) || closed.get()) return false;
        if (!OWNER.compareAndSet(false, true)) { closed.set(true); return false; }
        try { worker.start(); return true; }
        catch (RuntimeException error) { OWNER.set(false); closed.set(true); return false; }
    }

    boolean submit(byte[] frame) {
        if (!started.get() || closed.get() || frame == null || frame.length > H264AccessUnit.MAX_BYTES) return false;
        return pending.offer(frame.clone());
    }

    void setSurface(Surface surface) {
        if (initialSurface == null || surface == null || !surface.isValid()) throw new IllegalArgumentException();
        desiredSurface = surface;
    }

    @Override public void close() {
        closed.set(true);
        pending.clear();
        worker.interrupt(); // Never wait for codec IPC on the UI or audio thread.
    }

    boolean awaitStopped(long milliseconds) throws InterruptedException {
        worker.join(Math.max(1, Math.min(milliseconds, 3000)));
        return !worker.isAlive();
    }

    private void decode() {
        MediaCodec codec = null;
        boolean codecStarted = false, released = true;
        String reason = "closed";
        try {
            H264AccessUnit.Parameters parameters = null;
            Surface output = initialSurface;
            byte[] frame = null;
            long submitted = 0, decoded = 0;
            long lastOutputAt = System.nanoTime();
            MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
            while (!closed.get()) {
                if (frame == null) frame = pending.poll(10, TimeUnit.MILLISECONDS);
                if (frame != null) {
                    H264AccessUnit.Parameters current = H264AccessUnit.inspect(frame, width, height, codec == null);
                    if (codec == null) {
                        parameters = current;
                        MediaFormat format = MediaFormat.createVideoFormat("video/avc", width, height);
                        format.setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, H264AccessUnit.MAX_BYTES);
                        format.setByteBuffer("csd-0", ByteBuffer.wrap(parameters.sps));
                        format.setByteBuffer("csd-1", ByteBuffer.wrap(parameters.pps));
                        if (Build.VERSION.SDK_INT >= 30) format.setInteger(MediaFormat.KEY_LOW_LATENCY, 1);
                        codec = MediaCodec.createDecoderByType("video/avc");
                        codec.configure(format, output, null, 0);
                        codec.start(); codecStarted = true;
                        lastOutputAt = System.nanoTime();
                    } else if (current != null && (!Arrays.equals(parameters.sps, current.sps)
                            || !Arrays.equals(parameters.pps, current.pps))) {
                        throw new IllegalArgumentException("Video parameters changed");
                    }
                    int index = codec.dequeueInputBuffer(10_000);
                    if (index >= 0) {
                        byte[] payload = H264AccessUnit.videoPayload(frame);
                        ByteBuffer input = codec.getInputBuffer(index);
                        if (input == null || input.capacity() < payload.length) throw new IllegalStateException();
                        input.clear(); input.put(payload);
                        codec.queueInputBuffer(index, 0, payload.length, submitted++ * 1_000_000L / 30, 0);
                        frame = null;
                        if (!closed.get()) listener.onInputAccepted();
                    }
                }
                if (codec != null) {
                    Surface next = desiredSurface;
                    if (next != output && next != null) { codec.setOutputSurface(next); output = next; }
                    for (int n = 0; n < 8 && !closed.get(); ++n) {
                        int index = codec.dequeueOutputBuffer(info, 0);
                        if (index == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) {
                            MediaFormat format = codec.getOutputFormat();
                            if (format.getInteger(MediaFormat.KEY_WIDTH) != width
                                    || format.getInteger(MediaFormat.KEY_HEIGHT) != height) throw new IllegalStateException();
                        } else if (index >= 0) {
                            codec.releaseOutputBuffer(index, output != null);
                            lastOutputAt = System.nanoTime();
                            if (!closed.get()) listener.onFrameDecoded(++decoded);
                        } else break;
                    }
                    if (submitted > decoded && System.nanoTime() - lastOutputAt > 5_000_000_000L) {
                        throw new IllegalStateException("Video decoder made no progress");
                    }
                }
            }
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
        } catch (Exception error) {
            reason = "decode_failed"; // No frame payload or device-specific error text.
        } finally {
            closed.set(true); pending.clear();
            long cleanupStarted = System.nanoTime();
            if (diagnostics) android.util.Log.i("RemonteVoicePreview", "codec_cleanup_start clock_ns="+cleanupStarted);
            if (codec != null) {
                if (codecStarted) try { codec.stop(); } catch (RuntimeException ignored) { }
                try { codec.release(); } catch (RuntimeException error) { released = false; }
            }
            if (released) OWNER.set(false);
            else reason = "cleanup_unconfirmed";
            if (diagnostics) android.util.Log.i("RemonteVoicePreview", "codec_cleanup_end clock_ns="+System.nanoTime()
                    +" duration_ms="+TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-cleanupStarted)
                    +" released="+released);
            try { listener.onClosed(reason); } catch (RuntimeException ignored) { }
        }
    }
}
