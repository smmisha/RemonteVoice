import unittest
from dataclasses import replace
from unittest.mock import patch

from remotevoice.audio_backend import AudioBackend, AudioBackendError, prepare_audio_backend
from remotevoice.audio_backend import DriverAudioBackend, DriverBackendSnapshot, prepare_driver_audio_backend
from remotevoice import audio_backend
from remotevoice.voicemeeter_routes import VoicemeeterRoutingError


class AudioBackendTests(unittest.TestCase):
    def test_final_close_delegates_without_reconfiguring_routes(self):
        with patch('remotevoice.audio_backend.close_standard_mixer_session') as close, patch(
            'remotevoice.audio_backend.configure_standard_routes',
        ) as configure:
            audio_backend.close_audio_backend()
        close.assert_called_once_with()
        configure.assert_not_called()

    def test_final_close_preserves_sanitized_error_as_backend_error(self):
        original = VoicemeeterRoutingError('Known SDK close failure')
        with patch('remotevoice.audio_backend.close_standard_mixer_session', side_effect=original):
            with self.assertRaises(AudioBackendError) as raised:
                audio_backend.close_audio_backend()
        self.assertIs(raised.exception.__cause__, original)
        self.assertEqual(str(raised.exception), str(original))

    def test_preparation_keeps_selected_pair_and_legacy_adapter_result(self):
        for external in (False, True):
            with self.subTest(external=external), patch(
                'remotevoice.audio_backend.configure_standard_routes', return_value=external,
            ) as prepare:
                backend = prepare_audio_backend('selected capture', 'selected playback')
                prepare.assert_called_once_with('selected capture', 'selected playback')
                self.assertIs(backend.uses_external_mixer, external)

    def test_generic_pairs_never_query_or_start_mixer(self):
        with patch('remotevoice.audio_backend.standard_mixer_running') as poll:
            self.assertTrue(AudioBackend(False).is_available())
        poll.assert_not_called()

    def test_failed_health_and_health_exception_are_unavailable(self):
        for options in ({'return_value': False}, {'side_effect': OSError('unavailable')}):
            with patch('remotevoice.audio_backend.standard_mixer_running', **options):
                self.assertFalse(AudioBackend(True).is_available())

    def test_route_preparation_error_keeps_existing_explanation(self):
        original = VoicemeeterRoutingError('Known route could not be confirmed')
        with patch('remotevoice.audio_backend.configure_standard_routes', side_effect=original):
            with self.assertRaises(AudioBackendError) as raised:
                prepare_audio_backend('capture', 'playback')
        self.assertIs(raised.exception.__cause__, original)
        self.assertEqual(str(raised.exception), str(original))


class DriverBackendHealthTests(unittest.TestCase):
    def setUp(self):
        self.status = DriverBackendSnapshot(1, True, True, 'render-id', 'capture-id', 10.0)

    def backend(self, **changes):
        return DriverAudioBackend(lambda: replace(self.status, **changes), clock=lambda: 10.1)

    def test_idle_muted_driver_is_healthy_without_starting_mixer(self):
        with patch('remotevoice.audio_backend.standard_mixer_running') as poll, patch(
            'remotevoice.audio_backend.configure_standard_routes',
        ) as prepare:
            backend = prepare_driver_audio_backend(lambda: self.status, clock=lambda: 10.1)
            self.assertTrue(backend.is_available())
            self.assertFalse(backend.uses_external_mixer)
        prepare.assert_not_called()
        poll.assert_not_called()

    def test_active_microphone_requires_acknowledged_writer(self):
        self.assertFalse(self.backend(mic_requested=True).is_available())
        self.assertTrue(self.backend(mic_requested=True, mic_writer_ready=True).is_available())

    def test_untrusted_incomplete_or_expired_observations_fail_closed(self):
        for changes in (
            {'broker_authenticated': False}, {'broker_authenticated': 'true'},
            {'endpoints_verified': False}, {'protocol_version': 2}, {'protocol_version': True},
            {'capture_endpoint_id': ''}, {'microphone_endpoint_id': 'RENDER-ID'},
            {'microphone_endpoint_id': 'x' * 1025}, {'observed_at': 9.0},
            {'observed_at': 11.0}, {'observed_at': float('nan')},
            {'observed_at': float('inf')}, {'cleanup_pending': True},
            {'mic_requested': 'false'}, {'mic_writer_ready': None},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(self.backend(**changes).is_available())

    def test_missing_service_and_failed_observation_cannot_prepare_routes(self):
        for observe in (lambda: None, lambda: {}, lambda: (_ for _ in ()).throw(OSError('offline'))):
            with self.subTest(observe=observe):
                self.assertFalse(DriverAudioBackend(observe).is_available())
                with self.assertRaises(AudioBackendError):
                    prepare_driver_audio_backend(observe)

    def test_poll_does_not_renew_a_stale_monitor_snapshot(self):
        clock = [10.0]
        backend = DriverAudioBackend(lambda: self.status, clock=lambda: clock[0])
        self.assertTrue(backend.is_available())
        clock[0] = 11.0
        self.assertFalse(backend.is_available())
