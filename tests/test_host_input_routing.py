import unittest
from unittest.mock import MagicMock, patch

from remotevoice.host_app import matching_virtual_microphone
from remotevoice.input_routing import InputRoutingError
from remotevoice.audio_backend import AudioBackendError
import test_host_output_routing as fixtures


class VirtualMicrophoneMatchingTests(unittest.TestCase):
    def test_matches_aux_to_b2_excluding_render_loopback(self):
        devices = [{"id": "b2", "name": "Voicemeeter Out B2", "loopback": "False"},
                   {"id": "loop", "name": "Voicemeeter Out B2", "loopback": "True"},
                   {"id": "b1", "name": "Voicemeeter Out B1", "loopback": "False"}]
        self.assertEqual(matching_virtual_microphone("aux", "Voicemeeter AUX Input", devices), "b2")

    def test_missing_or_ambiguous_partner_is_not_guessed(self):
        self.assertIsNone(matching_virtual_microphone("aux", "Voicemeeter AUX Input", []))
        devices = [{"id": item, "name": "Voicemeeter Out B2"} for item in ("one", "two")]
        self.assertIsNone(matching_virtual_microphone("aux", "Voicemeeter AUX Input", devices))


class HostInputRoutingTests(fixtures.HeadlessHostTests):
    def test_connect_selects_virtual_mic_and_disconnect_restores_physical(self):
        window = fixtures.headless_window()
        window.input_route = MagicMock()
        window._set_status("iphone_connected_muted")
        window._set_status("iphone_microphone_on")
        window.input_route.activate.assert_called_once()
        window._set_status("server_ready")
        window.input_route.restore.assert_called_once()
        self.assertFalse(window._input_route_active)

    def test_input_failure_does_not_skip_output_or_mute_restoration(self):
        window = fixtures.headless_window()
        window.input_route = MagicMock()
        window.input_route.restore.side_effect = InputRoutingError("failed")
        window.microphone_guard = MagicMock()
        self.assertFalse(window._restore_session_routes())
        window.output_route.restore.assert_called_once()
        window.microphone_guard.restore.assert_called_once()
        self.assertIn("микрофон ноутбука", window._input_route_error)

    def test_recovery_does_not_require_ip_or_mic_mute_checkbox(self):
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        window.output_route = None
        window.ip_var.set("")
        window.mute_local_microphone_var.set(False)
        with patch("remotevoice.host_app.OutputRoute") as output:
            self.assertTrue(window._recover_waiting_routes())
        output.return_value.restore.assert_called_once()
        self.input_class.return_value.restore.assert_called_once()

    def test_manual_restore_stops_live_host_before_other_callbacks(self):
        window = fixtures.headless_window()
        window._stop_host = MagicMock()
        window._return_to_laptop()
        window._stop_host.assert_called_once()

    def test_start_prevents_the_vm_feedback_route_before_accepting_phone(self):
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        with patch("remotevoice.host_app.OutputRoute"), patch(
            "remotevoice.host_app.RemoteVoiceServer"
        ), patch("remotevoice.host_app.threading.Thread"):
            window._start_host()
        self.mixer_configure.assert_called_once_with("Voicemeeter Out B1", "Voicemeeter AUX Input")
        self.assertTrue(window.server_running)

    def test_mixer_failure_does_not_open_listener(self):
        window = fixtures.HostOutputRoutingTests.configured_window(self)
        self.mixer_configure.side_effect = AudioBackendError("failed")
        with patch("remotevoice.host_app.OutputRoute"), patch(
            "remotevoice.host_app.RemoteVoiceServer"
        ) as server, patch("remotevoice.host_app.messagebox.showerror") as error:
            window._start_host()
        server.assert_not_called()
        error.assert_called_once_with("Не удалось разделить аудиоканалы", "failed", parent=window)
        self.assertIn("failed", window.status_var.get())
        self.assertFalse(window.server_running)


if __name__ == "__main__":
    unittest.main()
