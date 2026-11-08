import types
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from remotevoice.host_app import HostWindow, matching_system_output
from remotevoice.microphone_guard import MicrophoneGuardError
from remotevoice.audio_backend import AudioBackendError
from remotevoice.output_routing import OutputRoutingError


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Combo(dict):
    def configure(self, **kwargs):
        self.update(kwargs)


def headless_window():
    """Use real UI handlers with inert widgets and a mocked route; never open Tk."""
    window = types.SimpleNamespace(
        output_route=MagicMock(),
        input_route=None,
        _input_route_error="",
        _input_route_active=False,
        microphone_guard=None,
        _microphone_guard_active=False,
        _microphone_guard_error="",
        _microphone_activation_error="",
        _session_restore_failure="",
        _session_activation_error="",
        _server_generation=0,
        _audio_backend=None,
        _driver_runtime_factory=None,
        _driver_runtime=None,
        _driver_startup_worker=None,
        _driver_cleanup_worker=None,
        _driver_generation=0,
        _driver_cancelled=False,
        _driver_ready=False,
        _driver_result=None,
        _driver_cleanup_error='',
        _close_deadline=None,
        _host_lease=types.SimpleNamespace(held=True, release=MagicMock()),
        _safety_guard=MagicMock(armed=True),
        _output_route_active=False,
        _output_route_error="",
        _route_activation_failed=False,
        _stopping=False,
        _screen_events_lock=threading.Lock(),
        _screen_events_closed=False,
        _pending_screen_event=None,
        _pending_audio_event=None,
        server_running=True,
        server=MagicMock(cleanup_failed=False),
        server_thread=None,
        status_var=Value(),
        screen_allowed_var=Value(False),
        screen_status_var=Value(),
        capture_var=Value(),
        playback_var=Value(),
        system_output_var=Value(),
        fallback_output_var=Value(),
        local_microphone_var=Value(),
        mute_local_microphone_var=Value(False),
        endpoint_hint_var=Value(),
        ip_var=Value("192.168.1.10"),
        capture_choices={},
        capture_names={},
        playback_choices={},
        playback_names={},
        system_output_choices={},
        fallback_output_choices={},
        local_microphone_choices={},
        virtual_microphones=[],
        config_data={},
        pairing=MagicMock(),
        data_dir=Path("unused"),
        _refresh_pairing_ui=MagicMock(),
        _save_config=MagicMock(),
        _post_status=MagicMock(),
        _run_server=MagicMock(),
        after=MagicMock(),
        destroy=MagicMock(),
    )
    for name in (
        "ip_combo", "capture_combo", "playback_combo", "system_output_combo",
        "fallback_output_combo", "local_microphone_combo", "mute_local_microphone_check",
        "start_button", "stop_button",
    ):
        setattr(window, name, Combo())
    for name in (
        "_set_status", "_set_status_text", "_activate_output_route",
        "_restore_output_route", "_set_running_controls", "_stop_host",
        "_finish_stop_when_ready", "_close_when_stopped", "_on_close",
        "_suggest_system_output", "_load_audio_devices", "_start_host",
        "_activate_microphone_guard", "_restore_microphone_guard", "_restore_session_routes",
        "_route_errors", "_update_microphone_controls",
        "_update_screen_permission", "_post_screen_status", "_set_screen_status",
        "_queue_screen_status", "_drain_screen_status",
        "_recover_waiting_routes", "_return_to_laptop", "_activate_input_route", "_restore_input_route",
        "_activate_session_routes", "_request_session_start", "_post_for_generation", "_post_audio_status",
        "_destroy_host",
        "_check_safety_guard",
        "_driver_workers_busy", "_driver_cleanup_confirmed", "_driver_audio_factory",
        "_prepare_driver_runtime", "_finish_driver_startup", "_begin_driver_cleanup",
    ):
        setattr(window, name, types.MethodType(getattr(HostWindow, name), window))
    return window


class SystemOutputMatchingTests(unittest.TestCase):
    def test_known_virtual_pairs(self):
        speakers = [
            {"id": "vaio", "name": "Voicemeeter Input (VB-Audio Voicemeeter VAIO)"},
            {"id": "aux", "name": "Voicemeeter AUX Input (VB-Audio Voicemeeter VAIO)"},
            {"id": "cable", "name": "CABLE Input (VB-Audio Virtual Cable)"},
        ]
        for capture, expected in (
            ("Voicemeeter Out B1 (VB-Audio Voicemeeter VAIO)", "vaio"),
            ("Voicemeeter Out B2 (VB-Audio Voicemeeter VAIO)", "aux"),
            ("CABLE Output (VB-Audio Virtual Cable)", "cable"),
        ):
            with self.subTest(capture=capture):
                self.assertEqual(matching_system_output(capture, speakers), expected)

    def test_unknown_or_ambiguous_pair_is_not_guessed(self):
        speakers = [
            {"id": "first", "name": "Voicemeeter Input (First driver)"},
            {"id": "second", "name": "Voicemeeter Input (Second driver)"},
        ]
        self.assertIsNone(matching_system_output("Voicemeeter Out B1", speakers))
        self.assertIsNone(matching_system_output("Voicemeeter Out B3", speakers))
        self.assertIsNone(matching_system_output("Microphone", speakers))


class HeadlessHostTests(unittest.TestCase):
    def setUp(self):
        safety_patch = patch("remotevoice.host_app.SafetyGuard")
        self.safety_class = safety_patch.start()
        self.safety_class.start.return_value = MagicMock(armed=True)
        self.addCleanup(safety_patch.stop)
        mixer_patch = patch("remotevoice.host_app.prepare_audio_backend", return_value=MagicMock(is_available=MagicMock(return_value=True)))
        self.mixer_configure = mixer_patch.start()
        self.addCleanup(mixer_patch.stop)
        input_patch = patch("remotevoice.host_app.InputRoute")
        self.input_class = input_patch.start()
        self.addCleanup(input_patch.stop)
        guard_patch = patch("remotevoice.host_app.MicrophoneMuteGuard")
        self.guard_class = guard_patch.start()
        self.addCleanup(guard_patch.stop)
        microphones_patch = patch("remotevoice.host_app.sc.all_microphones", return_value=[])
        self.microphones = microphones_patch.start()
        self.addCleanup(microphones_patch.stop)


class HostOutputRoutingTests(HeadlessHostTests):
    def test_backend_preparation_failure_prevents_server_and_guard_start(self):
        window = self.configured_window()
        self.mixer_configure.side_effect = AudioBackendError('Known component unavailable')
        with patch('remotevoice.host_app.RemoteVoiceServer') as server, patch(
            'remotevoice.host_app.messagebox.showerror'
        ) as error:
            window._start_host()
        server.assert_not_called()
        self.safety_class.start.assert_not_called()
        self.assertFalse(window.server_running)
        self.assertIn('Known component unavailable', error.call_args.args[1])

    def test_late_audio_ready_after_restore_cannot_show_phone_as_connected(self):
        window = headless_window()
        window._set_status("iphone_connected_muted")
        window._set_status("server_ready")
        waiting = window.status_var.get()
        window._set_status("audio_active")
        self.assertEqual(window.status_var.get(), waiting)
        self.assertIn("Ожидает телефон", waiting)
        window.output_route.activate.assert_called_once()
        window.output_route.restore.assert_called_once()

    def test_audio_ready_cannot_replace_stopped_state(self):
        window = headless_window()
        window.server_running = False
        window.status_var.set("Подключение остановлено")
        window._set_status("audio_active")
        self.assertEqual(window.status_var.get(), "Подключение остановлено")
        window.output_route.activate.assert_not_called()

    def test_connect_switches_once_and_microphone_mute_preserves_phone_output(self):
        window = headless_window()
        for status in (
            "iphone_connected_muted", "audio_active", "iphone_microphone_on",
            "iphone_connected_muted",
        ):
            window._set_status(status)
        window.output_route.activate.assert_called_once_with()
        window.output_route.restore.assert_not_called()
        self.assertTrue(window._output_route_active)
        window._set_status("server_ready")
        window.output_route.restore.assert_called_once_with()
        self.assertFalse(window._output_route_active)

    def test_audio_thread_status_alone_never_switches_output(self):
        window = headless_window()
        window._set_status("audio_active")
        window.output_route.activate.assert_not_called()
        window._set_status("server_ready")
        window._set_status("audio_active")
        window.output_route.activate.assert_not_called()

    def test_all_session_failures_restore_output_and_block_late_activation(self):
        for status in (
            "audio_error: device failed", "client_error: session failed",
            "server_error: listener failed", "server_stopped",
        ):
            with self.subTest(status=status):
                window = headless_window()
                window._set_status("iphone_connected_muted")
                window._set_status(status)
                window._set_status("audio_active")
                window._set_status("iphone_microphone_on")
                window.output_route.restore.assert_called_once_with()
                window.output_route.activate.assert_called_once_with()

    def test_activation_error_rolls_back_stops_and_does_not_reactivate(self):
        window = headless_window()
        window.output_route.activate.side_effect = OutputRoutingError("unavailable")
        with self.assertLogs("remotevoice.ui", level="WARNING"):
            window._set_status("iphone_connected_muted")
        for status in ("iphone_microphone_on", "iphone_connected_muted"):
            window._set_status(status)
        window.output_route.activate.assert_called_once_with()
        window.output_route.restore.assert_called_once_with()
        window.server.stop.assert_called_once_with()
        self.assertFalse(window.server_running)
        self.assertTrue(window._stopping)
        self.assertIn("Не удалось направить звук на телефон", window.status_var.get())
        window._set_status("server_ready")
        window.output_route.activate.side_effect = None
        window._set_status("iphone_connected_muted")
        self.assertEqual(window.output_route.activate.call_count, 1)
        self.assertEqual(window._output_route_error, "")

    def test_restore_error_remains_visible_instead_of_claiming_success(self):
        window = headless_window()
        window.output_route.restore.side_effect = OutputRoutingError("missing speakers")
        with self.assertLogs("remotevoice.ui", level="WARNING"):
            window._set_status("server_ready")
        self.assertIn("Не удалось вернуть звук", window.status_var.get())
        self.assertIn("Выберите их вручную", window.status_var.get())

    def test_partial_restore_failure_stops_host_and_prevents_automatic_reconnect(self):
        window = headless_window()
        window._set_status("iphone_connected_muted")
        window.output_route.restore.side_effect = OutputRoutingError("partial failure")
        with self.assertLogs("remotevoice.ui", level="WARNING"):
            window._set_status("server_ready")
        self.assertFalse(window._output_route_active)
        window._set_status("iphone_connected_muted")
        window.output_route.activate.assert_called_once_with()
        window.server.stop.assert_called_once_with()
        self.assertTrue(window._stopping)
        self.assertFalse(window._output_route_active)

    def test_stop_restores_output_without_manual_confirmation(self):
        window = headless_window()
        with patch("remotevoice.host_app.messagebox.askyesno") as ask:
            window._stop_host()
        ask.assert_not_called()
        window.output_route.restore.assert_called_once_with()
        window.server.stop.assert_called_once_with()
        window._set_status("iphone_connected_muted")
        window.output_route.activate.assert_not_called()
        self.assertFalse(window.server_running)

    def test_close_reports_restore_failure_and_closes(self):
        window = headless_window()
        window.output_route.restore.side_effect = OutputRoutingError("failed")
        with self.assertLogs("remotevoice.ui", level="WARNING"), patch(
            "remotevoice.host_app.messagebox.showerror"
        ) as show_error:
            window._on_close()
        show_error.assert_called_once()
        self.assertIn("не восстановлены", show_error.call_args.args[0])
        window.destroy.assert_called_once_with()

    def test_load_devices_filters_fallback_and_uses_saved_or_physical_default(self):
        window = headless_window()
        window.config_data = {"capture_device_id": "b1", "playback_device_id": "aux"}
        virtual = [
            {"id": "vaio", "name": "Voicemeeter Input"},
            {"id": "aux", "name": "Voicemeeter AUX Input"},
        ]
        speakers = [
            types.SimpleNamespace(id="vaio", name="Voicemeeter Input"),
            types.SimpleNamespace(id="laptop", name="Speakers (Realtek Audio)"),
            types.SimpleNamespace(id="headset", name="Headphones"),
        ]
        with patch("remotevoice.host_app.list_audio_devices", return_value=(
            virtual, [{"id": "b1", "name": "Voicemeeter Out B1", "loopback": "False"}]
        )), patch("remotevoice.host_app.sc.all_speakers", return_value=speakers), patch(
            "remotevoice.host_app.sc.default_speaker", return_value=speakers[1]
        ):
            window._load_audio_devices()
        self.assertEqual(window.system_output_choices[window.system_output_var.get()], "vaio")
        self.assertEqual(window.fallback_output_choices[window.fallback_output_var.get()], "laptop")
        self.assertNotIn("vaio", window.fallback_output_choices.values())

    def test_multiple_physical_outputs_without_physical_default_require_selection(self):
        window = headless_window()
        speakers = [
            types.SimpleNamespace(id="vaio", name="Voicemeeter Input"),
            types.SimpleNamespace(id="laptop", name="Speakers"),
            types.SimpleNamespace(id="headset", name="Headphones"),
        ]
        with patch("remotevoice.host_app.list_audio_devices", return_value=([], [])), patch(
            "remotevoice.host_app.sc.all_speakers", return_value=speakers
        ), patch("remotevoice.host_app.sc.default_speaker", return_value=speakers[0]):
            window._load_audio_devices()
        self.assertEqual(window.fallback_output_var.get(), "")

    def test_missing_windows_default_does_not_hide_available_outputs(self):
        window = headless_window()
        with patch("remotevoice.host_app.list_audio_devices", return_value=([], [])), patch(
            "remotevoice.host_app.sc.all_speakers",
            return_value=[types.SimpleNamespace(id="laptop", name="Speakers")],
        ), patch("remotevoice.host_app.sc.default_speaker", side_effect=RuntimeError("no default")):
            window._load_audio_devices()
        self.assertEqual(window.fallback_output_choices[window.fallback_output_var.get()], "laptop")

    def configured_window(self):
        window = headless_window()
        window.server_running = False
        window.capture_choices = {"capture": "b1"}
        window.capture_names = {"b1": "Voicemeeter Out B1"}
        window.playback_choices = {"playback": "aux"}
        window.playback_names = {"aux": "Voicemeeter AUX Input"}
        window.system_output_choices = {"system": "vaio"}
        window.fallback_output_choices = {"fallback": "laptop"}
        window.local_microphone_choices = {"laptop mic": "physical-mic"}
        window.local_microphone_var.set("laptop mic")
        window.virtual_microphones = [{"id": "b2", "name": "Voicemeeter Out B2", "loopback": "False"}]
        for name, value in (
            ("capture_var", "capture"), ("playback_var", "playback"),
            ("system_output_var", "system"), ("fallback_output_var", "fallback"),
        ):
            getattr(window, name).set(value)
        return window

    def test_start_restores_waiting_output_and_does_not_activate_before_connection(self):
        window = self.configured_window()
        with patch("remotevoice.host_app.OutputRoute") as route, patch(
            "remotevoice.host_app.RemoteVoiceServer"
        ), patch("remotevoice.host_app.threading.Thread"):
            window._start_host()
        route.assert_called_once_with("vaio", "laptop")
        route.return_value.restore.assert_called_once_with()
        route.return_value.activate.assert_not_called()
        self.assertEqual(window.config_data["system_output_device_id"], "vaio")
        self.assertEqual(window.config_data["fallback_output_device_id"], "laptop")
        self.assertTrue(window.server_running)

    def test_missing_fallback_or_initial_restore_failure_prevents_start(self):
        for missing in (True, False):
            with self.subTest(missing_fallback=missing):
                window = self.configured_window()
                if missing:
                    window.fallback_output_var.set("")
                with patch("remotevoice.host_app.OutputRoute") as route, patch(
                    "remotevoice.host_app.RemoteVoiceServer"
                ) as server, patch("remotevoice.host_app.messagebox.showerror") as error:
                    route.return_value.restore.side_effect = OutputRoutingError("failed")
                    window._start_host()
                server.assert_not_called()
                error.assert_called_once()
                self.assertFalse(window.server_running)

    def test_config_persists_both_output_endpoint_ids(self):
        window = self.configured_window()
        window.config_path = window.data_dir / "config.json"
        with patch.object(Path, "mkdir"), patch.object(Path, "write_text") as write, patch.object(
            Path, "replace"
        ), patch.object(Path, "read_text") as read:
            HostWindow._save_config(window)
            read.return_value = write.call_args.args[0]
            restored = HostWindow._read_config(window)
        self.assertEqual(restored["system_output_device_id"], "vaio")
        self.assertEqual(restored["fallback_output_device_id"], "laptop")


class HostMicrophoneGuardTests(HeadlessHostTests):
    def configured_window(self):
        window = HostOutputRoutingTests.configured_window(self)
        window.local_microphone_choices = {"laptop mic": "physical-mic"}
        window.local_microphone_var.set("laptop mic")
        window.mute_local_microphone_var.set(True)
        return window

    def test_connection_mutes_local_microphone_before_routing_output(self):
        window = headless_window()
        window.microphone_guard = MagicMock()
        order = []
        window.microphone_guard.activate.side_effect = lambda: order.append("mute")
        window.output_route.activate.side_effect = lambda: order.append("output")
        for status in (
            "iphone_connected_muted", "iphone_microphone_on", "iphone_connected_muted",
        ):
            window._set_status(status)
        self.assertEqual(order, ["mute", "output"])
        self.assertIn("микрофон ноутбука выключен", window.status_var.get())
        window.microphone_guard.restore.assert_not_called()
        self.assertTrue(window._microphone_guard_active)
        window._set_status("server_ready")
        window.microphone_guard.restore.assert_called_once_with()
        self.assertFalse(window._microphone_guard_active)

    def test_audio_thread_status_alone_never_mutes_local_microphone(self):
        window = headless_window()
        window.microphone_guard = MagicMock()
        window._set_status("audio_active")
        window.microphone_guard.activate.assert_not_called()

    def test_all_session_failures_restore_both_routes(self):
        for status in (
            "audio_error: failure", "client_error: failure", "server_error: failure",
            "server_stopped", "server_ready",
        ):
            with self.subTest(status=status):
                window = headless_window()
                window.microphone_guard = MagicMock()
                window._set_status("iphone_connected_muted")
                window._set_status(status)
                window.output_route.restore.assert_called_once_with()
                window.microphone_guard.restore.assert_called_once_with()

    def test_output_restore_failure_does_not_skip_microphone_restore(self):
        window = headless_window()
        window.microphone_guard = MagicMock()
        window.output_route.restore.side_effect = OutputRoutingError("output missing")
        with self.assertLogs("remotevoice.ui", level="WARNING"):
            window._set_status("server_ready")
        window.microphone_guard.restore.assert_called_once_with()
        self.assertIn("Не удалось вернуть звук", window.status_var.get())

    def test_microphone_restore_failure_does_not_skip_output_restore(self):
        window = headless_window()
        window.microphone_guard = MagicMock()
        window.microphone_guard.restore.side_effect = MicrophoneGuardError("mic missing")
        with self.assertLogs("remotevoice.ui", level="WARNING"):
            window._set_status("server_ready")
        window.output_route.restore.assert_called_once_with()
        self.assertIn("Не удалось вернуть прежнее состояние микрофона", window.status_var.get())
        self.assertFalse(window._microphone_guard_active)

    def test_failed_microphone_restore_stops_new_connections_and_preserves_original_error(self):
        window = headless_window()
        window.microphone_guard = MagicMock()
        window._set_status("iphone_connected_muted")
        window.microphone_guard.restore.side_effect = [MicrophoneGuardError("original failure"), None]
        with self.assertLogs("remotevoice.ui", level="WARNING"):
            window._set_status("server_ready")
        window.server.stop.assert_called_once_with()
        self.assertTrue(window._stopping)
        self.assertTrue(window._route_activation_failed)
        self.assertEqual(window.start_button["state"], "disabled")
        for status in ("iphone_connected_muted", "iphone_microphone_on", "audio_active"):
            window._set_status(status)
        window.microphone_guard.activate.assert_called_once_with()
        window.output_route.activate.assert_called_once_with()
        window._set_status("server_stopped")
        self.assertEqual(window._microphone_guard_error, "")
        self.assertIn("original failure", window.status_var.get())
        self.assertFalse(window.server_running)
        self.assertEqual(window.start_button["state"], "normal")

    def test_successful_manual_restart_clears_previous_restore_failure(self):
        window = self.configured_window()
        window._session_restore_failure = "previous restoration failed"
        window._stopping = True
        with patch("remotevoice.host_app.OutputRoute"), patch(
            "remotevoice.host_app.RemoteVoiceServer"
        ), patch("remotevoice.host_app.threading.Thread"):
            window._start_host()
        self.assertEqual(window._session_restore_failure, "")
        self.assertFalse(window._stopping)
        self.assertTrue(window.server_running)

    def test_mute_failure_stops_host_restores_both_and_keeps_error_visible(self):
        window = headless_window()
        window.microphone_guard = MagicMock()
        window.microphone_guard.activate.side_effect = MicrophoneGuardError("mute denied")
        with self.assertLogs("remotevoice.ui", level="WARNING"):
            window._set_status("iphone_connected_muted")
        window.server.stop.assert_called_once_with()
        window.output_route.activate.assert_not_called()
        window.output_route.restore.assert_called_once_with()
        window.microphone_guard.restore.assert_called_once_with()
        self.assertFalse(window._microphone_guard_active)
        self.assertIn("Не удалось выключить микрофон ноутбука", window.status_var.get())
        self.assertNotIn("микрофон ноутбука выключен", window.status_var.get())
        window._set_status("server_stopped")
        self.assertIn("Не удалось выключить микрофон ноутбука", window.status_var.get())
        window._set_status("iphone_microphone_on")
        window.microphone_guard.activate.assert_called_once_with()

    def test_close_displays_both_restore_failures_together(self):
        window = headless_window()
        window.microphone_guard = MagicMock()
        window.microphone_guard.restore.side_effect = MicrophoneGuardError("mic failed")
        window.output_route.restore.side_effect = OutputRoutingError("output failed")
        with self.assertLogs("remotevoice.ui", level="WARNING"), patch(
            "remotevoice.host_app.messagebox.showerror"
        ) as error:
            window._on_close()
        error.assert_called_once()
        self.assertIn("Не удалось вернуть звук", error.call_args.args[1])
        self.assertIn("Не удалось вернуть прежнее состояние микрофона", error.call_args.args[1])
        window.destroy.assert_called_once_with()

    def test_old_activation_error_does_not_claim_close_restore_failed(self):
        window = headless_window()
        window._microphone_activation_error = "Не удалось выключить микрофон ноутбука"
        with patch("remotevoice.host_app.messagebox.showerror") as error:
            window._on_close()
        error.assert_not_called()

    def test_start_restores_old_guard_and_recovers_journal_before_creating_new_guard(self):
        window = self.configured_window()
        old_guard = MagicMock()
        window.microphone_guard = old_guard
        order = []
        old_guard.restore.side_effect = lambda: order.append("old restore")
        self.guard_class.recover.side_effect = lambda _path: order.append("recover")
        self.guard_class.side_effect = lambda *_args, **_kwargs: order.append("new") or MagicMock()
        with patch("remotevoice.host_app.OutputRoute"), patch(
            "remotevoice.host_app.RemoteVoiceServer"
        ), patch("remotevoice.host_app.threading.Thread"):
            window._start_host()
        self.assertEqual(order, ["old restore", "recover", "new"])
        self.guard_class.assert_called_once_with(
            "physical-mic", journal_path=window.data_dir / "microphone-restore.json"
        )
        self.guard_class.recover.assert_called_once_with(window.data_dir / "microphone-restore.json")
        window.microphone_guard.activate.assert_not_called()
        self.assertTrue(window.server_running)

    def test_journal_recovery_failure_prevents_new_session(self):
        window = self.configured_window()
        self.guard_class.recover.side_effect = MicrophoneGuardError("recovery denied")
        with patch("remotevoice.host_app.RemoteVoiceServer") as server, patch(
            "remotevoice.host_app.messagebox.showerror"
        ) as error:
            window._start_host()
        server.assert_not_called()
        self.guard_class.assert_not_called()
        error.assert_called_once()
        self.assertIn("Не удалось восстановить микрофон ноутбука", error.call_args.args[1])

    def test_old_guard_restore_failure_still_attempts_journal_recovery_and_prevents_start(self):
        window = self.configured_window()
        window.microphone_guard = MagicMock()
        window.microphone_guard.restore.side_effect = MicrophoneGuardError("old restore denied")
        with self.assertLogs("remotevoice.ui", level="WARNING"), patch(
            "remotevoice.host_app.RemoteVoiceServer"
        ) as server, patch("remotevoice.host_app.messagebox.showerror") as error:
            window._start_host()
        server.assert_not_called()
        self.guard_class.recover.assert_called_once_with(window.data_dir / "microphone-restore.json")
        error.assert_called_once()
        window.output_route.restore.assert_called_once_with()

    def test_disabled_guard_still_recovers_old_journal_without_creating_new_guard(self):
        window = self.configured_window()
        window.mute_local_microphone_var.set(False)
        with patch("remotevoice.host_app.OutputRoute"), patch(
            "remotevoice.host_app.RemoteVoiceServer"
        ), patch("remotevoice.host_app.threading.Thread"):
            window._start_host()
        self.guard_class.recover.assert_called_once()
        self.guard_class.assert_not_called()
        self.assertIsNone(window.microphone_guard)
        self.assertTrue(window.server_running)

    def test_enabled_guard_requires_selected_physical_microphone(self):
        window = self.configured_window()
        window.local_microphone_var.set("")
        with patch("remotevoice.host_app.RemoteVoiceServer") as server, patch(
            "remotevoice.host_app.messagebox.showerror"
        ) as error:
            window._start_host()
        server.assert_not_called()
        self.assertIn("Выберите микрофон ноутбука", error.call_args.args[0])

    def test_microphone_list_filters_virtual_and_loopback_and_selects_only_physical(self):
        window = headless_window()
        self.microphones.return_value = [
            types.SimpleNamespace(id="b1", name="Voicemeeter Out B1", isloopback=False),
            types.SimpleNamespace(id="b2", name="Voicemeeter Out B2", isloopback=False),
            types.SimpleNamespace(id="loop", name="Speakers", isloopback=True),
            types.SimpleNamespace(id="mic", name="Microphone Array", isloopback=False),
        ]
        with patch("remotevoice.host_app.list_audio_devices", return_value=([], [])), patch(
            "remotevoice.host_app.sc.all_speakers", return_value=[]
        ), patch("remotevoice.host_app.sc.default_speaker", return_value=None):
            window._load_audio_devices()
        self.microphones.assert_called_once_with(include_loopback=False)
        self.assertEqual(list(window.local_microphone_choices.values()), ["mic"])
        self.assertEqual(window.local_microphone_choices[window.local_microphone_var.get()], "mic")

    def test_ambiguous_physical_microphones_require_selection_unless_saved(self):
        self.microphones.return_value = [
            types.SimpleNamespace(id="laptop", name="Microphone Array", isloopback=False),
            types.SimpleNamespace(id="usb", name="USB Microphone", isloopback=False),
        ]
        for saved, expected in ((None, ""), ("usb", "USB Microphone")):
            with self.subTest(saved=saved):
                window = headless_window()
                if saved is not None:
                    window.config_data["local_microphone_device_id"] = saved
                with patch("remotevoice.host_app.list_audio_devices", return_value=([], [])), patch(
                    "remotevoice.host_app.sc.all_speakers", return_value=[]
                ), patch("remotevoice.host_app.sc.default_speaker", return_value=None):
                    window._load_audio_devices()
                self.assertEqual(window.local_microphone_var.get(), expected)
                if saved is not None:
                    self.assertEqual(window.local_microphone_choices[expected], saved)

    def test_checkbox_disables_selector_without_enabling_it_during_session(self):
        window = self.configured_window()
        window._update_microphone_controls()
        self.assertEqual(window.local_microphone_combo["state"], "readonly")
        window.mute_local_microphone_var.set(False)
        window._update_microphone_controls()
        self.assertEqual(window.local_microphone_combo["state"], "readonly")
        window.mute_local_microphone_var.set(True)
        window.server_running = True
        window._set_running_controls(True)
        self.assertEqual(window.local_microphone_combo["state"], "disabled")
        self.assertEqual(window.mute_local_microphone_check["state"], "disabled")

    def test_config_preserves_microphone_id_and_boolean_preference(self):
        window = self.configured_window()
        window.config_path = window.data_dir / "config.json"
        with patch.object(Path, "mkdir"), patch.object(Path, "write_text") as write, patch.object(
            Path, "replace"
        ), patch.object(Path, "read_text") as read:
            HostWindow._save_config(window)
            read.return_value = write.call_args.args[0]
            restored = HostWindow._read_config(window)
        self.assertEqual(restored["local_microphone_device_id"], "physical-mic")
        self.assertIs(restored["mute_local_microphone"], True)


if __name__ == "__main__":
    unittest.main()
