"""Source guards for aggregate-only diagnostics on the explicit microphone worker."""

from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'android/app/src/main/java/dev/remotevoice/android/PhoneAudioEngine.java'


class AndroidMicrophoneDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.text = SOURCE.read_text(encoding='utf-8')
        self.recording = self.text.split('private final class Recording implements Runnable', 1)[1].split('private final class Playback implements Runnable', 1)[0]

    def test_only_explicit_microphone_capture_gets_diagnostics_and_normalizer_is_unchanged(self):
        start = self.text.split('synchronized void startMicrophone(', 1)[1].split('synchronized void stopMicrophone()', 1)[0]
        self.assertIn('checkSelfPermission(Manifest.permission.RECORD_AUDIO)', start)
        self.assertIn('MediaRecorder.AudioSource.VOICE_RECOGNITION', start)
        self.assertIn('record.startRecording();', start)
        self.assertIn('if (!running) break;', self.recording)
        self.assertIn('for (int i = 0; i < frame.length; i++) frame[i] = pcm[i] / 32768f;', self.recording)
        self.assertEqual(1, self.text.count('record.startRecording();'))
        self.assertNotIn('AutomaticGainControl', self.text)

    def test_debug_only_capture_loop_runs_before_normalization_and_reports_after_send(self):
        run = self.recording.split('public void run()', 1)[1]
        self.assertIn('if (diagnostics) recordCaptureDiagnostics(pcm);', run)
        self.assertIn('if (diagnostics) reportMicrophoneDiagnostics();', run)
        self.assertLess(run.index('recordCaptureDiagnostics(pcm)'), run.index('pcm[i] / 32768f'))
        self.assertLess(run.index('sink.accept(frame);'), run.index('reportMicrophoneDiagnostics();'))
        self.assertIn('ApplicationInfo.FLAG_DEBUGGABLE', self.text)

    def test_integer_pcm_aggregates_preserve_full_scale_and_never_retain_samples(self):
        observe = self.recording.split('void recordCaptureDiagnostics(short[] pcm)', 1)[1].split('void reportMicrophoneDiagnostics()', 1)[0]
        self.assertIn('capturedPackets++;', observe)
        self.assertIn('captureSamples += pcm.length;', observe)
        self.assertIn('captureSquareSum += (long) sample * sample;', observe)
        self.assertIn('Math.abs((int) sample)', observe)
        self.assertIn('sample == Short.MIN_VALUE || sample == Short.MAX_VALUE', observe)
        self.assertIn('clippedSamples++;', observe)
        for action in ('pcm[', 'Log.', 'progress.advanced()', 'record.', 'sink.', '.clone()', '.copyOf('):
            self.assertNotIn(action, observe)

    def test_report_has_three_second_monotonic_windows_and_resets_every_aggregate(self):
        report = self.recording.split('void reportMicrophoneDiagnostics()', 1)[1].split('public void run()', 1)[0]
        self.assertIn('System.nanoTime()', report)
        self.assertIn('TimeUnit.SECONDS.toNanos(3)', report)
        self.assertIn('microphoneStatsAt = now;', report)
        self.assertIn('Math.sqrt((double) captureSquareSum / captureSamples) / 32768d', report)
        self.assertIn('capturePeak / 32768d', report)
        self.assertIn('catch (RuntimeException ignored)', report)
        self.assertIn('finally', report)
        for field in ('capturedPackets', 'captureSamples', 'captureSquareSum', 'capturePeak', 'clippedSamples'):
            self.assertIn(field+' = 0;', report)

    def test_logs_contain_only_aggregate_scalars_and_previous_log_latency(self):
        report = self.recording.split('void reportMicrophoneDiagnostics()', 1)[1].split('public void run()', 1)[0]
        for name in ('microphone_stats captured_packets=', 'capture_rms=', 'capture_peak=', 'clipped_samples=', 'diagnostic_log_ms='):
            self.assertIn(name, report)
        self.assertIn('android.util.Log.i("RemoteVoiceSession", message)', report)
        self.assertIn('microphoneLogNanos = System.nanoTime()-logStarted;', report)
        for value in ('pcm', 'frame', 'exception.', 'credential', 'getRoutedDevice', 'record.', 'progress.advanced()'):
            self.assertNotIn(value, report)

    def test_diagnostics_add_no_main_task_native_query_or_audio_file(self):
        helpers = self.recording.split('void recordCaptureDiagnostics(short[] pcm)', 1)[1].split('public void run()', 1)[0]
        for action in ('Handler', 'postDelayed', 'Scheduled', 'Thread.sleep', 'new Thread',
                       'File', 'OutputStream', 'MediaRecorder', 'AudioRecord', 'setGain', 'setMicrophone'):
            self.assertNotIn(action, helpers)


if __name__ == '__main__':
    unittest.main()
