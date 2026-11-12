import unittest
from unittest.mock import MagicMock

from remontevoice.host_app import HostWindow
from test_host_output_routing import headless_window


class SessionStatusTests(unittest.TestCase):
    def test_retired_session_error_cannot_poison_next_preparation(self):
        window = headless_window()
        old = object()
        window.server.is_current_audio_session = MagicMock(return_value=False)
        HostWindow._post_audio_status(window, 0, old, "audio_error: retired")
        window._drain_screen_status()
        self.assertFalse(window._route_activation_failed)
        self.assertTrue(window._activate_session_routes())

    def test_event_queued_before_session_retired_is_checked_when_applied(self):
        window = headless_window()
        old = object()
        window._activate_session_routes()
        window.server.is_current_audio_session = MagicMock(return_value=True)
        HostWindow._post_audio_status(window, 0, old, "audio_error: retired")
        window.server.is_current_audio_session.return_value = False
        window._drain_screen_status()
        self.assertTrue(window._output_route_active)
        self.assertFalse(window._route_activation_failed)

    def test_current_session_error_still_restores_devices(self):
        window = headless_window()
        window._activate_session_routes()
        window.server.is_current_audio_session = MagicMock(return_value=True)
        HostWindow._post_audio_status(window, 0, object(), "audio_error: current")
        window._drain_screen_status()
        self.assertFalse(window._output_route_active)
        self.assertTrue(window._route_activation_failed)

    def test_restarted_server_ignores_previous_audio_event(self):
        window = headless_window()
        window._server_generation = 2
        window.server.is_current_audio_session = MagicMock(return_value=True)
        HostWindow._post_audio_status(window, 1, object(), "audio_error: retired")
        window._drain_screen_status()
        self.assertFalse(window._route_activation_failed)

    def test_capture_status_publication_never_calls_tk(self):
        window = headless_window()
        window.after = MagicMock(side_effect=AssertionError('Capture touched Tk'))
        session = object()
        window.server.is_current_audio_session.return_value = True
        window._post_audio_status(0, session, 'audio_active')
        window.after.assert_not_called()
        self.assertEqual(window._pending_audio_event, (0, session, 'audio_active'))

    def test_error_is_not_overwritten_by_late_active_status(self):
        window = headless_window()
        window._activate_session_routes()
        window.server.is_current_audio_session.return_value = True
        session = object()
        window._post_audio_status(0, session, 'audio_error: failed')
        for _ in range(1000):
            window._post_audio_status(0, session, 'audio_active')
        window._drain_screen_status()
        self.assertFalse(window._output_route_active)
        self.assertTrue(window._route_activation_failed)
        self.assertIsNone(window._pending_audio_event)

    def test_old_session_cannot_replace_pending_current_error(self):
        window = headless_window()
        old, current = object(), object()
        window.server.is_current_audio_session.side_effect = lambda session: session is current
        window._post_audio_status(0, current, 'audio_error: current')
        window._post_audio_status(0, old, 'audio_active')
        self.assertEqual(window._pending_audio_event, (0, current, 'audio_error: current'))

    def test_capture_indicator_cannot_hide_pending_pairing_refresh(self):
        window = headless_window()
        window._activate_session_routes()
        window.server.is_current_audio_session.return_value = True
        session = object()
        window._post_audio_status(0, session, 'iphone_connected_muted')
        window._post_audio_status(0, session, 'audio_active')
        window._drain_screen_status()
        window._refresh_pairing_ui.assert_called_once_with()

    def test_closed_window_rejects_audio_events_without_tk(self):
        window = headless_window()
        window._screen_events_closed = True
        window._post_audio_status(0, object(), 'audio_error: closed')
        window.after.assert_not_called()
        self.assertIsNone(window._pending_audio_event)
