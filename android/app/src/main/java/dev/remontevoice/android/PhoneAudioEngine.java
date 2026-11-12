package dev.remontevoice.android;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Context;
import android.content.pm.PackageManager;
import android.content.pm.ApplicationInfo;
import android.media.AudioAttributes;
import android.media.AudioFocusRequest;
import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioRecord;
import android.media.AudioTrack;
import android.media.MediaRecorder;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;

import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;

/** Short, bounded in-memory audio buffers. No audio files or automatic microphone restart. */
final class PhoneAudioEngine {
    private final Context context;
    private final Consumer<String> onFailure;
    private final AudioManager manager;
    private final AudioAttributes attributes = new AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA).setContentType(AudioAttributes.CONTENT_TYPE_SPEECH).build();
    private AudioFocusRequest focus;
    private Playback playback;
    private Recording recording;
    private final boolean diagnostics;

    PhoneAudioEngine(Context context, Consumer<String> onFailure) {
        this.context = context.getApplicationContext();
        this.onFailure = onFailure;
        manager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
        diagnostics = (context.getApplicationInfo().flags & ApplicationInfo.FLAG_DEBUGGABLE) != 0;
    }

    synchronized void startPlayback() {
        if (playback != null) return;
        focus = new AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN)
                .setAudioAttributes(attributes).setAcceptsDelayedFocusGain(false)
                .setOnAudioFocusChangeListener(change -> {
                    android.util.Log.i("RemonteVoiceSession", "audio_focus=" + change);
                    if (change == AudioManager.AUDIOFOCUS_LOSS || change == AudioManager.AUDIOFOCUS_LOSS_TRANSIENT) {
                        onFailure.accept("Звук прерван другим приложением. Подключитесь заново.");
                    }
                }, new Handler(Looper.getMainLooper())).build();
        if (manager.requestAudioFocus(focus) != AudioManager.AUDIOFOCUS_REQUEST_GRANTED) {
            manager.abandonAudioFocusRequest(focus);
            focus = null;
            throw new IllegalStateException("Не удалось получить доступ к воспроизведению звука.");
        }
        AudioTrack track = null;
        try {
            int minimum = AudioTrack.getMinBufferSize(48000, AudioFormat.CHANNEL_OUT_STEREO, AudioFormat.ENCODING_PCM_FLOAT);
            if (minimum <= 0) throw new IllegalStateException("Стерео 48 кГц недоступно на этом телефоне.");
            PlaybackBufferPolicy buffering = new PlaybackBufferPolicy(48000, (minimum + 7) / 8);
            track = new AudioTrack.Builder().setAudioAttributes(attributes)
                    .setAudioFormat(new AudioFormat.Builder().setSampleRate(48000)
                            .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO).setEncoding(AudioFormat.ENCODING_PCM_FLOAT).build())
                    .setBufferSizeInBytes(buffering.capacityFrames() * 8).setTransferMode(AudioTrack.MODE_STREAM).build();
            if (track.getState() != AudioTrack.STATE_INITIALIZED) {
                throw new IllegalStateException("Не удалось открыть аудиовыход телефона.");
            }
            int bufferFrames = track.setBufferSizeInFrames(buffering.frames());
            if (bufferFrames <= 0) throw new IllegalStateException("Не удалось подготовить буфер воспроизведения.");
            playback = new Playback(track, buffering, bufferFrames);
            playback.thread.start();
        } catch (RuntimeException exception) {
            if (track != null) track.release();
            playback = null;
            manager.abandonAudioFocusRequest(focus);
            focus = null;
            throw exception;
        }
    }

    void offer(float[] samples) {
        offer(samples, 0);
    }

    void offer(float[] samples, long packet) {
        long waitStarted = diagnostics ? System.nanoTime() : 0;
        Playback target;
        synchronized (this) {
            target = playback;
        }
        // Waiting for a short catch-up burst must not hold the engine lock:
        // disconnect and microphone controls need it while the writer drains.
        if (target != null) target.offer(samples, waitStarted, packet);
    }

    private void offer(Playback playback, float[] samples, long waitStarted, long packet) {
        synchronized (playback) {
            long acquiredAt = diagnostics ? System.nanoTime() : 0;
            if (playback.running) {
                long now = System.nanoTime();
                if (diagnostics && acquiredAt-waitStarted > playback.maxOfferWaitNanos) {
                    playback.maxOfferWaitNanos = acquiredAt-waitStarted;
                    // Playback uptime at the end of this chosen maximum wait.
                    playback.maxOfferWaitAtNanos = acquiredAt-playback.startedAt;
                }
                long previousGap = playback.offeredFrames > 0 ? Math.max(0, now-playback.lastOffer) : 0;
                if (diagnostics) playback.sessionGap.record(packet, now, previousGap);
                if (previousGap > playback.maxReceiveGap) {
                    playback.maxReceiveGap = previousGap;
                    playback.maxReceiveGapAtNanos = now;
                }
                playback.lastOffer = now;
                playback.offeredFrames++;
                if (diagnostics) for (float sample : samples) playback.peak = Math.max(playback.peak,Math.abs(sample));
                if (diagnostics && playback.dropTiming.wantsSample()) {
                    playback.dropTiming.sample(now, playback.offeredFrames, playback.queue.size(),
                            playback.writtenSamples, playback.playedFrames, playback.nativeQueuedFrames);
                }
                if (!playback.queue.offer(samples)) {
                    boolean accepted;
                    long backpressureStarted = diagnostics ? System.nanoTime() : 0;
                    if (diagnostics) playback.backpressureWaits++;
                    try {
                        // Recheck cancellation/native progress each100ms without
                        // treating a healthy slow write as lost PCM. Queue capacity
                        // and priming are unchanged; the lifecycle lock stays free.
                        accepted = PlaybackQueueHandoff.offer(playback.queue,samples,()->playback.running,
                                playback.progress::isFresh);
                    } catch (InterruptedException error) {
                        Thread.currentThread().interrupt();
                        throw new IllegalStateException("Воспроизведение звука остановлено.", error);
                    } finally {
                        if (diagnostics) playback.maxBackpressureNanos = Math.max(playback.maxBackpressureNanos,
                                System.nanoTime()-backpressureStarted);
                    }
                }
                if (!playback.running) {
                    playback.queue.clear(); // A cancelled handoff belongs only to this retired playback.
                    return;
                }
                playback.primingGate.signal();
                if (diagnostics) playback.maxOfferBodyNanos = Math.max(playback.maxOfferBodyNanos, System.nanoTime()-acquiredAt);
            }
        }
    }

    synchronized void assertProgress() {
        long watchdogStarted = diagnostics ? System.nanoTime() : 0;
        try {
            if (diagnostics && playback != null && playback.running) playback.reportDiagnostics();
            if (playback != null && playback.running && !playback.progress.isFresh()) {
                android.util.Log.i("RemonteVoiceSession", "playback_stalled offered=" + playback.offeredFrames
                        + " queued=" + playback.queue.size() + " receive_age_ms="
                        + TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - playback.lastOffer));
                throw new IllegalStateException("Звук ноутбука перестал воспроизводиться. Сеанс завершён.");
            }
            if (recording != null && recording.running && !recording.progress.isFresh()) {
                throw new IllegalStateException("Микрофон перестал передавать звук. Сеанс завершён.");
            }
        } finally {
            if (diagnostics && playback != null && playback.running) {
                playback.maxWatchdogHoldNanos = Math.max(playback.maxWatchdogHoldNanos, System.nanoTime()-watchdogStarted);
            }
        }
    }

    @SuppressLint("MissingPermission") // Explicit runtime check; only called after user's microphone action.
    synchronized void startMicrophone(Consumer<float[]> sink) {
        if (recording != null) return;
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            throw new SecurityException("Разрешите доступ к микрофону.");
        }
        int minimum = AudioRecord.getMinBufferSize(48000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        if (minimum <= 0) throw new IllegalStateException("Микрофон 48 кГц недоступен на этом телефоне.");
        AudioRecord record = new AudioRecord.Builder().setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION)
                .setAudioFormat(new AudioFormat.Builder().setSampleRate(48000)
                        .setChannelMask(AudioFormat.CHANNEL_IN_MONO).setEncoding(AudioFormat.ENCODING_PCM_16BIT).build())
                .setBufferSizeInBytes(Math.max(minimum, 480 * 2 * 4)).build();
        try {
            if (record.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("Не удалось открыть микрофон.");
            record.startRecording();
            if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING) throw new IllegalStateException("Микрофон занят или отключён системой.");
            recording = new Recording(record, sink);
            recording.thread.start();
        } catch (RuntimeException exception) {
            recording = null;
            record.release();
            throw exception;
        }
    }

    synchronized void stopMicrophone() {
        Recording previous = recording;
        recording = null;
        if (previous != null) {
            previous.running = false;
            try { previous.record.stop(); } catch (IllegalStateException ignored) { /* already stopped */ }
            previous.thread.interrupt();
        }
    }

    synchronized void stop() {
        stopMicrophone();
        Playback previous = playback;
        playback = null;
        if (previous != null) {
            previous.running = false;
            if (diagnostics) {
                PlaybackGapMaximum.Snapshot maximum = previous.sessionGap.freeze();
                try { android.util.Log.i("RemonteVoiceSession", "playback_gap_maximum" + maximum.fields()); }
                catch (RuntimeException ignored) { /* Observation must not prevent shutdown. */ }
            }
            previous.queue.clear();
            previous.primingGate.signal();
            try { previous.track.pause(); previous.track.flush(); } catch (IllegalStateException ignored) { /* device closed */ }
            previous.thread.interrupt();
        }
        if (focus != null) { manager.abandonAudioFocusRequest(focus); focus = null; }
    }

    private final class Recording implements Runnable {
        final AudioRecord record;
        final Consumer<float[]> sink;
        final Thread thread = new Thread(this, "RemonteVoice-microphone");
        final AudioProgress progress = new AudioProgress(2000);
        long microphoneStatsAt = diagnostics ? System.nanoTime() : 0;
        long capturedPackets, captureSamples, captureSquareSum, clippedSamples, microphoneLogNanos;
        int capturePeak;
        volatile boolean running = true;
        Recording(AudioRecord record, Consumer<float[]> sink) { this.record = record; this.sink = sink; }
        void recordCaptureDiagnostics(short[] pcm) {
            capturedPackets++;
            captureSamples += pcm.length;
            for (short sample : pcm) {
                captureSquareSum += (long) sample * sample;
                capturePeak = Math.max(capturePeak, Math.abs((int) sample));
                if (sample == Short.MIN_VALUE || sample == Short.MAX_VALUE) clippedSamples++;
            }
        }
        void reportMicrophoneDiagnostics() {
            long now = System.nanoTime();
            if (now-microphoneStatsAt < TimeUnit.SECONDS.toNanos(3)) return;
            microphoneStatsAt = now;
            try {
                // Levels use full-scale units, matching the unchanged float PCM.
                String message = "microphone_stats captured_packets=" + capturedPackets
                        + " capture_rms=" + (Math.sqrt((double) captureSquareSum / captureSamples) / 32768d)
                        + " capture_peak=" + (capturePeak / 32768d)
                        + " clipped_samples=" + clippedSamples
                        + " diagnostic_log_ms=" + TimeUnit.NANOSECONDS.toMillis(microphoneLogNanos);
                // Report after sending; this log call's latency appears next time.
                long logStarted = System.nanoTime();
                try { android.util.Log.i("RemonteVoiceSession", message); }
                finally { microphoneLogNanos = System.nanoTime()-logStarted; }
            } catch (RuntimeException ignored) { /* Diagnostics never change microphone lifecycle. */ }
            finally {
                capturedPackets = 0;
                captureSamples = 0;
                captureSquareSum = 0;
                capturePeak = 0;
                clippedSamples = 0;
            }
        }
        public void run() {
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
            short[] pcm = new short[480];
            try {
                while (running) {
                    int offset = 0;
                    while (running && offset < pcm.length) {
                        int count = record.read(pcm, offset, pcm.length - offset, AudioRecord.READ_BLOCKING);
                        if (count <= 0) throw new IllegalStateException("Микрофон недоступен. Проверьте разрешение и аудиоустройство.");
                        offset += count;
                        progress.advanced();
                    }
                    if (!running) break;
                    if (diagnostics) recordCaptureDiagnostics(pcm);
                    float[] frame = new float[480];
                    for (int i = 0; i < frame.length; i++) frame[i] = pcm[i] / 32768f;
                    sink.accept(frame);
                    if (diagnostics) reportMicrophoneDiagnostics();
                }
            } catch (RuntimeException exception) {
                if (running) onFailure.accept("Передача микрофона прервана. Подключитесь заново.");
            } finally { record.release(); }
        }
    }

    private final class Playback implements Runnable {
        final AudioTrack track;
        long lastOffer = System.nanoTime();
        long offeredFrames;
        long droppedFrames;
        volatile long writtenSamples;
        long maxReceiveGap;
        long maxReceiveGapAtNanos;
        float peak;
        long reportedAt=System.nanoTime();
        final ArrayBlockingQueue<float[]> queue = new ArrayBlockingQueue<>(PlaybackBufferPolicy.QUEUE_FRAMES);
        final PlaybackPrimingGate primingGate = new PlaybackPrimingGate();
        final PlaybackBufferPolicy buffering;
        volatile int bufferFrames;
        volatile int observedUnderruns;
        volatile long rebufferDiscardedFrames;
        final long startedAt = System.nanoTime();
        volatile long nativeQueuedFrames;
        volatile long playedFrames;
        volatile long maxWriteNanos;
        volatile long maxWriteAtNanos;
        volatile long maxWriterIdleNanos;
        volatile long maxQueueWaitNanos;
        volatile long maxOfferWaitNanos;
        volatile long maxOfferWaitAtNanos;
        volatile long maxOfferBodyNanos;
        volatile long maxDiagnosticLogNanos;
        volatile long maxWatchdogHoldNanos;
        volatile long rebufferCount;
        volatile long rebufferNanos;
        volatile long zeroWrites;
        volatile long backpressureWaits;
        volatile long maxBackpressureNanos;
        final PlaybackDropTiming dropTiming = diagnostics ? new PlaybackDropTiming() : null;
        final PlaybackGapMaximum sessionGap = diagnostics ? new PlaybackGapMaximum() : null;
        final Thread thread = new Thread(this, "RemonteVoice-playback");
        final AudioProgress progress = new AudioProgress(5000);
        volatile boolean running = true;
        Playback(AudioTrack track, PlaybackBufferPolicy buffering, int bufferFrames) {
            this.track = track; this.buffering = buffering; this.bufferFrames = bufferFrames;
        }
        void offer(float[] samples, long waitStarted, long packet) {
            PhoneAudioEngine.this.offer(this, samples, waitStarted, packet);
        }
        void reportDiagnostics() {
            long now=System.nanoTime();
            if (now-reportedAt<TimeUnit.SECONDS.toNanos(3)) return;
            reportedAt=now;
            try {
                String message = "playback_stats offered="+offeredFrames+" dropped="+droppedFrames
                    +" written_samples="+writtenSamples+" queue="+queue.size()+" max_gap_ms="+TimeUnit.NANOSECONDS.toMillis(maxReceiveGap)
                    +" underruns="+observedUnderruns+" buffer_frames="+bufferFrames+" peak="+peak
                    +" rebuffer_discarded_frames="+rebufferDiscardedFrames
                    +" uptime_ms="+TimeUnit.NANOSECONDS.toMillis(now-startedAt)
                    +" last_offer_age_ms="+TimeUnit.NANOSECONDS.toMillis(now-lastOffer)
                    +" native_queued_frames="+nativeQueuedFrames+" played_frames="+playedFrames
                    +" max_write_ms="+TimeUnit.NANOSECONDS.toMillis(maxWriteNanos)
                    +" max_write_at_ns="+maxWriteAtNanos
                    +" max_gap_at_ns="+maxReceiveGapAtNanos
                    +" max_writer_idle_ms="+TimeUnit.NANOSECONDS.toMillis(maxWriterIdleNanos)
                    +" max_queue_wait_ms="+TimeUnit.NANOSECONDS.toMillis(maxQueueWaitNanos)
                    +" clock_ns="+now
                    +" max_offer_wait_ms="+TimeUnit.NANOSECONDS.toMillis(maxOfferWaitNanos)
                    +" max_offer_wait_at_ms="+TimeUnit.NANOSECONDS.toMillis(maxOfferWaitAtNanos)
                    +" max_offer_body_ms="+TimeUnit.NANOSECONDS.toMillis(maxOfferBodyNanos)
                    +" max_diagnostic_log_ms="+TimeUnit.NANOSECONDS.toMillis(maxDiagnosticLogNanos)
                    +" max_watchdog_hold_ms="+TimeUnit.NANOSECONDS.toMillis(maxWatchdogHoldNanos)
                    +" rebuffer_count="+rebufferCount+" rebuffer_ms="+TimeUnit.NANOSECONDS.toMillis(rebufferNanos)
                    +" zero_writes="+zeroWrites
                    +" backpressure_waits="+backpressureWaits
                    +" max_backpressure_ms="+TimeUnit.NANOSECONDS.toMillis(maxBackpressureNanos)
                    + dropTiming.fields() + sessionGap.snapshot().fields();
                // The current log call's duration appears in the next report.
                maxDiagnosticLogNanos=0;
                long logStarted = System.nanoTime();
                try { android.util.Log.i("RemonteVoiceSession", message); }
                finally { maxDiagnosticLogNanos = Math.max(maxDiagnosticLogNanos, System.nanoTime()-logStarted); }
                maxReceiveGap=0;maxReceiveGapAtNanos=0;peak=0;
                maxOfferWaitNanos=0;maxOfferWaitAtNanos=0;maxOfferBodyNanos=0;
                // assertProgress records its entire hold after this report returns.
                maxWatchdogHoldNanos=0;
            } catch (RuntimeException ignored) { /* Diagnostics never change audio lifecycle. */ }
        }
        public void run() {
            Process.setThreadPriority(Process.THREAD_PRIORITY_AUDIO);
            boolean playing = false;
            int primedFrames = 0;
            long trackFrames = 0;
            long rebufferStarted = 0;
            long playedBeforeFlush = 0;
            int handledUnderruns = 0;
            long lastWriteEnd = System.nanoTime();
            String operation = "queue";
            int nativeResult = 0;
            try {
                while (running) {
                    if (!playing && primedFrames==0) {
                        operation = "priming_reserve";
                        primingGate.await(buffering.primingPackets(480,bufferFrames),()->running,queue::size);
                        if (!running) break;
                    }
                    long pollStarted = diagnostics ? System.nanoTime() : 0;
                    float[] frame = queue.poll(500, TimeUnit.MILLISECONDS);
                    if (diagnostics) maxQueueWaitNanos = Math.max(maxQueueWaitNanos, System.nanoTime()-pollStarted);
                    if (frame == null) {
                        operation = "underruns";
                        observedUnderruns = track.getUnderrunCount();
                        continue;
                    }
                    int offset = 0;
                    while (running && offset < frame.length) {
                        operation = "write_budget";
                        int count = PlaybackWriteBudget.samples(frame.length-offset, 2, playing, primedFrames, bufferFrames);
                        long writeStarted = System.nanoTime();
                        if (diagnostics) maxWriterIdleNanos = Math.max(maxWriterIdleNanos, writeStarted-lastWriteEnd);
                        operation = "write";
                        int written = track.write(frame, offset, count, AudioTrack.WRITE_BLOCKING);
                        nativeResult = written;
                        long writeEnd = System.nanoTime();
                        if (writeEnd-writeStarted > maxWriteNanos) {
                            maxWriteNanos = writeEnd-writeStarted;
                            maxWriteAtNanos = writeEnd;
                        }
                        lastWriteEnd = writeEnd;
                        PlaybackWriteBudget.acceptedSamples(written,count,2);
                        if (written == 0) {
                            zeroWrites++;
                            // A short native write is not a device failure. Keep
                            // this offset and let the existing progress watchdog
                            // close a persistent stall; never spin or fake progress.
                            Thread.sleep(2);
                            continue;
                        }
                        offset += written;
                        writtenSamples += written;
                        trackFrames += written / 2;
                        if (!playing) primedFrames += written / 2;
                        progress.advanced();
                        // Start at the exact boundary, even when the device's
                        // effective buffer is not a multiple of a wire packet.
                        if (!playing && primedFrames >= bufferFrames) {
                            if (rebufferStarted != 0) {
                                // Native capacity alone does not rebuild the app
                                // reserve after starvation. Refill the existing
                                // queue before resuming; initial startup is unchanged.
                                operation = "recovery_reserve";
                                primingGate.await(PlaybackBufferPolicy.QUEUE_FRAMES,()->running,queue::size);
                                if (!running) break;
                                if (diagnostics) android.util.Log.i("RemonteVoiceSession",
                                        "recovery_reserve clock_ns="+System.nanoTime()
                                        +" native_frames="+primedFrames+" queued_packets="+queue.size());
                            }
                            // A late underrun from the previous play interval is
                            // already covered by this completed refill.
                            operation = "underruns";
                            observedUnderruns = buffering.acknowledgeUnderruns(track.getUnderrunCount());
                            handledUnderruns = observedUnderruns;
                            operation = "play";
                            track.play();
                            playing = true;
                            if (rebufferStarted != 0) {
                                rebufferNanos += System.nanoTime()-rebufferStarted;
                                rebufferStarted = 0;
                            }
                        }
                    }
                    if (!running) break;
                    operation = "head";
                    long head = Integer.toUnsignedLong(track.getPlaybackHeadPosition());
                    nativeQueuedFrames = Math.max(0, trackFrames-head);
                    playedFrames = playedBeforeFlush+head;
                    if (playing) {
                        int previousUnderruns = handledUnderruns;
                        operation = "underruns";
                        observedUnderruns = track.getUnderrunCount();
                        int target = buffering.observeUnderruns(observedUnderruns);
                        if (target > bufferFrames) {
                            operation = "buffer_size";
                            int actual = track.setBufferSizeInFrames(target);
                            if (actual > 0) bufferFrames = actual;
                        }
                        if (observedUnderruns > previousUnderruns) {
                            if (diagnostics) android.util.Log.i("RemonteVoiceSession",
                                    "playback_underrun clock_ns="+System.nanoTime()
                                    +" count="+observedUnderruns+" offered="+offeredFrames
                                    +" receive_age_ms="+TimeUnit.NANOSECONDS.toMillis(System.nanoTime()-lastOffer)
                                    +" queue="+queue.size()+" native_queued_frames="+nativeQueuedFrames
                                    +" max_gap_ms="+TimeUnit.NANOSECONDS.toMillis(maxReceiveGap)
                                    +" max_gap_at_ns="+maxReceiveGapAtNanos
                                    +" max_write_ms="+TimeUnit.NANOSECONDS.toMillis(maxWriteNanos)
                                    +" max_write_at_ns="+maxWriteAtNanos);
                            // An empty track has lost its jitter reserve. Merely
                            // increasing capacity never fills it: rebuild before
                            // resuming, retaining the bounded incoming queue.
                            operation = "rebuffer";
                            track.pause();
                            rebufferDiscardedFrames += Math.max(0, trackFrames
                                    - Integer.toUnsignedLong(track.getPlaybackHeadPosition()));
                            playedBeforeFlush += Integer.toUnsignedLong(track.getPlaybackHeadPosition());
                            track.flush();
                            playing = false;
                            primedFrames = 0;
                            trackFrames = 0;
                            nativeQueuedFrames = 0;
                            rebufferCount++;
                            rebufferStarted = System.nanoTime();
                        }
                        handledUnderruns = observedUnderruns;
                    }
                }
            } catch (InterruptedException exception) {
                Thread.currentThread().interrupt();
            } catch (RuntimeException exception) {
                if (diagnostics && running) android.util.Log.i("RemonteVoiceSession",
                        "playback_api_failure operation="+operation+" class="+exception.getClass().getSimpleName()
                                +" native_result="+nativeResult+" buffer_frames="+bufferFrames);
                if (running) onFailure.accept("Воспроизведение звука прервано. Подключитесь заново.");
            } finally { track.release(); }
        }
    }
}
