"""Source guards for fixed, debug-only fullscreen close observations."""

from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[1] / 'android/app/src/main/java/dev/remontevoice/android/MainActivity.java'


class AndroidViewerDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.text = SOURCE.read_text(encoding='utf-8')

    def test_close_reasons_are_a_fixed_enum_not_remote_or_display_text(self):
        reasons = self.text.split('private enum PreviewCloseReason {', 1)[1].split('}', 1)[0]
        self.assertEqual(set(re.findall(r'[A-Z][A-Z_]+', reasons)), {
            'COLLAPSE', 'HIDDEN', 'RESTART', 'SERVICE_DISCONNECTED', 'SESSION_CHANGED',
            'OFFER_ERROR', 'REQUEST_UNAVAILABLE', 'TRANSPORT_CLOSED', 'DECODE_FAILED',
            'ACTIVITY_PAUSE', 'ACTIVITY_STOP',
        })

    def test_explicit_close_logs_fixed_reason_before_the_existing_dismiss(self):
        close = self.text.split('private void closeFullscreen(PreviewCloseReason reason)', 1)[1].split('private void tracePreview', 1)[0]
        self.assertIn('if (dialog != null)', close)
        self.assertIn('tracePreview("fullscreen_close_requested reason=" + reason.name());', close)
        self.assertLess(close.index('tracePreview('), close.index('dialog.dismiss()'))
        self.assertIn('fullscreenDialog = null;', close)
        self.assertIn('fullscreenImage.clearFrame()', close)
        self.assertNotIn('message', close)
        self.assertIn('close.setOnClickListener(view -> closeFullscreen(PreviewCloseReason.COLLAPSE));', self.text)

    def test_cancel_is_observed_without_intercepting_back_or_changing_cancelability(self):
        open_view = self.text.split('private void openFullscreen()', 1)[1].split('private void closeFullscreen', 1)[0]
        self.assertIn('dialog.setOnCancelListener(ignored -> tracePreview("fullscreen_cancel"));', open_view)
        self.assertIn('dialog.setOnDismissListener(', open_view)
        self.assertIn('tracePreview("fullscreen_closed");', open_view)
        for behavior in ('setCancelable(', 'setCanceledOnTouchOutside(', 'setOnKeyListener(', 'onBackPressed('):
            self.assertNotIn(behavior, open_view)

    def test_preview_stop_propagates_a_typed_reason_and_keeps_message_out_of_logs(self):
        stop = self.text.split('private void stopPreview(String message, PreviewCloseReason reason)', 1)[1].split('private void openFullscreen()', 1)[0]
        self.assertIn('closeFullscreen(reason);', stop)
        self.assertIn('screenStatus.setText(message)', stop)
        self.assertNotIn('tracePreview(message', stop)
        self.assertNotIn('closeFullscreen();', self.text)
        for reason in ('SERVICE_DISCONNECTED', 'SESSION_CHANGED', 'OFFER_ERROR', 'REQUEST_UNAVAILABLE',
                       'TRANSPORT_CLOSED', 'DECODE_FAILED', 'HIDDEN', 'RESTART'):
            self.assertIn('PreviewCloseReason.' + reason, self.text)

    def test_pause_and_stop_mark_their_actual_existing_close_paths(self):
        pause = self.text.split('@Override protected void onPause()', 1)[1].split('@Override protected void onStop()', 1)[0]
        stop = self.text.split('@Override protected void onStop()', 1)[1].split('@Override protected void onNewIntent', 1)[0]
        self.assertIn('PreviewCloseReason.ACTIVITY_PAUSE', pause)
        self.assertIn('PreviewCloseReason.ACTIVITY_STOP', stop)
        self.assertIn('visible = false;', pause)
        self.assertIn('super.onPause();', pause)
        self.assertIn('unbindService(binding)', stop)
        self.assertIn('super.onStop();', stop)

    def test_frame_display_never_closes_fullscreen_and_trace_stays_debug_only(self):
        display = self.text.split('private void displayPreview(long epoch)', 1)[1].split('private void stopPreview', 1)[0]
        self.assertIn('fullscreenImage.showFrame(bitmap)', display)
        self.assertNotIn('closeFullscreen', display)
        self.assertNotIn('stopPreview(', display)
        trace = self.text.split('private void tracePreview(String event)', 1)[1].split('@Override public void onConfigurationChanged', 1)[0]
        self.assertLess(trace.index('FLAG_DEBUGGABLE'), trace.index('android.util.Log.i'))


if __name__ == '__main__':
    unittest.main()
