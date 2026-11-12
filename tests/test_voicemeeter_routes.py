import ctypes
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from remontevoice import voicemeeter_routes as routes


class FakeApi:
    def __init__(self):
        self.values = {name: (index + 1) / 4 for index, name in enumerate(routes._ROUTES)}
        self.original = dict(self.values)
        self.events = []
        self.VBVMR_Login = MagicMock(return_value=0)
        self.VBVMR_Logout = MagicMock(return_value=0)
        self.VBVMR_IsParametersDirty = MagicMock(return_value=1)
        self.VBVMR_GetVoicemeeterType = MagicMock(side_effect=self.product_type)
        self.VBVMR_RunVoicemeeter = MagicMock(return_value=0)
        self.VBVMR_GetParameterFloat = MagicMock(side_effect=self.get)
        self.VBVMR_SetParameterFloat = MagicMock(side_effect=self.set)

    def product_type(self, out):
        ctypes.cast(out, ctypes.POINTER(ctypes.c_int32))[0] = 2
        return 0

    def get(self, name, out):
        key = name.decode("ascii")
        self.events.append(("get", key))
        ctypes.cast(out, ctypes.POINTER(ctypes.c_float))[0] = self.values[key]
        return 0

    def set(self, name, value):
        key = name.decode("ascii")
        self.events.append(("set", key, value.value))
        self.values[key] = value.value
        return 0


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class VoicemeeterRoutesTests(unittest.TestCase):
    @contextmanager
    def api_context(self, api=None, *, side_effect=None):
        # Each context models one application lifetime; helper assertions after
        # it include final teardown, never a logout per configure/health call.
        session = routes._MixerSession()
        with patch.object(routes, '_session', session), patch.object(
            routes, '_load_api', return_value=api, side_effect=side_effect,
        ):
            try:
                yield
            finally:
                if api is not None:
                    api.VBVMR_Logout.assert_not_called()
                session.close()

    def configure(self):
        return routes.configure_standard_routes(
            "Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)",
            "Voicemeeter AUX Input (VB-Audio Voicemeeter VAIO)",
        )

    def test_unrelated_or_ambiguous_names_never_load_dll(self):
        pairs = [
            ("CABLE Output", "CABLE Input"),
            ("Voicemeeter Out B2", "Voicemeeter AUX Input"),
            ("Voicemeeter Out B1", "Voicemeeter Input"),
            ("Voicemeeter Out B1", "Voicemeeter AUX Input 2"),
            ("Voicemeeter Out B1 extra", "Voicemeeter AUX Input"),
            ("Physical Microphone", "Voicemeeter AUX Input"),
            (None, "Voicemeeter AUX Input"),
            ("Voicemeeter Out B1", None),
            (b"Voicemeeter Out B1", "Voicemeeter AUX Input"),
            ("", ""),
        ]
        with patch.object(routes, "_load_api") as load:
            for capture, playback in pairs:
                with self.subTest(capture=capture, playback=playback):
                    self.assertFalse(routes.configure_standard_routes(capture, playback))
            load.assert_not_called()

    def test_configures_only_verified_twenty_virtual_controls_after_snapshot(self):
        api = FakeApi()
        with self.api_context(api):
            self.assertTrue(self.configure())
        self.assertEqual(api.values, routes._ROUTES)
        self.assertEqual(api.events[:20], [("get", name) for name in routes._ROUTES])
        self.assertEqual([event[1] for event in api.events if event[0] == "set"], list(routes._ROUTES))
        self.assertEqual(len(routes._ROUTES), 20)
        self.assertEqual(api.values["Strip[3].B1"], 1)
        self.assertEqual(api.values["Strip[3].B2"], 0)
        self.assertEqual(api.values["Strip[4].B1"], 0)
        self.assertEqual(api.values["Strip[4].B2"], 1)
        api.VBVMR_Login.assert_called_once_with()
        api.VBVMR_Logout.assert_called_once_with()

    def test_accepts_exact_legacy_output_base_and_case_whitespace(self):
        api = FakeApi()
        with self.api_context(api):
            self.assertTrue(routes.configure_standard_routes(
                "  VoiceMeeter Output (VB-Audio VoiceMeeter VAIO)", " VOICEMEETER AUX INPUT "
            ))

    def test_dll_is_loaded_only_from_installed_program_files_and_declares_types(self):
        api = FakeApi()
        with patch.dict(routes.os.environ, {"ProgramFiles(x86)": r"C:\Program Files (x86)"}):
            with patch.object(routes.ctypes, "WinDLL", return_value=api) as load:
                self.assertIs(routes._load_api(), api)
        load.assert_called_once_with(str(Path(r"C:\Program Files (x86)") / "VB/Voicemeeter/VoicemeeterRemote64.dll"))
        for name in ("VBVMR_Login", "VBVMR_Logout", "VBVMR_IsParametersDirty",
                     "VBVMR_GetParameterFloat", "VBVMR_SetParameterFloat", "VBVMR_GetVoicemeeterType"):
            self.assertIs(getattr(api, name).restype, ctypes.c_int32)
            self.assertEqual(ctypes.sizeof(getattr(api, name).restype), 4)
        self.assertEqual(api.VBVMR_Login.argtypes, [])
        self.assertEqual(api.VBVMR_Logout.argtypes, [])
        self.assertEqual(api.VBVMR_IsParametersDirty.argtypes, [])
        self.assertEqual(api.VBVMR_GetParameterFloat.argtypes, [ctypes.c_char_p, ctypes.POINTER(ctypes.c_float)])
        self.assertEqual(api.VBVMR_SetParameterFloat.argtypes, [ctypes.c_char_p, ctypes.c_float])
        self.assertEqual(api.VBVMR_GetVoicemeeterType.argtypes, [ctypes.POINTER(ctypes.c_int32)])

    def test_wrong_or_unreadable_product_never_reads_or_writes_banana_controls(self):
        for product in (0, 1, 3, -1):
            with self.subTest(product=product):
                api = FakeApi()
                def get_type(out):
                    ctypes.cast(out, ctypes.POINTER(ctypes.c_int32))[0] = product
                    return -1 if product == -1 else 0
                api.VBVMR_GetVoicemeeterType.side_effect = get_type
                with self.api_context(api):
                    with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                        self.configure()
                self.assertEqual(str(error.exception),
                                 routes._ERROR if product == -1 else routes._WRONG_PRODUCT_ERROR)
                self.assertEqual(api.events, [])
                api.VBVMR_Logout.assert_called_once()

    def test_dll_error_is_fixed_and_does_not_expose_native_detail(self):
        with self.api_context(side_effect=OSError("PRIVATE NATIVE PATH")):
            with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                self.configure()
        self.assertEqual(str(error.exception), routes._ERROR)

    def test_login_failure_or_exception_never_logs_out_an_unowned_session(self):
        for failure in (-1, OSError("native detail")):
            with self.subTest(failure=type(failure).__name__):
                api = FakeApi()
                if isinstance(failure, Exception):
                    api.VBVMR_Login.side_effect = failure
                else:
                    api.VBVMR_Login.return_value = failure
                with self.api_context(api):
                    with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                        self.configure()
                self.assertEqual(str(error.exception), routes._ERROR)
                api.VBVMR_Logout.assert_not_called()
                api.VBVMR_SetParameterFloat.assert_not_called()

    def test_missing_mixer_is_started_once_and_confirmed_before_route_changes(self):
        api = FakeApi()
        api.VBVMR_Login.return_value = 1
        api.VBVMR_IsParametersDirty.side_effect = [-2, -2, -2, 1, 1, 1]
        clock = FakeClock()
        with self.api_context(api), patch.object(routes, 'time', clock):
            self.assertTrue(self.configure())
        api.VBVMR_RunVoicemeeter.assert_called_once_with(2)
        self.assertEqual(clock.sleeps, [.1, .1])
        self.assertEqual(api.values, routes._ROUTES)
        api.VBVMR_Logout.assert_called_once()

    def test_slow_cold_start_beyond_old_eight_second_budget_is_confirmed(self):
        # Measured on this machine: the SDK accepted the launch and the process
        # started, but the server only answered after 11.2 s. The old 8 s budget
        # failed a launch that was actually in progress.
        api = FakeApi()
        api.VBVMR_Login.return_value = 1
        clock = FakeClock()
        api.VBVMR_IsParametersDirty.side_effect = lambda: -2 if clock.now < 11.2 else 1
        with self.api_context(api), patch.object(routes, 'time', clock):
            self.assertTrue(self.configure())
        api.VBVMR_RunVoicemeeter.assert_called_once_with(2)
        self.assertGreater(clock.now, 8.0)
        self.assertLessEqual(clock.now, routes._START_TIMEOUT)
        self.assertEqual(api.values, routes._ROUTES)

    def test_start_timeout_or_error_never_changes_routes(self):
        for launch_result in (0, -1):
            api = FakeApi()
            api.VBVMR_Login.return_value = 1
            api.VBVMR_RunVoicemeeter.return_value = launch_result
            api.VBVMR_IsParametersDirty.return_value = -2
            clock = FakeClock()
            with self.api_context(api), patch.object(routes, 'time', clock):
                with self.assertRaisesRegex(routes.VoicemeeterRoutingError, 'автоматически'):
                    self.configure()
            self.assertLessEqual(clock.now, routes._START_TIMEOUT)
            api.VBVMR_SetParameterFloat.assert_not_called()
            api.VBVMR_Logout.assert_called_once()

    def test_existing_mixer_never_launches_another(self):
        api = FakeApi()
        with self.api_context(api):
            self.assertTrue(self.configure())
        api.VBVMR_RunVoicemeeter.assert_not_called()

    def test_availability_poll_never_launches_or_mutates_missing_mixer(self):
        api = FakeApi()
        with self.api_context(api):
            self.assertTrue(routes.standard_mixer_running())
            api.VBVMR_IsParametersDirty.return_value = -2
            self.assertFalse(routes.standard_mixer_running())
        api.VBVMR_RunVoicemeeter.assert_not_called()
        api.VBVMR_SetParameterFloat.assert_not_called()

    def test_snapshot_dirty_get_or_nonfinite_error_prevents_every_write(self):
        for failure in ("dirty", "get", "nonfinite"):
            with self.subTest(failure=failure):
                api = FakeApi()
                if failure == "dirty":
                    api.VBVMR_IsParametersDirty.return_value = -1
                elif failure == "get":
                    api.VBVMR_GetParameterFloat.return_value = -1
                    api.VBVMR_GetParameterFloat.side_effect = None
                else:
                    api.values[next(iter(api.values))] = float("nan")
                with self.api_context(api):
                    with self.assertRaises(routes.VoicemeeterRoutingError):
                        self.configure()
                api.VBVMR_SetParameterFloat.assert_not_called()
                api.VBVMR_Logout.assert_called_once_with()

    def test_set_failure_rolls_back_all_twenty_original_values(self):
        api = FakeApi()
        writes = 0

        def set_value(name, value):
            nonlocal writes
            writes += 1
            if writes == 3:
                return -1
            return api.set(name, value)

        api.VBVMR_SetParameterFloat.side_effect = set_value
        with self.api_context(api):
            with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                self.configure()
        self.assertEqual(str(error.exception), routes._ERROR)
        self.assertEqual(api.values, api.original)
        self.assertEqual(writes, 23)
        self.assertEqual([call.args[0].decode() for call in api.VBVMR_SetParameterFloat.call_args_list[-20:]], list(routes._ROUTES))
        api.VBVMR_Logout.assert_called_once_with()

    def test_readback_failure_rolls_back_all_original_values(self):
        api = FakeApi()
        reads = 0

        def get_value(name, out):
            nonlocal reads
            reads += 1
            return -1 if reads == 21 else api.get(name, out)

        api.VBVMR_GetParameterFloat.side_effect = get_value
        with self.api_context(api):
            with self.assertRaises(routes.VoicemeeterRoutingError):
                self.configure()
        self.assertEqual(api.values, api.original)
        self.assertEqual(api.VBVMR_SetParameterFloat.call_count, 40)
        api.VBVMR_Logout.assert_called_once_with()

    def test_stale_readback_stops_within_three_seconds_and_rolls_back(self):
        api = FakeApi()
        clock = FakeClock()
        writes = 0

        def set_value(name, value):
            nonlocal writes
            writes += 1
            return 0 if writes <= 20 else api.set(name, value)

        api.VBVMR_SetParameterFloat.side_effect = set_value
        with self.api_context(api):
            with patch.object(routes.time, "monotonic", clock.monotonic), patch.object(routes.time, "sleep", clock.sleep):
                with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                    self.configure()
        self.assertEqual(str(error.exception), routes._ERROR)
        self.assertLessEqual(clock.now, 3)
        self.assertEqual(api.values, api.original)
        self.assertEqual(writes, 40)
        api.VBVMR_Logout.assert_called_once_with()

    def test_rollback_continues_after_one_restore_write_fails(self):
        api = FakeApi()
        writes = 0

        def set_value(name, value):
            nonlocal writes
            writes += 1
            return -1 if writes in (2, 3) else api.set(name, value)

        api.VBVMR_SetParameterFloat.side_effect = set_value
        with self.api_context(api), patch.object(routes, "_READBACK_TIMEOUT", 0):
            with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                self.configure()
        self.assertIn(routes._ROLLBACK_ERROR, str(error.exception))
        self.assertEqual(writes, 22)
        api.VBVMR_Logout.assert_called_once_with()

    def test_final_logout_failure_does_not_rewrite_completed_routes_or_retry_logout(self):
        api = FakeApi()
        api.VBVMR_Logout.side_effect = [-1, 0]
        session = routes._MixerSession(lambda: api)
        with patch.object(routes, '_session', session):
            self.assertTrue(self.configure())
            api.VBVMR_Logout.assert_not_called()
            with self.assertRaises(routes.VoicemeeterRoutingError) as error:
                routes.close_standard_mixer_session()
            self.assertNotIn('native', str(error.exception))
            # Logout failure cannot justify writing a stale snapshot after the
            # Host has already restored the physical Windows audio routes.
            self.assertEqual(api.values, routes._ROUTES)
            self.assertEqual(api.VBVMR_SetParameterFloat.call_count, 20)
            try:
                routes.close_standard_mixer_session()
            except routes.VoicemeeterRoutingError:
                pass
            api.VBVMR_Logout.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
