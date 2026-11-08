import ctypes as ct
import unittest
from unittest.mock import patch

from remotevoice.driver_mic import DriverMicError
from remotevoice.windows_driver_transport import _WindowsApi


class SetupApi:
    """SetupAPI metadata substitutes, not actual device handles or driver I/O."""
    def __init__(self, entries, *, kind=7, terminated=True, bad_path=False, close_ok=True):
        self.entries = entries
        self.kind = kind
        self.terminated = terminated
        self.bad_path = bad_path
        self.close_ok = close_ok
        self.error = 0
        self.closed = 0

    def SetupDiGetClassDevsW(self, *_args): return 711

    def SetupDiEnumDeviceInterfaces(self, info, dev, category, index, interface):
        if index >= len(self.entries):
            self.error = 259; return 0
        interface._obj.Flags = index
        return 1

    def SetupDiGetDeviceInterfaceDetailW(self, info, interface, detail, size, required, device):
        path, _ = self.entries[interface._obj.Flags]
        encoded = (path + ("" if self.bad_path else "\0")).encode("utf-16-le")
        needed = len(encoded) + 4
        if required is not None: required._obj.value = needed
        if detail is None:
            self.error = 122; return 0
        assert size == needed and ct.c_uint32.from_buffer(detail).value == 8
        ct.memmove(ct.addressof(detail) + 4, encoded, len(encoded))
        device._obj.DevInst = interface._obj.Flags
        return 1

    def SetupDiGetDeviceRegistryPropertyW(self, info, device, prop, kind, data, size, required):
        _, hardware_ids = self.entries[device._obj.DevInst]
        encoded = ("\0".join(hardware_ids) + ("\0\0" if self.terminated else "")).encode("utf-16-le")
        kind._obj.value, required._obj.value = self.kind, len(encoded)
        ct.memmove(data, encoded, len(encoded))
        return 1

    def SetupDiDestroyDeviceInfoList(self, info):
        self.closed += 1
        return int(self.close_ok)


class DriverIdentityTests(unittest.TestCase):
    def discover(self, setup):
        api = _WindowsApi.__new__(_WindowsApi)
        api.setup = setup
        with patch("remotevoice.windows_driver_transport.ct.get_last_error", side_effect=lambda: setup.error, create=True):
            return api.discover()

    def test_requires_exact_hardware_id_and_wave_filter_not_display_name(self):
        setup = SetupApi([
            (r"\\?\unrelated\WaveMicArray1", [r"ROOT\OtherVendor"]),
            (r"\\?\root\RemoteVoice Microphone", [r"ROOT\RemoteVoiceAudio"]),
            (r"\\?\root\TopologyMicArray1", [r"ROOT\RemoteVoiceAudio"]),
            (r"\\?\root\WAVEMICARRAY1", [r"root\remotevoiceaudio"]),
        ])
        endpoints = self.discover(setup)
        self.assertEqual([e.path for e in endpoints], [r"\\?\root\WAVEMICARRAY1"])
        self.assertEqual(setup.closed, 1)

    def test_mismatching_hardware_id_prefix_does_not_match(self):
        setup = SetupApi([(r"\\?\root\WaveMicArray1", [r"ROOT\RemoteVoiceAudioSpoof"])])
        self.assertEqual(self.discover(setup), [])
        self.assertEqual(setup.closed, 1)

    def test_no_interfaces_still_closes_metadata_handle(self):
        setup = SetupApi([])
        self.assertEqual(self.discover(setup), [])
        self.assertEqual(setup.closed, 1)

    def test_malformed_hardware_metadata_or_path_fails_closed(self):
        for options in ({"kind": 1}, {"terminated": False}, {"bad_path": True}):
            with self.subTest(options=options):
                setup = SetupApi([(r"\\?\root\WaveMicArray1", [r"ROOT\RemoteVoiceAudio"])], **options)
                with self.assertRaises(DriverMicError): self.discover(setup)
                self.assertEqual(setup.closed, 1)

    def test_metadata_cleanup_failure_is_not_success(self):
        setup = SetupApi([], close_ok=False)
        with self.assertRaisesRegex(DriverMicError, "cleanup unconfirmed"):
            self.discover(setup)

    def test_property_size_is_bounded_before_allocating_or_reading(self):
        setup = SetupApi([(r"\\?\root\WaveMicArray1", [r"ROOT\RemoteVoiceAudio"])])
        def oversized(info, interface, detail, size, required, device):
            required._obj.value = 65538; setup.error = 122; return 0
        setup.SetupDiGetDeviceInterfaceDetailW = oversized
        with self.assertRaises(DriverMicError): self.discover(setup)
        self.assertEqual(setup.closed, 1)

    def test_configured_instance_excludes_another_device_with_same_hardware(self):
        setup = SetupApi([(r'\\?\one\WaveMicArray1', [r'ROOT\RemoteVoiceAudio']),
                          (r'\\?\two\WaveMicArray1', [r'ROOT\RemoteVoiceAudio'])])
        def instance(info, device, data, capacity, required):
            text = [r'ROOT\MEDIA\0001', r'ROOT\MEDIA\0000'][device._obj.DevInst]
            data.value = text
            required._obj.value = len(text) + 1
            return 1
        setup.SetupDiGetDeviceInstanceIdW = instance
        api = _WindowsApi.__new__(_WindowsApi); api.setup = setup
        with patch('remotevoice.windows_driver_transport.ct.get_last_error', side_effect=lambda: setup.error, create=True):
            matches = api.discover(r'ROOT\MEDIA\0000')
        self.assertEqual([e.path for e in matches], [r'\\?\two\WaveMicArray1'])
        self.assertEqual(setup.closed, 1)

    def test_instance_query_failure_rejects_before_opening_filter(self):
        setup = SetupApi([(r'\\?\one\WaveMicArray1', [r'ROOT\RemoteVoiceAudio'])])
        setup.SetupDiGetDeviceInstanceIdW = lambda *_: 0
        api = _WindowsApi.__new__(_WindowsApi); api.setup = setup
        with patch('remotevoice.windows_driver_transport.ct.get_last_error', side_effect=lambda: setup.error, create=True):
            with self.assertRaises(DriverMicError): api.discover(r'ROOT\MEDIA\0000')
        self.assertEqual(setup.closed, 1)

    def test_bad_instance_pin_is_rejected_without_enumeration(self):
        api = _WindowsApi.__new__(_WindowsApi)
        for text in ('', r'ROOT\OTHER\0000', r'ROOT\MEDIA\*', 42):
            with self.subTest(text=text):
                with self.assertRaises(ValueError): api.discover(text)
