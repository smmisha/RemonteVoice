import ctypes
import unittest
import uuid
from unittest.mock import MagicMock, call, patch

from remontevoice import windows_output


class WindowsOutputTests(unittest.TestCase):
    def test_get_reads_render_endpoint_and_releases_every_interface(self):
        for role in (0, 1, 2):
            with self.subTest(role=role), patch.object(windows_output, "_ComAdapter") as adapter_type:
                adapter = adapter_type.return_value
                adapter.initialize.return_value = 0
                adapter.create.return_value = "enumerator"
                adapter.default_device.return_value = "device"
                adapter.device_id.return_value = "opaque endpoint id"
                self.assertEqual(windows_output.get_default_output(role), "opaque endpoint id")
                adapter.default_device.assert_called_once_with("enumerator", role)
                adapter.device_id.assert_called_once_with("device")
                self.assertEqual(adapter.release.call_args_list, [call("device"), call("enumerator")])
                adapter.uninitialize.assert_called_once_with()

    def test_set_changes_only_requested_role_and_releases_policy(self):
        for role in (0, 1, 2):
            with self.subTest(role=role), patch.object(windows_output, "_ComAdapter") as adapter_type:
                adapter = adapter_type.return_value
                adapter.initialize.return_value = 0
                adapter.create.return_value = "policy"
                windows_output.set_default_output("opaque endpoint id", role)
                adapter.set_default.assert_called_once_with("policy", "opaque endpoint id", role)
                adapter.release.assert_called_once_with("policy")
                adapter.uninitialize.assert_called_once_with()

    def test_invalid_arguments_never_initialize_com(self):
        with patch.object(windows_output, "_ComAdapter") as adapter_type:
            for role in (-1, 3, True, False, None, "0", 0.0):
                with self.subTest(role=role):
                    with self.assertRaises(ValueError):
                        windows_output.get_default_output(role)
                    with self.assertRaises(ValueError):
                        windows_output.set_default_output("endpoint", role)
            for endpoint_id in (None, "", "  ", "bad\0id", b"endpoint", 1, "x" * 32768):
                with self.subTest(endpoint_id_type=type(endpoint_id).__name__):
                    with self.assertRaises(ValueError):
                        windows_output.set_default_output(endpoint_id, 1)
            adapter_type.assert_not_called()

    def test_existing_com_success_is_balanced_but_changed_mode_is_not(self):
        for initialize_result, balanced in ((0, True), (1, True), (0x80010106, False), (-2147417850, False)):
            with self.subTest(result=initialize_result), patch.object(windows_output, "_ComAdapter") as adapter_type:
                adapter = adapter_type.return_value
                adapter.initialize.return_value = initialize_result
                adapter.create.return_value = "policy"
                windows_output.set_default_output("endpoint", 0)
                self.assertEqual(adapter.uninitialize.call_count, int(balanced))

    def test_initialize_failure_never_creates_interface_or_uninitializes(self):
        with patch.object(windows_output, "_ComAdapter") as adapter_type:
            adapter = adapter_type.return_value
            adapter.initialize.return_value = 0x80004005
            with self.assertRaisesRegex(windows_output.WindowsOutputError, "0x80004005"):
                windows_output.get_default_output(0)
            adapter.create.assert_not_called()
            adapter.uninitialize.assert_not_called()

    def test_failure_reading_id_releases_device_and_enumerator_and_com(self):
        with patch.object(windows_output, "_ComAdapter") as adapter_type:
            adapter = adapter_type.return_value
            adapter.initialize.return_value = 0
            adapter.create.return_value = "enumerator"
            adapter.default_device.return_value = "device"
            adapter.device_id.side_effect = windows_output.WindowsOutputError("test failure")
            with self.assertRaises(windows_output.WindowsOutputError):
                windows_output.get_default_output(2)
            self.assertEqual(adapter.release.call_args_list, [call("device"), call("enumerator")])
            adapter.uninitialize.assert_called_once_with()

    def test_failure_finding_default_releases_enumerator_and_com(self):
        with patch.object(windows_output, "_ComAdapter") as adapter_type:
            adapter = adapter_type.return_value
            adapter.initialize.return_value = 0
            adapter.create.return_value = "enumerator"
            adapter.default_device.side_effect = windows_output.WindowsOutputError("test failure")
            with self.assertRaises(windows_output.WindowsOutputError):
                windows_output.get_default_output(0)
            adapter.release.assert_called_once_with("enumerator")
            adapter.uninitialize.assert_called_once_with()

    def test_unavailable_setter_is_reported_and_com_is_released(self):
        with patch.object(windows_output, "_ComAdapter") as adapter_type:
            adapter = adapter_type.return_value
            adapter.initialize.return_value = 0
            adapter.create.side_effect = windows_output.WindowsOutputError("IPolicyConfig unavailable")
            with self.assertRaisesRegex(windows_output.WindowsOutputError, "IPolicyConfig"):
                windows_output.set_default_output("endpoint", 0)
            adapter.set_default.assert_not_called()
            adapter.release.assert_not_called()
            adapter.uninitialize.assert_called_once_with()

    def test_setter_failure_releases_policy_and_com(self):
        with patch.object(windows_output, "_ComAdapter") as adapter_type:
            adapter = adapter_type.return_value
            adapter.initialize.return_value = 0
            adapter.create.return_value = "policy"
            adapter.set_default.side_effect = windows_output.WindowsOutputError("test failure")
            with self.assertRaises(windows_output.WindowsOutputError):
                windows_output.set_default_output("endpoint", 0)
            adapter.release.assert_called_once_with("policy")
            adapter.uninitialize.assert_called_once_with()

    def test_hresult_is_exactly_signed_32_bits(self):
        self.assertEqual(ctypes.sizeof(windows_output._HRESULT), 4)
        windows_output._check_hresult(0, "test")
        windows_output._check_hresult(1, "test")
        for result in (0x80004005, -2147467259):
            with self.assertRaisesRegex(windows_output.WindowsOutputError, "0x80004005"):
                windows_output._check_hresult(result, "test")

    def test_guid_layout_matches_windows_sdk(self):
        self.assertEqual(ctypes.sizeof(windows_output._GUID), 16)
        self.assertEqual(bytes(windows_output._IID_IPOLICY_CONFIG),
                         uuid.UUID("f8679f50-850a-41cf-9c72-430f290290c8").bytes_le)
        self.assertEqual(bytes(windows_output._CLSID_POLICY_CONFIG),
                         uuid.UUID("870af99c-171d-4f9e-af0d-e63df40c2bc9").bytes_le)

    def test_device_id_frees_wide_string_allocation_even_on_error(self):
        buffer = ctypes.create_unicode_buffer("opaque device id")
        pointer = ctypes.cast(buffer, ctypes.c_void_p).value
        for result in (0, 0x80004005):
            with self.subTest(result=result):
                adapter = object.__new__(windows_output._ComAdapter)
                adapter._ole32 = MagicMock()

                def get_id(device, out_pointer):
                    ctypes.cast(out_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = pointer
                    return result

                with patch.object(adapter, "_method", return_value=get_id) as method:
                    if result == 0:
                        self.assertEqual(adapter.device_id(ctypes.c_void_p(1)), "opaque device id")
                    else:
                        with self.assertRaises(windows_output.WindowsOutputError):
                            adapter.device_id(ctypes.c_void_p(1))
                    self.assertEqual(method.call_args.args[1], 5)
                adapter._ole32.CoTaskMemFree.assert_called_once()
                self.assertEqual(adapter._ole32.CoTaskMemFree.call_args.args[0].value, pointer)

    def test_setter_uses_vtable_slot_thirteen_and_correct_argument_types(self):
        adapter = object.__new__(windows_output._ComAdapter)
        policy = ctypes.c_void_p(1)
        with patch.object(adapter, "_method", return_value=MagicMock(return_value=0)) as method:
            adapter.set_default(policy, "opaque endpoint", 2)
            self.assertEqual(method.call_args.args,
                             (policy, 13, windows_output._HRESULT, ctypes.c_wchar_p, ctypes.c_int32))
            method.return_value.assert_called_once_with(policy, "opaque endpoint", 2)

    def test_default_device_uses_render_direction_and_slot_four(self):
        adapter = object.__new__(windows_output._ComAdapter)
        enumerator = ctypes.c_void_p(1)

        def default_device(pointer, flow, role, out_pointer):
            self.assertIs(pointer, enumerator)
            self.assertEqual(flow, 0)
            self.assertEqual(role, 1)
            ctypes.cast(out_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 2
            return 0

        with patch.object(adapter, "_method", return_value=default_device) as method:
            self.assertEqual(adapter.default_device(enumerator, 1).value, 2)
            self.assertEqual(method.call_args.args[1], 4)

    def test_create_instance_uses_in_process_context_and_releases_failed_pointer(self):
        for result in (0, 0x80004005):
            with self.subTest(result=result):
                adapter = object.__new__(windows_output._ComAdapter)
                adapter._ole32 = MagicMock()

                def create(clsid, outer, context, iid, out_pointer):
                    self.assertIsNone(outer)
                    self.assertEqual(context, 1)
                    self.assertEqual(ctypes.string_at(clsid, 16), bytes(windows_output._CLSID_POLICY_CONFIG))
                    self.assertEqual(ctypes.string_at(iid, 16), bytes(windows_output._IID_IPOLICY_CONFIG))
                    ctypes.cast(out_pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 2
                    return result

                adapter._ole32.CoCreateInstance.side_effect = create
                with patch.object(adapter, "release") as release:
                    if result == 0:
                        pointer = adapter.create(windows_output._CLSID_POLICY_CONFIG,
                                                 windows_output._IID_IPOLICY_CONFIG, "test")
                        self.assertEqual(pointer.value, 2)
                        release.assert_not_called()
                    else:
                        with self.assertRaises(windows_output.WindowsOutputError):
                            adapter.create(windows_output._CLSID_POLICY_CONFIG,
                                           windows_output._IID_IPOLICY_CONFIG, "test")
                        self.assertEqual(release.call_args.args[0].value, 2)

    def test_success_with_null_pointers_is_explicit_failure(self):
        adapter = object.__new__(windows_output._ComAdapter)
        adapter._ole32 = MagicMock()
        adapter._ole32.CoCreateInstance.return_value = 0
        with self.assertRaises(windows_output.WindowsOutputError):
            adapter.create(windows_output._CLSID_POLICY_CONFIG, windows_output._IID_IPOLICY_CONFIG, "test")
        with patch.object(adapter, "_method", return_value=MagicMock(return_value=0)):
            with self.assertRaises(windows_output.WindowsOutputError):
                adapter.default_device(ctypes.c_void_p(1), 0)
            with self.assertRaises(windows_output.WindowsOutputError):
                adapter.device_id(ctypes.c_void_p(1))
        adapter._ole32.CoTaskMemFree.assert_not_called()

    def test_release_calls_iunknown_slot_two(self):
        adapter = object.__new__(windows_output._ComAdapter)
        pointer = ctypes.c_void_p(2)
        with patch.object(adapter, "_method", return_value=MagicMock(return_value=0)) as method:
            adapter.release(pointer)
            self.assertEqual(method.call_args.args, (pointer, 2, ctypes.c_uint32))
            method.return_value.assert_called_once_with(pointer)
            adapter.release(ctypes.c_void_p())
            self.assertEqual(method.call_count, 1)

    def test_dll_boundary_declares_explicit_32_bit_hresult_and_pointer_types(self):
        dll = MagicMock()
        with patch.object(windows_output.sys, "platform", "win32"), patch.object(
            windows_output.ctypes, "WinDLL", return_value=dll, create=True
        ) as loader:
            adapter = windows_output._ComAdapter()
            loader.assert_called_once_with("ole32", use_last_error=True)
            self.assertIs(dll.CoInitializeEx.restype, ctypes.c_int32)
            self.assertIs(dll.CoCreateInstance.restype, ctypes.c_int32)
            self.assertEqual(dll.CoTaskMemFree.argtypes, [ctypes.c_void_p])
            self.assertIsNone(dll.CoTaskMemFree.restype)
            dll.CoInitializeEx.return_value = 0
            self.assertEqual(adapter.initialize(), 0)
            dll.CoInitializeEx.assert_called_once_with(None, 2)
            adapter.uninitialize()
            dll.CoUninitialize.assert_called_once_with()

    def test_non_windows_fails_explicitly_before_loading_dll(self):
        with patch.object(windows_output.sys, "platform", "linux"):
            with self.assertRaisesRegex(windows_output.WindowsOutputError, "Windows"):
                windows_output.get_default_output(0)


if __name__ == "__main__":
    unittest.main()
