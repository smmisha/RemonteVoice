import unittest
from unittest.mock import MagicMock, patch
from test_host_output_routing import headless_window
from remotevoice.audio_backend import AudioBackend
from remotevoice.audio_backend import DriverAudioBackend, DriverBackendSnapshot


class MixerHealthTests(unittest.TestCase):
    def test_driver_lost_before_activation_does_not_mute_or_reroute(self):
        for mode in ('unavailable', 'exception'):
            with self.subTest(mode=mode):
                window = headless_window()
                window.input_route = MagicMock()
                window.microphone_guard = MagicMock()
                window._audio_backend = MagicMock(unavailable_message='Аудиокомпонент недоступен')
                if mode == 'exception':
                    window._audio_backend.is_available.side_effect = OSError('offline')
                else:
                    window._audio_backend.is_available.return_value = False
                self.assertFalse(window._activate_session_routes())
                window.output_route.activate.assert_not_called()
                window.input_route.activate.assert_not_called()
                window.microphone_guard.activate.assert_not_called()
                window.output_route.restore.assert_called_once()
                window.input_route.restore.assert_called_once()
                window.microphone_guard.restore.assert_called_once()
                window.server.stop.assert_called_once()

    def test_expired_driver_status_restores_all_routes_without_polling_mixer(self):
        window = headless_window()
        window.input_route = MagicMock()
        window.microphone_guard = MagicMock()
        old = DriverBackendSnapshot(1, True, True, 'render', 'capture', 1.0)
        window._audio_backend = DriverAudioBackend(lambda: old, clock=lambda: 2.0)
        with patch('remotevoice.audio_backend.standard_mixer_running') as mixer:
            window._check_safety_guard(0)
        mixer.assert_not_called()
        window.server.stop.assert_called_once()
        for route in (window.output_route, window.input_route, window.microphone_guard):
            route.restore.assert_called_once()

    def test_backend_health_exception_also_restores_routes(self):
        window = headless_window()
        window._audio_backend = MagicMock(
            is_available=MagicMock(side_effect=OSError('component unavailable')),
            unavailable_message='Аудиокомпонент недоступен',
        )
        window._check_safety_guard(0)
        window.server.stop.assert_called_once()
        window.output_route.restore.assert_called_once()
        self.assertFalse(window.server_running)
        window.after.assert_not_called()

    def test_missing_mixer_stops_session_and_restores_every_managed_route(self):
        window = headless_window()
        window._audio_backend = AudioBackend(True)
        window.input_route = MagicMock()
        window.microphone_guard = MagicMock()
        with patch('remotevoice.audio_backend.standard_mixer_running', return_value=False):
            window._check_safety_guard(window._server_generation)
        window.server.stop.assert_called_once()
        window.output_route.restore.assert_called_once()
        window.input_route.restore.assert_called_once()
        window.microphone_guard.restore.assert_called_once()
        self.assertFalse(window.server_running)
        self.assertIn('микшер', window._session_activation_error)
        window.after.assert_not_called()

    def test_healthy_mixer_keeps_session_and_schedules_next_read_only_poll(self):
        window = headless_window()
        window._audio_backend = AudioBackend(True)
        with patch('remotevoice.audio_backend.standard_mixer_running', return_value=True):
            window._check_safety_guard(0)
        window.server.stop.assert_not_called()
        window.output_route.restore.assert_not_called()
        window.after.assert_called_once_with(500, window._check_safety_guard, 0)

    def test_stale_poll_and_other_audio_backend_do_not_query_voicemeeter(self):
        for generation, standard in ((1, True), (0, False)):
            window = headless_window()
            window._audio_backend = AudioBackend(standard)
            with patch('remotevoice.audio_backend.standard_mixer_running') as poll:
                window._check_safety_guard(generation)
            poll.assert_not_called()
            window.server.stop.assert_not_called()
