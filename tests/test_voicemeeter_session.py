import ctypes
import threading
import unittest
from unittest.mock import MagicMock, patch

from remontevoice import voicemeeter_routes as routes
from test_voicemeeter_routes import FakeApi, FakeClock


class VoicemeeterSessionTests(unittest.TestCase):
    def test_repeated_health_observation_reuses_one_login_without_logout(self):
        api = MagicMock()
        api.VBVMR_Login.return_value = 0
        api.VBVMR_Logout.return_value = 0
        api.VBVMR_IsParametersDirty.return_value = 1

        def product(out):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_int32))[0] = 2
            return 0

        api.VBVMR_GetVoicemeeterType.side_effect = product
        session = routes._MixerSession()
        self.addCleanup(session.close)
        with patch.object(routes, '_session', session), patch.object(routes, '_load_api', return_value=api):
            self.assertTrue(all(routes.standard_mixer_running() for _ in range(10)))
        api.VBVMR_Login.assert_called_once_with()
        api.VBVMR_Logout.assert_not_called()
        api.VBVMR_RunVoicemeeter.assert_not_called()
        api.VBVMR_SetParameterFloat.assert_not_called()

    def test_prepare_health_and_reconfigure_share_the_same_lazy_session(self):
        api = FakeApi()
        session = routes._MixerSession()
        with patch.object(routes, '_session', session), patch.object(routes, '_load_api', return_value=api) as load:
            load.assert_not_called()
            api.VBVMR_Login.assert_not_called()
            self.assertTrue(routes.configure_standard_routes('Voicemeeter Out B1', 'Voicemeeter AUX Input'))
            self.assertTrue(all(routes.standard_mixer_running() for _ in range(10)))
            self.assertTrue(routes.configure_standard_routes('Voicemeeter Output', 'Voicemeeter AUX Input'))
            api.VBVMR_Login.assert_called_once_with()
            api.VBVMR_Logout.assert_not_called()
            load.assert_called_once_with()
            routes.close_standard_mixer_session()
            routes.close_standard_mixer_session()
            api.VBVMR_Logout.assert_called_once_with()
        self.assertEqual(api.values, routes._ROUTES)
        self.assertEqual(api.VBVMR_SetParameterFloat.call_count, 40)

    def test_close_before_first_use_is_inert_and_terminal(self):
        load = MagicMock(return_value=FakeApi())
        session = routes._MixerSession(load)
        session.close()
        session.close()
        self.assertFalse(session.is_running())
        with self.assertRaises(routes.VoicemeeterRoutingError):
            session.configure_routes()
        load.assert_not_called()

    def test_successful_close_never_reopens_or_rewrites_routes(self):
        api = FakeApi()
        load = MagicMock(return_value=api)
        session = routes._MixerSession(load)
        self.assertTrue(session.is_running())
        session.close()
        session.close()
        self.assertFalse(session.is_running())
        with self.assertRaises(routes.VoicemeeterRoutingError):
            session.configure_routes()
        load.assert_called_once_with()
        api.VBVMR_Login.assert_called_once_with()
        api.VBVMR_Logout.assert_called_once_with()
        api.VBVMR_SetParameterFloat.assert_not_called()

    def test_failed_login_never_logs_out_an_unowned_session_or_retries_login(self):
        for failure in (-1, -2, 99, OSError('PRIVATE LOGIN DETAIL')):
            with self.subTest(failure=type(failure).__name__):
                api = FakeApi()
                if isinstance(failure, Exception):
                    api.VBVMR_Login.side_effect = failure
                else:
                    api.VBVMR_Login.return_value = failure
                session = routes._MixerSession(lambda: api)
                self.assertFalse(session.is_running())
                self.assertFalse(session.is_running())
                with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                    session.configure_routes()
                self.assertEqual(str(error.exception), routes._ERROR)
                session.close()
                api.VBVMR_Login.assert_called_once_with()
                api.VBVMR_Logout.assert_not_called()
                api.VBVMR_IsParametersDirty.assert_not_called()
                api.VBVMR_RunVoicemeeter.assert_not_called()
                api.VBVMR_SetParameterFloat.assert_not_called()

    def test_dll_load_failure_before_login_can_retry_without_native_cleanup(self):
        api = FakeApi()
        load = MagicMock(side_effect=[OSError('PRIVATE DLL PATH'), api])
        session = routes._MixerSession(load)
        self.assertFalse(session.is_running())
        self.assertTrue(session.is_running())
        self.assertEqual(load.call_count, 2)
        api.VBVMR_Login.assert_called_once_with()
        api.VBVMR_Logout.assert_not_called()
        session.close()
        api.VBVMR_Logout.assert_called_once_with()

    def test_health_observes_mixer_disappearance_and_reappearance_without_relogin_or_launch(self):
        api = FakeApi()
        api.VBVMR_Login.return_value = 1
        api.VBVMR_IsParametersDirty.side_effect = [-2, 1, -2, 0]
        session = routes._MixerSession(lambda: api)
        self.assertEqual([session.is_running() for _ in range(4)], [False, True, False, True])
        api.VBVMR_Login.assert_called_once_with()
        api.VBVMR_Logout.assert_not_called()
        api.VBVMR_RunVoicemeeter.assert_not_called()
        api.VBVMR_SetParameterFloat.assert_not_called()
        session.close()

    def test_unexpected_dirty_error_never_launches_mixer_or_changes_routes(self):
        for dirty in (-1, 2, 99):
            with self.subTest(dirty=dirty):
                api = FakeApi()
                api.VBVMR_IsParametersDirty.return_value = dirty
                session = routes._MixerSession(lambda: api)
                self.assertFalse(session.is_running())
                with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                    session.configure_routes()
                self.assertEqual(str(error.exception), routes._ERROR)
                api.VBVMR_RunVoicemeeter.assert_not_called()
                api.VBVMR_SetParameterFloat.assert_not_called()
                session.close()

    def test_start_timeout_keeps_owned_login_for_later_readiness_without_another_launch(self):
        api = FakeApi()
        api.VBVMR_Login.return_value = 1
        api.VBVMR_IsParametersDirty.return_value = -2
        clock = FakeClock()
        session = routes._MixerSession(lambda: api)
        with patch.object(routes, 'time', clock):
            with self.assertRaisesRegex(routes.VoicemeeterRoutingError, 'автоматически'):
                session.configure_routes()
        self.assertLessEqual(clock.now, routes._START_TIMEOUT)
        api.VBVMR_Logout.assert_not_called()
        api.VBVMR_SetParameterFloat.assert_not_called()
        api.VBVMR_IsParametersDirty.return_value = 1
        self.assertTrue(session.is_running())
        session.configure_routes()
        self.assertEqual(api.values, routes._ROUTES)
        api.VBVMR_RunVoicemeeter.assert_called_once_with(2)
        api.VBVMR_Login.assert_called_once_with()
        session.close()

    def test_wrong_product_after_cold_launch_never_changes_banana_controls(self):
        api = FakeApi()
        api.VBVMR_IsParametersDirty.side_effect = [-2, 1]
        def wrong_product(out):
            ctypes.cast(out, ctypes.POINTER(ctypes.c_int32))[0] = 1
            return 0
        api.VBVMR_GetVoicemeeterType.side_effect = wrong_product
        session = routes._MixerSession(lambda: api)
        with self.assertRaises(routes.VoicemeeterRoutingError) as error:
            session.configure_routes()
        self.assertEqual(str(error.exception), routes._WRONG_PRODUCT_ERROR)
        api.VBVMR_RunVoicemeeter.assert_called_once_with(2)
        api.VBVMR_SetParameterFloat.assert_not_called()
        session.close()

    def test_close_failure_remains_visible_without_second_logout_or_reopen(self):
        for failure in (-1, OSError('PRIVATE LOGOUT DETAIL')):
            with self.subTest(failure=type(failure).__name__):
                api = FakeApi()
                session = routes._MixerSession(lambda: api)
                self.assertTrue(session.is_running())
                if isinstance(failure, Exception):
                    api.VBVMR_Logout.side_effect = failure
                else:
                    api.VBVMR_Logout.return_value = failure
                for _ in range(2):
                    with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                        session.close()
                    self.assertNotIn('PRIVATE', str(error.exception))
                self.assertFalse(session.is_running())
                with self.assertRaises(routes.VoicemeeterRoutingError):
                    session.configure_routes()
                api.VBVMR_Login.assert_called_once_with()
                api.VBVMR_Logout.assert_called_once_with()
                api.VBVMR_SetParameterFloat.assert_not_called()

    def test_health_waits_for_complete_route_transaction(self):
        api = FakeApi()
        session = routes._MixerSession(lambda: api)
        entered = threading.Event()
        release = threading.Event()
        observer_started = threading.Event()
        observer_done = threading.Event()
        errors = []
        outcomes = []
        original_get = api.get
        def hold_first_snapshot(name, out):
            if not entered.is_set():
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('test did not release snapshot')
            return original_get(name, out)
        api.VBVMR_GetParameterFloat.side_effect = hold_first_snapshot
        def configure():
            try:
                session.configure_routes()
            except BaseException as error:
                errors.append(error)
        def observe():
            observer_started.set()
            try:
                outcomes.append(session.is_running())
            except BaseException as error:
                errors.append(error)
            finally:
                observer_done.set()
        writer = threading.Thread(target=configure, daemon=True)
        observer = threading.Thread(target=observe, daemon=True)
        writer.start()
        try:
            self.assertTrue(entered.wait(1))
            dirty_before = api.VBVMR_IsParametersDirty.call_count
            observer.start()
            self.assertTrue(observer_started.wait(1))
            self.assertFalse(observer_done.wait(.05))
            self.assertEqual(api.VBVMR_IsParametersDirty.call_count, dirty_before)
            api.VBVMR_Logout.assert_not_called()
        finally:
            release.set()
            writer.join(2)
            if observer.ident is not None:
                observer.join(2)
        self.assertFalse(writer.is_alive() or observer.is_alive())
        self.assertEqual(errors, [])
        # A different thread cannot refresh the SDK's owner-thread cache after
        # the transaction, even though the calls would no longer overlap.
        self.assertEqual(outcomes, [False])
        self.assertEqual(api.values, routes._ROUTES)
        session.close()

    def test_non_owner_thread_never_refreshes_or_mutates_the_owned_sdk(self):
        api = FakeApi()
        session = routes._MixerSession(lambda: api)
        self.assertTrue(session.is_running())
        dirty_before = api.VBVMR_IsParametersDirty.call_count
        type_before = api.VBVMR_GetVoicemeeterType.call_count
        outcomes = []
        def other_thread():
            outcomes.append(session.is_running())
            try:
                session.configure_routes()
            except routes.VoicemeeterRoutingError as error:
                outcomes.append(str(error))
        thread = threading.Thread(target=other_thread, daemon=True)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [False, routes._ERROR])
        self.assertEqual(api.VBVMR_IsParametersDirty.call_count, dirty_before)
        self.assertEqual(api.VBVMR_GetVoicemeeterType.call_count, type_before)
        api.VBVMR_Login.assert_called_once_with()
        api.VBVMR_RunVoicemeeter.assert_not_called()
        api.VBVMR_SetParameterFloat.assert_not_called()
        self.assertTrue(session.is_running())  # The original owner stays usable.
        session.close()

    def test_cold_start_diagnostics_are_bounded_to_scalar_stage_results(self):
        api = FakeApi()
        api.VBVMR_Login.return_value = 1
        api.VBVMR_IsParametersDirty.side_effect = [-2, -2, -2, 1, 1, 1]
        session = routes._MixerSession(lambda: api)
        clock = FakeClock()
        with self.assertLogs('remontevoice.voicemeeter_routes', level='INFO') as logs, patch.object(routes, 'time', clock):
            session.configure_routes()
            session.close()
        text = '\n'.join(logs.output)
        self.assertEqual(sum('stage=launch ' in row for row in logs.output), 1)
        self.assertEqual(sum('stage=ready ' in row for row in logs.output), 1)
        self.assertIn('product=2', text)
        self.assertIn('elapsed_ms=', text)
        self.assertLessEqual(len(logs.output), 4)

    def test_cold_start_timeout_reports_final_no_server_code_without_poll_log_spam(self):
        api = FakeApi()
        api.VBVMR_IsParametersDirty.return_value = -2
        session = routes._MixerSession(lambda: api)
        clock = FakeClock()
        with self.assertLogs('remontevoice.voicemeeter_routes', level='INFO') as logs, patch.object(routes, 'time', clock):
            with self.assertRaises(routes.VoicemeeterRoutingError):
                session.configure_routes()
            session.close()
        text = '\n'.join(logs.output)
        self.assertIn('stage=readiness_timeout ', text)
        self.assertIn('dirty=-2', text)
        self.assertLessEqual(len(logs.output), 4)
        self.assertLessEqual(clock.now, routes._START_TIMEOUT)

    def test_native_launch_exception_does_not_expose_exception_detail_in_error_or_log(self):
        api = FakeApi()
        api.VBVMR_IsParametersDirty.return_value = -2
        api.VBVMR_RunVoicemeeter.side_effect = OSError('PRIVATE PATH AND DEVICE DATA')
        session = routes._MixerSession(lambda: api)
        with self.assertLogs('remontevoice.voicemeeter_routes', level='INFO') as logs:
            with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                session.configure_routes()
            session.close()
        self.assertEqual(str(error.exception), routes._ERROR)
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        self.assertIn('stage=launch_error ', '\n'.join(logs.output))
        self.assertIn('error_type=OSError', '\n'.join(logs.output))

    def test_diagnostic_logger_failure_cannot_change_native_result_or_ownership(self):
        api = FakeApi()
        api.VBVMR_IsParametersDirty.side_effect = [-2, 1, 1, 1]
        session = routes._MixerSession(lambda: api)
        with patch.object(routes.LOGGER, 'info', side_effect=OSError('PRIVATE LOGGER DETAIL')):
            session.configure_routes()
            session.close()
        self.assertEqual(api.values, routes._ROUTES)
        api.VBVMR_Login.assert_called_once_with()
        api.VBVMR_RunVoicemeeter.assert_called_once_with(2)
        api.VBVMR_Logout.assert_called_once_with()

    def test_unexpected_readiness_error_fails_without_waiting_or_route_writes(self):
        api = FakeApi()
        api.VBVMR_IsParametersDirty.side_effect = [-2, -1]
        session = routes._MixerSession(lambda: api)
        clock = FakeClock()
        with self.assertLogs('remontevoice.voicemeeter_routes', level='INFO') as logs, patch.object(routes, 'time', clock):
            with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                session.configure_routes()
            session.close()
        self.assertEqual(str(error.exception), routes._ERROR)
        self.assertEqual(clock.sleeps, [])
        api.VBVMR_SetParameterFloat.assert_not_called()
        self.assertIn('stage=readiness_error ', '\n'.join(logs.output))

    def test_final_close_waits_for_current_health_observation(self):
        api = FakeApi()
        entered = threading.Event()
        release = threading.Event()
        closing_started = threading.Event()
        closing_done = threading.Event()
        errors = []
        def dirty():
            entered.set()
            if not release.wait(2):
                raise RuntimeError('test did not release observation')
            return 1
        api.VBVMR_IsParametersDirty.side_effect = dirty
        session = routes._MixerSession(lambda: api)
        def close():
            closing_started.set()
            try:
                session.close()
            except BaseException as error:
                errors.append(error)
            finally:
                closing_done.set()
        observer = threading.Thread(target=session.is_running, daemon=True)
        closer = threading.Thread(target=close, daemon=True)
        observer.start()
        try:
            self.assertTrue(entered.wait(1))
            closer.start()
            self.assertTrue(closing_started.wait(1))
            self.assertFalse(closing_done.wait(.05))
            api.VBVMR_Logout.assert_not_called()
        finally:
            release.set()
            observer.join(2)
            if closer.ident is not None:
                closer.join(2)
        self.assertFalse(observer.is_alive() or closer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(closing_done.is_set())
        session.close()
        api.VBVMR_Logout.assert_called_once_with()
