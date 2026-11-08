"""Screen controls use separate UI state and never activate an audio route."""
import unittest
import threading
from unittest.mock import MagicMock

from test_host_output_routing import headless_window


class HostScreenUiTests(unittest.TestCase):
    def test_permission_is_explicit_and_not_saved_in_config(self):
        window = headless_window()
        window._update_screen_permission()
        window.server.set_screen_allowed.assert_called_once_with(False)
        self.assertEqual(window.screen_status_var.get(), "Просмотр экрана выключен")
        window.screen_allowed_var.set(True)
        window._update_screen_permission()
        window.server.set_screen_allowed.assert_called_with(True)
        self.assertIn("Просмотр разрешён", window.screen_status_var.get())
        window.output_route.activate.assert_not_called()

    def test_indicator_does_not_overwrite_audio_status_or_routes(self):
        window = headless_window()
        window.status_var.set("Микрофон включён")
        window.screen_allowed_var.set(True)
        for status, expected in (
            ("screen_active", "передаётся"),
            ("screen_stopped", "Просмотр разрешён"),
            ("screen_error", "Не удалось"),
        ):
            window.server.screen_streaming = status == "screen_active"
            window._set_screen_status(status)
            self.assertIn(expected, window.screen_status_var.get())
        self.assertEqual(window.status_var.get(), "Микрофон включён")
        window.output_route.activate.assert_not_called()
        window.output_route.restore.assert_not_called()

    def test_late_active_callback_cannot_override_revocation(self):
        window = headless_window()
        window._set_screen_status("screen_active")
        self.assertEqual(window.screen_status_var.get(), "Просмотр экрана выключен")

    def test_status_from_server_thread_is_posted_to_ui(self):
        window = headless_window()
        window.after = MagicMock()
        window._post_screen_status("screen_active")
        window.after.assert_not_called()
        self.assertEqual(window._pending_screen_event, (0, 'screen_active'))
        window._drain_screen_status()
        window.after.assert_called_once_with(25, window._drain_screen_status)

    def test_active_event_after_stop_does_not_show_live_indicator(self):
        window = headless_window()
        window.screen_allowed_var.set(True)
        window.server_running = False
        window._set_screen_status("screen_active")
        self.assertIn("подключение остановлено", window.screen_status_var.get())

    def test_retired_preview_stop_cannot_hide_current_stream(self):
        window = headless_window()
        window.screen_allowed_var.set(True)
        window.server.screen_streaming = True
        window._set_screen_status("screen_stopped")
        self.assertIn("передаётся", window.screen_status_var.get())
        window._set_screen_status("screen_error")
        self.assertIn("передаётся", window.screen_status_var.get())

    def test_queued_active_event_cannot_claim_a_closed_preview_is_live(self):
        window = headless_window()
        window.screen_allowed_var.set(True)
        window.server.screen_streaming = False
        window._post_for_generation(0, "screen_active", screen=True)
        window._drain_screen_status()
        self.assertNotIn("передаётся", window.screen_status_var.get())

    def test_callback_after_window_destruction_is_ignored(self):
        window = headless_window()
        window._screen_events_closed = True
        window.after = MagicMock(side_effect=RuntimeError("UI closed"))
        window._post_screen_status("screen_stopped")
        self.assertIsNone(window._pending_screen_event)
        window.after.assert_not_called()

    def test_busy_or_destroyed_tk_cannot_block_server_screen_publication(self):
        window = headless_window()
        window.after = MagicMock(side_effect=AssertionError('Worker touched Tk'))
        done = threading.Event()
        def post():
            window._post_for_generation(0, 'screen_active', screen=True)
            done.set()
        worker = threading.Thread(target=post)
        worker.start()
        self.assertTrue(done.wait(1))
        worker.join(1)
        window.after.assert_not_called()
        self.assertEqual(window._pending_screen_event, (0, 'screen_active'))

    def test_mailbox_coalesces_only_screen_indicators_and_discards_retired_generation(self):
        window = headless_window()
        for _ in range(1000):
            window._post_for_generation(0, 'screen_active', screen=True)
        window._post_for_generation(0, 'screen_stopped', screen=True)
        self.assertEqual(window._pending_screen_event, (0, 'screen_stopped'))
        window._set_screen_status = MagicMock()
        window._server_generation = 1
        window._drain_screen_status()
        window._set_screen_status.assert_not_called()
        self.assertIsNone(window._pending_screen_event)

    def test_old_worker_cannot_overwrite_current_pending_indicator(self):
        window = headless_window()
        window._server_generation = 2
        window._post_for_generation(2, 'screen_active', screen=True)
        window._post_for_generation(1, 'screen_stopped', screen=True)
        self.assertEqual(window._pending_screen_event, (2, 'screen_active'))


if __name__ == "__main__":
    unittest.main()
