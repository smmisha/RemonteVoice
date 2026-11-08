import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from remotevoice.host_app import HostWindow
from remotevoice.host_lease import HostLease, HostLeaseError
import test_host_output_routing as fixtures

headless_window = fixtures.headless_window


@unittest.skipUnless(os.name == 'nt', 'Windows byte-range locks')
class HostLeaseTests(unittest.TestCase):
    def test_exclusive_ownership_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host.lock'
            first, second = HostLease(path), HostLease(path)
            try:
                first.acquire()
                first.acquire()
                self.assertTrue(first.held)
                with self.assertRaises(HostLeaseError):
                    second.acquire()
                self.assertFalse(second.held)
                first.release()
                second.acquire()
                self.assertTrue(second.held)
            finally:
                first.release()
                second.release()

    def test_process_exit_releases_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'host.lock'
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'host'))
            code = ('import os,sys; from pathlib import Path; '
                    'from remotevoice.host_lease import HostLease; '
                    'lease=HostLease(Path(sys.argv[1])); lease.acquire(); os._exit(0)')
            result = subprocess.run([sys.executable, '-c', code, str(path)], env=env,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
            lease = HostLease(path)
            try:
                lease.acquire()
                self.assertTrue(lease.held)
            finally:
                lease.release()

    def test_second_constructor_cannot_enter_initializer_or_recover_routes(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            'remotevoice.host_app.tk.Tk.__init__', return_value=None
        ), patch('remotevoice.host_app.app_data_dir', return_value=Path(directory)), patch.object(
            HostWindow, '_initialize'
        ) as initialize, patch.object(HostWindow, 'destroy'), patch(
            'remotevoice.host_app.messagebox.showerror'
        ):
            first = HostWindow()
            try:
                with self.assertRaises(HostLeaseError):
                    HostWindow()
                initialize.assert_called_once_with()
            finally:
                first._host_lease.release()


class HostSessionSafetyTests(fixtures.HeadlessHostTests):
    def test_no_lease_means_no_recovery_mutations(self):
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        window._host_lease.held = False
        window.output_route = MagicMock()
        window.input_route = MagicMock()
        window.microphone_guard = MagicMock()
        self.assertFalse(window._recover_waiting_routes())
        self.assertFalse(window._restore_session_routes())
        self.assertFalse(window._activate_session_routes())
        for route in (window.output_route, window.input_route, window.microphone_guard):
            route.activate.assert_not_called()
            route.restore.assert_not_called()
        self.guard_class.recover.assert_not_called()

    def test_manual_return_recovers_previous_process_microphone_journal(self):
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        window._return_to_laptop()
        self.guard_class.recover.assert_called_once_with(window.data_dir / 'microphone-restore.json')
        self.assertIn('возвращены', window.status_var.get())

    def test_manual_return_does_not_claim_success_if_journal_recovery_failed(self):
        from remotevoice.microphone_guard import MicrophoneGuardError
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        self.guard_class.recover.side_effect = MicrophoneGuardError('old mic remains muted')
        window._return_to_laptop()
        self.assertNotIn('возвращены', window.status_var.get())
        self.assertIn('Не удалось восстановить', window.status_var.get())

    def test_partial_activation_restores_all_and_preserves_primary_error(self):
        window = headless_window()
        window.microphone_guard = MagicMock()
        window.input_route = MagicMock()
        from remotevoice.output_routing import OutputRoutingError
        window.output_route.activate.side_effect = OutputRoutingError('primary-output-failure')
        with self.assertLogs('remotevoice.ui', level='WARNING'):
            self.assertFalse(window._activate_session_routes())
        window.microphone_guard.restore.assert_called_once()
        window.input_route.restore.assert_called_once()
        window.output_route.restore.assert_called_once()
        self.assertIn('primary-output-failure', window._session_activation_error)
        self.assertFalse(window.server_running)

    def test_unexpected_adapter_failure_does_not_skip_other_recovery(self):
        window = headless_window()
        window.input_route = MagicMock()
        window.microphone_guard = MagicMock()
        window.output_route.restore.side_effect = RuntimeError('unexpected adapter failure')
        with self.assertLogs('remotevoice.ui', level='ERROR'):
            self.assertFalse(window._restore_session_routes())
        window.input_route.restore.assert_called_once()
        window.microphone_guard.restore.assert_called_once()

    def test_unexpected_disconnect_recovery_failure_preserves_guard_after_later_success(self):
        for failed_route in ('output_route', 'input_route', 'microphone_guard'):
            with self.subTest(route=failed_route):
                window = headless_window()
                window.input_route = MagicMock()
                window.microphone_guard = MagicMock()
                guard = window._safety_guard
                getattr(window, failed_route).restore.side_effect = [RuntimeError('adapter failed'), None]
                with self.assertLogs('remotevoice.ui', level='ERROR'):
                    window._set_status('server_ready')
                failure = window._session_restore_failure
                self.assertTrue(failure)
                self.assertIn(failure, window.status_var.get())
                for route in (window.output_route, window.input_route, window.microphone_guard):
                    route.restore.assert_called_once()
                window.server.stop.assert_called_once()
                self.assertTrue(window._stopping)

                # A later successful retry does not erase the failed disconnect
                # or authorize disarming the independent recovery process.
                window._set_status('server_stopped')
                self.assertEqual(window._session_restore_failure, failure)
                self.assertIn(failure, window.status_var.get())
                window._destroy_host()
                guard.disarm.assert_not_called()
                window._host_lease.release.assert_not_called()
                window.destroy.assert_called_once()

    def test_disconnect_without_recovery_ownership_retains_an_explicit_failure(self):
        window = headless_window()
        window._host_lease.held = False
        window._set_status('server_ready')
        self.assertTrue(window._session_restore_failure)
        self.assertIn(window._session_restore_failure, window.status_var.get())
        self.assertTrue(window._stopping)
        window.server.stop.assert_called_once()
        window.output_route.restore.assert_not_called()
        window._safety_guard.disarm.assert_not_called()

    def test_old_server_events_cannot_mutate_current_routes(self):
        window = headless_window()
        queued = []
        window.after = lambda delay, callback: queued.append(callback)
        window._server_generation = 5
        window._post_for_generation(4, 'iphone_connected_muted')
        window._post_for_generation(4, 'server_ready')
        for callback in queued:
            callback()
        window.output_route.activate.assert_not_called()
        window.output_route.restore.assert_not_called()

    def test_preparation_confirms_routes_before_returning_success(self):
        window = headless_window()
        window.after = lambda delay, callback: callback()
        self.assertTrue(window._request_session_start(0))
        window.output_route.activate.assert_called_once()

    def test_timed_out_ui_preparation_never_activates_later(self):
        window = headless_window()
        queued = []
        window.after = lambda delay, callback: queued.append(callback)
        with patch('remotevoice.host_app.threading.Event.wait', return_value=False):
            self.assertFalse(window._request_session_start(0))
        queued[0]()
        window.output_route.activate.assert_not_called()

    def test_blocked_cross_thread_after_cannot_activate_after_absolute_deadline(self):
        window = headless_window()
        now = [0.0]
        def delayed_after(delay, callback):
            now[0] = 6.0
            callback()
        window.after = delayed_after
        with patch('remotevoice.host_app.time.monotonic', side_effect=lambda: now[0]):
            self.assertFalse(window._request_session_start(0))
        window.output_route.activate.assert_not_called()

    def test_failed_worker_cleanup_blocks_new_host_even_after_network_thread_exits(self):
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        window.server.cleanup_failed = True
        window._recover_waiting_routes = MagicMock()
        window._start_host()
        window._recover_waiting_routes.assert_not_called()
        self.assertFalse(window.server_running)
        self.assertIn('Перезапустите RemoteVoice', window.status_var.get())

    def test_clean_close_releases_lease_but_unclean_workers_keep_it_until_process_exit(self):
        for failed in (False, True):
            with self.subTest(cleanup_failed=failed):
                window = headless_window()
                window.server.cleanup_failed = failed
                window._destroy_host()
                window.destroy.assert_called_once()
                self.assertEqual(window._host_lease.release.call_count, 0 if failed else 1)

    def test_guard_must_confirm_readiness_before_listener_can_start(self):
        from remotevoice.safety_guard import SafetyGuardError
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        self.safety_class.start.side_effect = SafetyGuardError('helper unavailable')
        with patch('remotevoice.host_app.RemoteVoiceServer') as server, patch(
            'remotevoice.host_app.messagebox.showerror'
        ):
            window._start_host()
        server.assert_not_called()
        self.assertFalse(window.server_running)
        self.assertIn('Подключение недоступно', window.status_var.get())

    def test_dead_guard_blocks_new_session_and_restores_devices(self):
        window = headless_window()
        window._safety_guard.armed = False
        self.assertFalse(window._activate_session_routes())
        window.output_route.activate.assert_not_called()
        window.output_route.restore.assert_called_once()
        window.server.stop.assert_called_once()

    def test_guard_death_during_active_session_restores_laptop(self):
        window = headless_window()
        window._safety_guard.armed = False
        window._check_safety_guard(0)
        window.output_route.restore.assert_called_once()
        window.server.stop.assert_called_once()
        self.assertIn('восстановления завершился', window.status_var.get())

    def test_restarting_saved_configuration_rearms_exact_route_snapshot(self):
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        old_guard = window._safety_guard
        with patch('remotevoice.host_app.RemoteVoiceServer'), patch(
            'remotevoice.host_app.threading.Thread'
        ):
            window._start_host()
        old_guard.disarm.assert_called_once()
        self.safety_class.start.assert_called_once_with(window.data_dir, 'vaio', 'laptop', 'b2', 'physical-mic')


if __name__ == '__main__':
    unittest.main()
