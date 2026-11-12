import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from remontevoice.audio_backend import DriverBackendSnapshot
from remontevoice.host_app import HostWindow
from remontevoice.driver_route_recovery import DriverRouteRecovery, DriverRecoveryError
from remontevoice.installed_driver_host import InstalledDriverHost
import test_host_output_routing as fixtures
import test_driver_backend_runtime as runtime_fixtures
from remontevoice.driver_backend_runtime import DriverBackendRuntime

headless_window = fixtures.headless_window


def controller():
    owner=Mock()
    owner.backend.is_available.return_value=True
    owner.backend.matches_endpoints.return_value=True
    owner.backend.unavailable_message='Аудиокомпонент недоступен'
    owner.guard_is_available.return_value=True
    owner.snapshot=SimpleNamespace(cleanup_confirmed=False)
    owner.observe.return_value=DriverBackendSnapshot(1,True,True,'own-render','own-mic',10.0)
    owner.stop.side_effect=lambda **_:setattr(owner.snapshot,'cleanup_confirmed',True)
    return owner


class HostDriverModeTests(fixtures.HeadlessHostTests):
    def test_exact_driver_ids_bypass_mixer_and_preserve_legacy_selections(self):
        window=fixtures.HostOutputRoutingTests.configured_window(self)
        owner=controller();window._driver_runtime_factory=Mock();window._driver_runtime=owner
        window._driver_ready=True
        window.config_data={'playback_device_id':'legacy-aux','capture_device_id':'legacy-b1'}
        with patch('remontevoice.host_app.OutputRoute') as output, \
             patch('remontevoice.host_app.RemonteVoiceServer') as server, \
             patch('remontevoice.host_app.save_driver_recovery') as save, \
             patch('remontevoice.host_app.threading.Thread'):
            window._start_host()
        self.mixer_configure.assert_not_called()
        output.assert_called_once_with('own-render','laptop')
        self.assertEqual(self.input_class.call_args.args,('own-mic','physical-mic'))
        self.assertEqual(server.call_args.kwargs['capture_device_id'],'own-render')
        self.assertEqual(server.call_args.kwargs['playback_device_id'],'own-mic')
        factory=server.call_args.kwargs['audio_factory']
        self.assertIs(factory.keywords['driver_backend'],owner.backend)
        self.assertEqual(window.config_data['playback_device_id'],'legacy-aux')
        self.assertEqual(window.config_data['capture_device_id'],'legacy-b1')
        self.assertEqual(self.safety_class.start.call_args.args[1:],
                         ('own-render','laptop','own-mic','physical-mic'))
        self.assertEqual(save.call_args.args[1],DriverRouteRecovery('own-render','laptop','own-mic','physical-mic'))

    def test_startup_runs_on_worker_and_does_not_open_server_or_mute_before_ready(self):
        window=headless_window();window.server_running=False
        owner=controller();window._driver_runtime_factory=Mock(return_value=owner)
        window._start_host=Mock()
        caller=threading.get_ident();called=[]
        owner.start.side_effect=lambda:called.append(threading.get_ident())
        self.assertFalse(window._prepare_driver_runtime())
        window._driver_startup_worker.join(1)
        self.assertFalse(window._driver_startup_worker.is_alive())
        self.assertNotEqual(called,[caller])
        window.output_route.activate.assert_not_called()
        window._finish_driver_startup(window._driver_generation)
        window._start_host.assert_called_once()
        self.assertTrue(window._driver_ready)

    def test_cancelled_startup_cannot_start_server_or_reactivate_after_stop(self):
        window=headless_window();window.server_running=False;window.server=None
        owner=controller();entered=threading.Event();release=threading.Event()
        owner.start.side_effect=lambda:(entered.set(),release.wait(1))
        window._driver_runtime_factory=Mock(return_value=owner)
        window._start_host=Mock()
        window._prepare_driver_runtime();token=window._driver_generation
        self.assertTrue(entered.wait(1))
        window._stop_host();release.set();window._driver_startup_worker.join(1)
        window._finish_driver_startup(token)
        window._start_host.assert_not_called()
        owner.stop.assert_called_once_with(timeout=1.0)
        self.assertTrue(window._driver_cancelled)
        window._finish_stop_when_ready()
        self.assertFalse(window.server_running)

    def test_stop_during_real_controller_manifest_load_prevents_late_runtime_creation(self):
        window=headless_window();window.server_running=False;window.server=None
        owner=InstalledDriverHost();window._driver_runtime_factory=Mock(return_value=owner)
        entered=threading.Event();release=threading.Event()
        cancelled_when_restoring=[]
        window.output_route.restore.side_effect=lambda:cancelled_when_restoring.append(owner.snapshot.stopping)
        manifest=Mock(is_verified=True,cleanup_confirmed=False)
        def load():
            entered.set()
            if not release.wait(2):raise RuntimeError('test manifest not released')
            return SimpleNamespace(driver_instance_id=r'ROOT\MEDIA\0000')
        manifest.load.side_effect=load
        manifest.close.side_effect=lambda:setattr(manifest,'cleanup_confirmed',True)
        runtime=Mock(snapshot=SimpleNamespace(cleanup_confirmed=False))
        runtime.stop.side_effect=lambda **_:setattr(runtime.snapshot,'cleanup_confirmed',True)
        runtime.observe.return_value=DriverBackendSnapshot(1,True,True,'render','mic',0.0)
        with patch('remontevoice.installed_driver_host.PinnedInstalledManifest',return_value=manifest), \
             patch('remontevoice.installed_driver_host.WindowsDriverEndpointInventory') as inventory, \
             patch('remontevoice.installed_driver_host.DriverBackendRuntime',return_value=runtime) as make_runtime:
            try:
                window._prepare_driver_runtime()
                self.assertTrue(entered.wait(1))
                window._stop_host()
                release.set();window._driver_startup_worker.join(2)
                self.assertFalse(window._driver_startup_worker.is_alive())
                inventory.assert_not_called();make_runtime.assert_not_called()
                self.assertTrue(owner.snapshot.cleanup_confirmed)
                window.output_route.restore.assert_called()
                self.assertEqual(cancelled_when_restoring,[True])
            finally:
                release.set()
                if window._driver_startup_worker is not None:window._driver_startup_worker.join(2)
                owner.stop()

    def test_cancellation_signal_failure_does_not_skip_routes_server_or_final_cleanup(self):
        window=headless_window();owner=controller()
        window._driver_runtime_factory=Mock();window._driver_runtime=owner
        owner.request_stop.side_effect=RuntimeError('signal failed')
        window._stop_host()
        if window._driver_cleanup_worker is not None:window._driver_cleanup_worker.join(1)
        window.output_route.restore.assert_called_once()
        window.server.stop.assert_called_once()
        owner.stop.assert_called_once_with(timeout=1.0)
        self.assertTrue(window._driver_cleanup_confirmed())

    def test_server_self_stop_closes_runtime_and_allows_new_clean_controller(self):
        window=headless_window();owner=controller();window._driver_runtime=owner
        window._driver_runtime_factory=Mock(return_value=controller())
        window._driver_ready=True;window._driver_cancelled=False
        server=SimpleNamespace(run=lambda:object())
        with patch('remontevoice.host_app.asyncio.run'):
            HostWindow._run_server(window,server,0)
        owner.stop.assert_called_once_with(timeout=1.0)
        self.assertFalse(window._driver_ready);self.assertTrue(window._driver_cancelled)
        self.assertTrue(window._driver_cleanup_confirmed())
        window._prepare_driver_runtime();window._driver_startup_worker.join(1)
        window._driver_runtime_factory.assert_called_once()
        self.assertIsNot(window._driver_runtime,owner)
        window._driver_runtime.stop()

    def test_timer_allows_explicit_transition_but_route_activation_stays_strict(self):
        window=headless_window();owner=controller()
        window._driver_runtime_factory=Mock();window._driver_runtime=owner
        window._audio_backend=owner.backend;owner.backend.is_available.return_value=False
        window._check_safety_guard(0)
        window.server.stop.assert_not_called();window.output_route.restore.assert_not_called()
        self.assertEqual(window.after.call_args.args[0],500)
        window._stop_host=Mock()
        self.assertFalse(window._activate_session_routes())
        window._stop_host.assert_called_once()
        window.output_route.activate.assert_not_called()

    def test_timer_does_not_stop_host_while_previous_phone_mute_is_still_confirming(self):
        sink = runtime_fixtures.Sink()
        inventory = Mock(cleanup_confirmed=True)
        inventory.observe.return_value = runtime_fixtures.PAIR
        runtime = DriverBackendRuntime(inventory, lambda: sink)
        entered, release = threading.Event(), threading.Event()
        worker = None
        outcome = []
        try:
            runtime.start()
            runtime_fixtures.wait_for(runtime.backend.is_available)
            lease = runtime.acquire_sink()
            lease.start()
            self.assertTrue(lease.set_enabled(True))

            def mute(enabled):
                if not enabled:
                    sink.available = False
                    entered.set()
                    release.wait(1)
                    sink.available = True

            def end_phone():
                try:
                    lease.stop(timeout=0.8)
                except BaseException as error:
                    outcome.append(error)

            sink.control_hook = mute
            worker = threading.Thread(target=end_phone)
            worker.start()
            self.assertTrue(entered.wait(1))
            runtime_fixtures.wait_for(lambda: not runtime.backend.is_available())
            window = headless_window()
            window._driver_runtime_factory = Mock()
            window._driver_runtime = runtime
            window._audio_backend = runtime.backend
            window._stop_host = Mock()
            window._check_safety_guard(0)
            window._stop_host.assert_not_called()
            self.assertEqual(window.after.call_args.args[0], 500)
            self.assertFalse(lease.submit(b'old phone PCM'))
        finally:
            release.set()
            if worker is not None:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            runtime.stop()
            self.assertTrue(runtime.snapshot.cleanup_confirmed)
        self.assertEqual(outcome, [])

    def test_failed_runtime_cleanup_never_releases_host_or_recovery_guard(self):
        window=headless_window();owner=controller();window._driver_runtime=owner
        window._driver_runtime_factory=Mock();window._driver_cleanup_error='retained'
        window._set_running_controls(False)
        self.assertEqual(window.start_button['state'],'disabled')
        window._destroy_host()
        window._host_lease.release.assert_not_called()
        window._safety_guard.disarm.assert_not_called()
        window.destroy.assert_called_once()

    def test_cold_recovery_uses_saved_own_routes_even_with_broker_unavailable(self):
        window=headless_window();window.server_running=False
        plan=DriverRouteRecovery('own-render','laptop','own-mic','physical-mic')
        with patch('remontevoice.host_app.load_driver_recovery',return_value=plan), \
             patch('remontevoice.host_app.OutputRoute') as output, \
             patch('remontevoice.host_app.InputRoute') as input_route:
            self.assertTrue(window._recover_waiting_routes())
        output.assert_called_once_with('own-render','laptop')
        input_route.assert_called_once_with('own-mic','physical-mic')
        output.return_value.restore.assert_called_once();input_route.return_value.restore.assert_called_once()
        self.assertIsNone(window._audio_backend)

    def test_recovery_failure_still_attempts_other_routes_and_blocks_listener(self):
        window=headless_window();window.server_running=False
        plan=DriverRouteRecovery('own-render','laptop','own-mic','physical-mic')
        with patch('remontevoice.host_app.load_driver_recovery',return_value=plan), \
             patch('remontevoice.host_app.OutputRoute') as output, \
             patch('remontevoice.host_app.InputRoute') as input_route:
            output.return_value.restore.side_effect=RuntimeError('device missing')
            self.assertFalse(window._recover_waiting_routes())
        input_route.return_value.restore.assert_called_once()
        window.output_route.restore.assert_called_once()
        self.assertIn('сохранённые устройства',window._session_restore_failure)

    def test_failed_durable_plan_save_never_opens_listener_or_arms_routes(self):
        window=fixtures.HostOutputRoutingTests.configured_window(self)
        owner=controller();window._driver_runtime_factory=Mock();window._driver_runtime=owner
        window._driver_ready=True
        with patch('remontevoice.host_app.OutputRoute'), \
             patch('remontevoice.host_app.save_driver_recovery',side_effect=DriverRecoveryError()), \
             patch('remontevoice.host_app.RemonteVoiceServer') as server:
            window._start_host()
        server.assert_not_called();self.safety_class.start.assert_not_called()
        self.assertFalse(window.server_running)

    def test_failed_retained_cleanup_is_visible_when_no_worker_is_alive(self):
        window=headless_window();window.server_running=False;window.server=None
        window._driver_runtime=controller();window._driver_runtime_factory=Mock()
        window._driver_cleanup_error='Остановка аудиокомпонента не подтверждена'
        window._stop_host()
        self.assertIn('не подтверждена',window.status_var.get())
        self.assertEqual(window.start_button['state'],'disabled')
        self.assertIsNone(window._driver_cleanup_worker)

    def test_invalid_user_inputs_do_not_boot_driver_or_leave_stop_inaccessible(self):
        window=fixtures.HostOutputRoutingTests.configured_window(self)
        window._driver_runtime_factory=Mock();window.ip_var.set('')
        with patch('remontevoice.host_app.messagebox.showerror'):
            window._start_host()
        window._driver_runtime_factory.assert_not_called()
        owner=controller();window._driver_runtime=owner;window._driver_ready=True
        window._set_running_controls(False)
        self.assertEqual(window.stop_button['state'],'normal')


if __name__=='__main__':unittest.main()
