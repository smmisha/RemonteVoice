"""Source integration guard; device counters and JVM tests cover runtime/policy."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class AndroidPlaybackIntegrationTests(unittest.TestCase):
    def test_network_playback_does_not_keep_the_old_fixed_40ms_buffer(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        self.assertNotIn('Math.max(minimum, 480 * 2 * 4 * 4)', text)
        self.assertIn('PlaybackBufferPolicy', text)

    def test_playback_is_primed_on_worker_before_play(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        start = text.split('synchronized void startPlayback()', 1)[1].split('void offer(float[] samples)', 1)[0]
        self.assertNotIn('track.play()', start)
        self.assertIn('primedFrames >= bufferFrames', text)

    def test_session_acquires_and_releases_wifi_performance_lease(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/BridgeService.java').read_text(encoding='utf-8')
        self.assertIn('acquireWifiPerformance();', text)
        close = text.split('private void close(String message)', 1)[1].split('private void watchAudioProgress', 1)[0]
        self.assertIn('releaseWifiPerformance();', close)
        manifest = (ROOT / 'android/app/src/main/AndroidManifest.xml').read_text(encoding='utf-8')
        self.assertIn('android.permission.WAKE_LOCK', manifest)

    def test_diagnostics_do_not_call_native_track_while_holding_engine_monitor(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        report = text.split('void reportDiagnostics()', 1)[1].split('public void run()', 1)[0]
        self.assertNotIn('track.get', report)

    def test_offer_measures_wait_before_monitor_and_body_inside_monitor(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        self.assertNotIn('synchronized void offer', text)
        offer = text.split('void offer(float[] samples)', 1)[1].split('synchronized void assertProgress()', 1)[0]
        entry, playback_offer = offer.split('private void offer(Playback playback, float[] samples, long waitStarted, long packet)', 1)
        before_lock = entry.split('synchronized (this)', 1)[0]
        under_lock = playback_offer.split('synchronized (playback)', 1)[1]
        self.assertIn('long waitStarted = diagnostics ? System.nanoTime() : 0;', before_lock)
        self.assertIn('long acquiredAt = diagnostics ? System.nanoTime() : 0;', under_lock)
        self.assertIn('if (diagnostics && acquiredAt-waitStarted > playback.maxOfferWaitNanos)', under_lock)
        self.assertIn('playback.maxOfferBodyNanos = Math.max(playback.maxOfferBodyNanos, System.nanoTime()-acquiredAt)', under_lock)
        for mutation in ('playback.lastOffer = now', 'playback.offeredFrames++', 'playback.queue.offer(samples)', 'PlaybackQueueHandoff.offer('):
            self.assertNotIn(mutation, entry)
            self.assertIn(mutation, under_lock)
        self.assertNotIn('playback.queue.poll()', offer)
        self.assertNotIn('Log.', offer)
        self.assertNotIn('progress.advanced()', offer)

    def test_playback_stats_report_and_reset_per_playback_timing_maxima(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        holder = text.split('private final class Playback implements Runnable', 1)[1].split('void reportDiagnostics()', 1)[0]
        report = text.split('void reportDiagnostics()', 1)[1].split('public void run()', 1)[0]
        for field in ('maxOfferWaitNanos', 'maxOfferWaitAtNanos', 'maxOfferBodyNanos', 'maxDiagnosticLogNanos', 'maxWatchdogHoldNanos'):
            self.assertIn('volatile long '+field, holder)
            self.assertIn('TimeUnit.NANOSECONDS.toMillis('+field+')', report)
            self.assertIn(field+'=0;', report)
        for key in ('max_offer_wait_ms=', 'max_offer_wait_at_ms=', 'max_offer_body_ms=', 'max_diagnostic_log_ms=', 'max_watchdog_hold_ms='):
            self.assertIn(key, report)

    def test_offer_wait_timestamp_is_updated_only_for_the_chosen_maximum(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        offer = text.split('void offer(float[] samples)', 1)[1].split('synchronized void assertProgress()', 1)[0]
        maximum = offer.split('if (diagnostics && acquiredAt-waitStarted > playback.maxOfferWaitNanos)', 1)[1].split('}', 1)[0]
        self.assertIn('playback.maxOfferWaitNanos = acquiredAt-waitStarted;', maximum)
        self.assertIn('playback.maxOfferWaitAtNanos = acquiredAt-playback.startedAt;', maximum)
        self.assertEqual(1, offer.count('playback.maxOfferWaitAtNanos ='))
        self.assertNotIn('currentTimeMillis', offer)

    def test_watchdog_hold_covers_report_and_both_checks_under_the_same_monitor(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        watchdog = text.split('synchronized void assertProgress()', 1)[1].split('@SuppressLint', 1)[0]
        self.assertIn('long watchdogStarted = diagnostics ? System.nanoTime() : 0;', watchdog)
        before_finally, measured = watchdog.split('finally', 1)
        self.assertLess(before_finally.index('long watchdogStarted'), before_finally.index('try {'))
        self.assertIn('playback.reportDiagnostics()', before_finally)
        self.assertIn('!playback.progress.isFresh()', before_finally)
        self.assertIn('!recording.progress.isFresh()', before_finally)
        self.assertIn('if (diagnostics && playback != null && playback.running)', measured)
        self.assertIn('playback.maxWatchdogHoldNanos = Math.max(playback.maxWatchdogHoldNanos, System.nanoTime()-watchdogStarted)', measured)
        for action in ('progress.advanced()', 'track.', 'currentTimeMillis'):
            self.assertNotIn(action, watchdog)

    def test_diagnostic_log_timing_covers_log_call_without_audio_side_effects(self):
        text = (ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        report = text.split('void reportDiagnostics()', 1)[1].split('public void run()', 1)[0]
        self.assertLess(report.index('String message ='), report.index('long logStarted = System.nanoTime()'))
        self.assertLess(report.index('long logStarted = System.nanoTime()'), report.index('android.util.Log.i("RemonteVoiceSession", message)'))
        self.assertLess(report.index('android.util.Log.i("RemonteVoiceSession", message)'), report.index('System.nanoTime()-logStarted'))
        self.assertIn('maxDiagnosticLogNanos = Math.max(maxDiagnosticLogNanos, System.nanoTime()-logStarted)', report)
        for action in ('track.', 'progress.advanced()', 'onFailure.accept', 'startMicrophone', 'stopMicrophone'):
            self.assertNotIn(action, report)

    def test_underrun_rebuilds_reserve_instead_of_continuing_with_empty_track(self):
        text=(ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        self.assertIn('if (observedUnderruns > previousUnderruns)',text)
        recover=text.split('if (observedUnderruns > previousUnderruns)',1)[1].split('}',1)[0]
        for action in ('track.pause()', 'track.flush()', 'playing = false', 'primedFrames = 0'):
            self.assertIn(action,recover)
        self.assertIn('rebuffer_discarded_frames=',text)

    def test_wifi_latency_hint_has_independent_background_fallback_and_cleanup(self):
        text=(ROOT / 'android/app/src/main/java/dev/remontevoice/android/BridgeService.java').read_text(encoding='utf-8')
        self.assertIn('Build.VERSION.SDK_INT >= 29 && Build.VERSION.SDK_INT < 34',text)
        self.assertIn('wifiLatency = manager.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY',text)
        self.assertIn('wifiLatency = releaseWifiLease(wifiLatency)',text)
        self.assertIn('wifiPerformance = releaseWifiLease(wifiPerformance)',text)

    def test_zero_native_write_retries_without_advancing_packet_or_watchdog(self):
        text=(ROOT / 'android/app/src/main/java/dev/remontevoice/android/PhoneAudioEngine.java').read_text(encoding='utf-8')
        retry=text.split('if (written == 0)',1)[1].split('}',1)[0]
        self.assertIn('Thread.sleep(2)',retry)
        self.assertIn('continue;',retry)
        self.assertNotIn('progress.advanced()',retry)
        self.assertNotIn('offset +=',retry)
        self.assertIn('PlaybackWriteBudget.acceptedSamples(written,count,2)',text)

if __name__ == '__main__':
    unittest.main()
