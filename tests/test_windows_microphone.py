import ctypes
import unittest
import uuid
from unittest.mock import MagicMock, patch

from remontevoice import windows_microphone as microphone
from remontevoice import windows_output


class FakeComAdapter:
    """Local COM signatures only; no DLL, device, or audio access takes place."""

    def __init__(self):
        self.direction = 1
        self.muted = 0
        self.failure_stage = None
        self.null_stage = None
        self.failure_pointer = False
        self.released = []
        self.calls = []
        self.stages = []
        self.set_values = []
        self.success_result = 0
        self.created_guids = None
        self.created = []
        self.default_requests = []
        self.default_sets = []

    def create(self, clsid, iid, operation):
        self.created_guids = (bytes(clsid), bytes(iid))
        self.created.append(self.created_guids)
        if self.failure_stage == "create":
            raise windows_output.WindowsOutputError("test unavailable")
        if bytes(clsid) == bytes(windows_output._CLSID_POLICY_CONFIG):
            if self.failure_stage == "policy":
                raise windows_output.WindowsOutputError("test policy unavailable")
            return ctypes.c_void_p(5)
        return ctypes.c_void_p(1)

    def device_id(self, device):
        if self.failure_stage == "device_id":
            raise windows_output.WindowsOutputError("test identifier unavailable")
        assert device.value == 2
        return "default capture endpoint"

    def set_default(self, policy, endpoint_id, role):
        assert policy.value == 5
        self.default_sets.append((endpoint_id, role))
        if self.failure_stage == "set_default":
            raise windows_output.WindowsOutputError("test set default failed")

    def release(self, pointer):
        if pointer:
            self.released.append(pointer.value)

    def _method(self, pointer, index, result_type, *argument_types):
        self.calls.append((pointer.value, index, result_type, argument_types))
        stages = {(1, 5): "get_device", (1, 4): "get_default",
                  (2, 0): "query_endpoint", (3, 3): "direction",
                  (2, 3): "activate", (4, 15): "get_mute", (4, 14): "set_mute"}
        stage = stages[(pointer.value, index)]

        def invoke(this, *arguments):
            self.stages.append(stage)
            if stage in ("get_device", "get_default", "query_endpoint", "activate"):
                output_value = {"get_device": 2, "get_default": 2,
                                "query_endpoint": 3, "activate": 4}[stage]
                if stage != self.null_stage and (stage != self.failure_stage or self.failure_pointer):
                    ctypes.cast(arguments[-1], ctypes.POINTER(ctypes.c_void_p))[0] = output_value
            if stage == self.failure_stage:
                return 0x80004005
            if stage == "direction":
                ctypes.cast(arguments[0], ctypes.POINTER(ctypes.c_int32))[0] = self.direction
            elif stage == "get_mute":
                ctypes.cast(arguments[0], ctypes.POINTER(ctypes.c_int32))[0] = self.muted
            elif stage == "set_mute":
                self.set_values.append(arguments)
            elif stage == "get_device":
                self.requested_id = arguments[0]
            elif stage == "get_default":
                self.default_requests.append(arguments[:2])
            elif stage == "query_endpoint":
                self.endpoint_iid = ctypes.string_at(arguments[0], 16)
            elif stage == "activate":
                self.volume_iid = ctypes.string_at(arguments[0], 16)
                self.activation_context = arguments[1]
                self.activation_parameters = arguments[2]
            return self.success_result

        return invoke


class WindowsMicrophoneTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeComAdapter()
        self.apartment = MagicMock()
        self.apartment.__enter__.return_value = self.adapter
        self.patch = patch.object(microphone._com, "_com_apartment", return_value=self.apartment)
        self.apartment_factory = self.patch.start()
        self.addCleanup(self.patch.stop)

    def assert_released(self, expected):
        self.assertEqual(self.adapter.released, expected)
        self.apartment.__exit__.assert_called_once()

    def test_get_returns_bool_and_never_changes_mute(self):
        for mute_value in (0, 1, -1):
            with self.subTest(mute_value=mute_value):
                self.adapter.muted = mute_value
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                result = microphone.get_microphone_mute("opaque capture endpoint")
                self.assertIs(type(result), bool)
                self.assertEqual(result, bool(mute_value))
                self.assertEqual(self.adapter.set_values, [])
                self.assertEqual(self.adapter.requested_id, "opaque capture endpoint")
                self.assert_released([4, 3, 2, 1])

    def test_set_changes_exact_bool_with_null_event_context(self):
        for muted in (True, False):
            with self.subTest(muted=muted):
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                microphone.set_microphone_mute("opaque capture endpoint", muted)
                self.assertEqual(self.adapter.set_values[-1], (int(muted), None))
                self.assertNotIn("get_mute", self.adapter.stages)
                self.assert_released([4, 3, 2, 1])

    def test_speaker_and_unknown_directions_cannot_be_muted_or_read(self):
        for direction in (0, 2, -1):
            for operation in (microphone.get_microphone_mute,
                              lambda endpoint_id: microphone.set_microphone_mute(endpoint_id, True)):
                with self.subTest(direction=direction, operation=operation):
                    self.adapter.direction = direction
                    self.adapter.released.clear()
                    self.adapter.stages.clear()
                    self.apartment.__exit__.reset_mock()
                    with self.assertRaisesRegex(microphone.WindowsMicrophoneError, "не является микрофоном"):
                        operation("speaker endpoint must not change")
                    self.assertNotIn("activate", self.adapter.stages)
                    self.assertEqual(self.adapter.set_values, [])
                    self.assert_released([3, 2, 1])

    def test_invalid_endpoint_and_non_bool_values_never_open_com(self):
        for endpoint_id in (None, "", " ", "bad\0id", b"device", 7, "a" * 32768):
            with self.subTest(endpoint_type=type(endpoint_id).__name__):
                with self.assertRaises(ValueError):
                    microphone.get_microphone_mute(endpoint_id)
                with self.assertRaises(ValueError):
                    microphone.set_microphone_mute(endpoint_id, True)
        for muted in (None, 0, 1, "true", [], object()):
            with self.subTest(muted_type=type(muted).__name__):
                with self.assertRaises(ValueError):
                    microphone.set_microphone_mute("endpoint", muted)
        self.apartment_factory.assert_not_called()

    def test_sdk_guids_slots_and_argument_types(self):
        microphone.get_microphone_mute("endpoint")
        self.assertEqual(self.adapter.created_guids, (
            uuid.UUID("bcde0395-e52f-467c-8e3d-c4579291692e").bytes_le,
            uuid.UUID("a95664d2-9614-4f35-a746-de8db63617e6").bytes_le,
        ))
        self.assertEqual(self.adapter.endpoint_iid,
                         uuid.UUID("1be09788-6894-4089-8586-9a2a6c265ac5").bytes_le)
        self.assertEqual(self.adapter.volume_iid,
                         uuid.UUID("5cdf2c82-841e-4546-9722-0cf74078229a").bytes_le)
        self.assertEqual(self.adapter.activation_context, 1)
        self.assertIsNone(self.adapter.activation_parameters)
        self.assertEqual([(entry[0], entry[1]) for entry in self.adapter.calls],
                         [(1, 5), (2, 0), (3, 3), (2, 3), (4, 15)])
        self.assertEqual(self.adapter.calls[-1][2:],
                         (ctypes.c_int32, (ctypes.POINTER(ctypes.c_int32),)))
        self.adapter.calls.clear()
        microphone.set_microphone_mute("endpoint", True)
        self.assertEqual(self.adapter.calls[-1][1:],
                         (14, ctypes.c_int32, (ctypes.c_int32, ctypes.POINTER(windows_output._GUID))))
        self.assertEqual(ctypes.sizeof(microphone._BOOL), 4)

    def test_failure_at_each_read_stage_releases_prior_references(self):
        for stage, released in (("get_device", [1]), ("query_endpoint", [2, 1]),
                                ("direction", [3, 2, 1]), ("activate", [3, 2, 1]),
                                ("get_mute", [4, 3, 2, 1])):
            with self.subTest(stage=stage):
                self.adapter.failure_stage = stage
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                with self.assertRaisesRegex(microphone.WindowsMicrophoneError, "0x80004005") as raised:
                    microphone.get_microphone_mute("PRIVATE-ENDPOINT-ID")
                self.assertNotIn("PRIVATE-ENDPOINT-ID", str(raised.exception))
                self.assert_released(released)

    def test_failed_call_with_returned_pointer_also_releases_it(self):
        self.adapter.failure_pointer = True
        for stage, released in (("get_device", [2, 1]), ("query_endpoint", [3, 2, 1]),
                                ("activate", [4, 3, 2, 1])):
            with self.subTest(stage=stage):
                self.adapter.failure_stage = stage
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                with self.assertRaises(microphone.WindowsMicrophoneError):
                    microphone.get_microphone_mute("endpoint")
                self.assert_released(released)

    def test_success_with_null_interface_is_reported_and_prior_references_released(self):
        for stage, released in (("get_device", [1]), ("query_endpoint", [2, 1]),
                                ("activate", [3, 2, 1])):
            with self.subTest(stage=stage):
                self.adapter.null_stage = stage
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                with self.assertRaisesRegex(microphone.WindowsMicrophoneError, "COM-интерфейс недоступен"):
                    microphone.get_microphone_mute("endpoint")
                self.assert_released(released)

    def test_mute_failure_releases_all_references(self):
        self.adapter.failure_stage = "set_mute"
        with self.assertRaisesRegex(microphone.WindowsMicrophoneError, "0x80004005"):
            microphone.set_microphone_mute("endpoint", True)
        self.assert_released([4, 3, 2, 1])

    def test_already_muted_s_false_is_success(self):
        self.adapter.success_result = 1
        microphone.set_microphone_mute("endpoint", True)
        self.assertEqual(self.adapter.set_values, [(1, None)])
        self.assert_released([4, 3, 2, 1])

    def test_com_unavailable_errors_use_microphone_error_type(self):
        self.apartment.__enter__.side_effect = windows_output.WindowsOutputError("test COM unavailable")
        with self.assertRaisesRegex(microphone.WindowsMicrophoneError, "микрофоном недоступно"):
            microphone.get_microphone_mute("endpoint")
        self.assertEqual(self.adapter.released, [])

    def test_enumerator_failure_uses_microphone_error_and_exits_apartment(self):
        self.adapter.failure_stage = "create"
        with self.assertRaises(microphone.WindowsMicrophoneError):
            microphone.set_microphone_mute("endpoint", False)
        self.assert_released([])

    def test_get_default_uses_capture_direction_and_each_requested_role(self):
        for role in (0, 1, 2):
            with self.subTest(role=role):
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                self.assertEqual(microphone.get_default_microphone(role), "default capture endpoint")
                self.assertEqual(self.adapter.default_requests[-1], (1, role))
                self.assert_released([2, 1])
        self.assertEqual(self.adapter.calls[-1][1:], (
            4, ctypes.c_int32,
            (ctypes.c_int32, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p)),
        ))
        self.assertNotIn("activate", self.adapter.stages)
        self.assertEqual(self.adapter.set_values, [])
        self.assertEqual(self.adapter.default_sets, [])

    def test_set_default_verifies_capture_without_opening_volume(self):
        for role in (0, 1, 2):
            with self.subTest(role=role):
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                microphone.set_default_microphone("capture endpoint", role)
                self.assertEqual(self.adapter.default_sets[-1], ("capture endpoint", role))
                self.assert_released([5, 3, 2, 1])
        self.assertNotIn("activate", self.adapter.stages)
        self.assertEqual(self.adapter.set_values, [])
        self.assertEqual(self.adapter.created[-1], (
            bytes(windows_output._CLSID_POLICY_CONFIG), bytes(windows_output._IID_IPOLICY_CONFIG),
        ))

    def test_set_default_rejects_speakers_before_policy_creation(self):
        self.adapter.direction = 0
        with self.assertRaisesRegex(microphone.WindowsMicrophoneError, "не является микрофоном"):
            microphone.set_default_microphone("speaker", 0)
        self.assertEqual(len(self.adapter.created), 1)
        self.assertEqual(self.adapter.default_sets, [])
        self.assert_released([3, 2, 1])

    def test_default_invalid_arguments_do_not_open_com(self):
        for role in (-1, 3, True, False, None, "0", 0.0):
            with self.subTest(role=role):
                with self.assertRaises(ValueError):
                    microphone.get_default_microphone(role)
                with self.assertRaises(ValueError):
                    microphone.set_default_microphone("endpoint", role)
        for endpoint_id in (None, "", " ", "bad\0id", b"device", 7, "a" * 32768):
            with self.subTest(endpoint_type=type(endpoint_id).__name__):
                with self.assertRaises(ValueError):
                    microphone.set_default_microphone(endpoint_id, 0)
        self.apartment_factory.assert_not_called()

    def test_default_read_failures_release_and_use_microphone_error(self):
        for stage, released in (("get_default", [1]), ("device_id", [2, 1])):
            with self.subTest(stage=stage):
                self.adapter.failure_stage = stage
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                with self.assertRaises(microphone.WindowsMicrophoneError):
                    microphone.get_default_microphone(2)
                self.assert_released(released)

    def test_default_read_releases_pointer_returned_with_failure(self):
        self.adapter.failure_stage = "get_default"
        self.adapter.failure_pointer = True
        with self.assertRaises(microphone.WindowsMicrophoneError):
            microphone.get_default_microphone(0)
        self.assert_released([2, 1])

    def test_default_read_rejects_null_device(self):
        self.adapter.null_stage = "get_default"
        with self.assertRaisesRegex(microphone.WindowsMicrophoneError, "COM-интерфейс недоступен"):
            microphone.get_default_microphone(0)
        self.assert_released([1])

    def test_default_policy_creation_and_set_failures_release_every_reference(self):
        for stage, released in (("policy", [3, 2, 1]), ("set_default", [5, 3, 2, 1])):
            with self.subTest(stage=stage):
                self.adapter.failure_stage = stage
                self.adapter.released.clear()
                self.apartment.__exit__.reset_mock()
                with self.assertRaises(microphone.WindowsMicrophoneError):
                    microphone.set_default_microphone("endpoint", 2)
                self.assert_released(released)


if __name__ == "__main__":
    unittest.main()
