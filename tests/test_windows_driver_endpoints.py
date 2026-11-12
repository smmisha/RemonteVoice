import ctypes as ct
import threading
import unittest
import uuid
from dataclasses import FrozenInstanceError
from unittest.mock import Mock, patch

from remontevoice import windows_driver_endpoints as endpoints

ROOT = "ROOT\\MEDIA\\0000"
RENDER = "{0.0.0.00000000}.{11111111-2222-3333-4444-555555555555}"
MIC = "{0.0.1.00000000}.{66666666-7777-8888-9999-aaaaaaaaaaaa}"


class Api:
    def __init__(self):
        self.initialized = self.uninitialized = 0
        self.init_result = 0
        self.collections = {0: ["render"], 1: ["mic"]}
        self.identifiers = {"render": RENDER, "mic": MIC}
        self.instances = {"render": "SWD\\MMDEVAPI\\" + RENDER, "mic": "SWD\\MMDEVAPI\\" + MIC}
        self.ids = {1: self.instances["render"], 2: self.instances["mic"], 3: ROOT, 4: "HTREE\\ROOT\\0"}
        self.parents = {1: 3, 2: 3, 3: 4}
        self.hardware = ("ROOT\\RemonteVoiceAudio",)
        self.service_name = "RemonteVoiceAudio"
        self.states = {"render": 1, "mic": 1}
        self.enumerated = []
        self.released = []
        self.located = []
        self.state_calls = {"render": 0, "mic": 0}
        self.count_override = None
        self.release_error = None
        self.uninitialize_error = None
        self.on_state = None

    def initialize(self):
        self.initialized += 1
        return self.init_result

    def uninitialize(self):
        self.uninitialized += 1
        if self.uninitialize_error:
            raise self.uninitialize_error

    def enumerator(self):
        return "enumerator"

    def endpoints(self, enumerator, flow, state):
        assert enumerator == "enumerator"
        self.enumerated.append((flow, state))
        return flow

    def count(self, collection):
        return self.count_override if self.count_override is not None else len(self.collections[collection])

    def item(self, collection, index):
        return self.collections[collection][index]

    def state(self, device):
        self.state_calls[device] = self.state_calls.get(device, 0) + 1
        return self.on_state(device) if self.on_state else self.states[device]

    def device_id(self, device):
        return self.identifiers[device]

    def instance_id(self, device):
        return self.instances[device]

    def locate(self, instance):
        self.located.append(instance)
        for node, identity in self.ids.items():
            if identity.casefold() == instance.casefold():
                return node
        raise OSError("Devnode disappeared")

    def node_id(self, node):
        return self.ids[node]

    def parent(self, node):
        return self.parents[node]

    def hardware_ids(self, node):
        assert node == 3
        return self.hardware

    def service(self, node):
        assert node == 3
        return self.service_name

    def release(self, reference):
        self.released.append(reference)
        if reference == self.release_error:
            raise OSError("COM release unconfirmed")


class DriverEndpointInventoryTests(unittest.TestCase):
    def make(self, api=None, instance=ROOT):
        api = api or Api()
        return endpoints.WindowsDriverEndpointInventory(instance, api=api), api

    def test_constructor_is_inert_and_rejects_unconfigured_or_wildcard_root(self):
        with patch.object(endpoints, "_WindowsApi") as native:
            inventory = endpoints.WindowsDriverEndpointInventory(ROOT)
            self.assertIsNone(inventory._api)
            native.assert_not_called()
            for value in (None, "", "ROOT\\MEDIA\\*", "ROOT\\MEDIA", "ROOT\\MEDIA\\0000\0",
                          "ROOT\\MEDIA\\0000\\extra", "PCI\\MEDIA\\0000", "ROOT\\MEDIA\\0000 "):
                with self.assertRaises(ValueError):
                    endpoints.WindowsDriverEndpointInventory(value)
            native.assert_not_called()

    def test_pair_binds_both_active_directions_and_releases_every_reference(self):
        inventory, api = self.make()
        pair = inventory.observe()
        self.assertEqual(pair, endpoints.DriverEndpointPair(RENDER, MIC))
        self.assertEqual(api.enumerated, [(0, 1), (1, 1)])
        self.assertEqual(api.state_calls, {"render": 2, "mic": 2})
        self.assertEqual(api.released, ["mic", 1, "render", 0, "enumerator"])
        self.assertEqual((api.initialized, api.uninitialized), (1, 1))
        self.assertTrue(inventory.cleanup_confirmed)
        self.assertNotIn(RENDER, repr(pair))
        with self.assertRaises(FrozenInstanceError):
            pair.capture_endpoint_id = "other"

    def test_absent_instance_property_uses_only_existing_exact_swd_mapping(self):
        inventory, api = self.make()
        api.instances = {"render": None, "mic": None}
        self.assertEqual(inventory.observe(), endpoints.DriverEndpointPair(RENDER, MIC))
        self.assertEqual(set(api.located), {"SWD\\MMDEVAPI\\" + RENDER, "SWD\\MMDEVAPI\\" + MIC})
        api.identifiers["render"] = "friendly RemonteVoice Speakers"
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()
        # A nonempty wrong property never falls back to the otherwise valid ID.
        api.identifiers["render"] = RENDER
        api.instances["render"] = "ROOT\\forged\\0000"
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()

    def test_unrelated_hardware_endpoint_is_skipped_without_friendly_name_or_audio(self):
        inventory, api = self.make()
        api.collections[0].insert(0, "physical")
        api.identifiers["physical"] = "physical-render-id"
        api.instances["physical"] = "HDAUDIO\\physical\\0001"
        api.ids[5] = api.instances["physical"]
        api.parents[5] = 4
        api.states["physical"] = 1
        self.assertEqual(inventory.observe(), endpoints.DriverEndpointPair(RENDER, MIC))
        self.assertEqual(api.state_calls["physical"], 1)

    def test_hardware_and_service_must_both_match_configured_root(self):
        for hardware, service in ((("ROOT\\OtherAudio",), "RemonteVoiceAudio"),
                                  (("ROOT\\RemonteVoiceAudioX",), "RemonteVoiceAudio"),
                                  (("ROOT\\RemonteVoiceAudio",), "OtherAudio"),
                                  (("ROOT\\RemonteVoiceAudio",), "RemonteVoiceAudio\0bad"),
                                  (("ROOT\\RemonteVoiceAudio", "root\\remontevoiceaudio"), "RemonteVoiceAudio"),
                                  ((), "RemonteVoiceAudio"), (["ROOT\\RemonteVoiceAudio"], "RemonteVoiceAudio")):
            with self.subTest(hardware=hardware, service=service):
                inventory, api = self.make()
                api.hardware, api.service_name = hardware, service
                with self.assertRaises(endpoints.DriverEndpointInventoryError):
                    inventory.observe()
                self.assertEqual(api.uninitialized, 1)
                self.assertTrue(inventory.cleanup_confirmed)
        inventory, api = self.make()
        api.ids[3] = "ROOT\\MEDIA\\0001"
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()
        alternate, _ = self.make(api, "root\\media\\0001")
        self.assertEqual(alternate.observe(), endpoints.DriverEndpointPair(RENDER, MIC))

    def test_missing_duplicate_or_extra_matching_direction_is_rejected(self):
        for case in ("missing", "duplicate", "extra", "same ID both directions"):
            with self.subTest(case=case):
                inventory, api = self.make()
                if case == "missing":
                    api.collections[1] = []
                elif case == "duplicate":
                    api.collections[0].append("render")
                elif case == "extra":
                    api.collections[0].append("other")
                    api.identifiers["other"] = "other-render"
                    api.instances["other"] = api.instances["render"]
                    api.states["other"] = 1
                else:
                    api.identifiers["mic"] = RENDER
                with self.assertRaises(endpoints.DriverEndpointInventoryError):
                    inventory.observe()
                self.assertEqual(api.uninitialized, 1)

    def test_disappear_inactive_or_changed_during_final_recheck_never_returns_pair(self):
        for case in ("inactive", "removed", "changed", "late inactive"):
            with self.subTest(case=case):
                inventory, api = self.make()
                if case == "inactive":
                    api.states["mic"] = 4
                elif case == "removed":
                    del api.ids[2]
                elif case == "changed":
                    def changed(device):
                        if device == "render" and api.state_calls[device] > 1:
                            api.identifiers[device] = "replacement-id"
                        return 1
                    api.on_state = changed
                else:
                    api.on_state = lambda device: 4 if api.state_calls[device] > 1 else 1
                with self.assertRaises(endpoints.DriverEndpointInventoryError):
                    inventory.observe()
                self.assertEqual(api.uninitialized, 1)

    def test_malformed_ids_counts_cycles_and_deep_ancestry_fail_with_bounds(self):
        for value in ("", " ", "id\0tail", 5, "x" * 1025):
            inventory, api = self.make()
            api.identifiers["render"] = value
            with self.assertRaises(endpoints.DriverEndpointInventoryError):
                inventory.observe()
        for count in (-1, 257, True, 1.5):
            inventory, api = self.make()
            api.count_override = count
            with self.assertRaises(endpoints.DriverEndpointInventoryError):
                inventory.observe()
            self.assertEqual(api.located, [])
        inventory, api = self.make()
        api.parents[1] = 1
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()
        inventory, api = self.make()
        for index in range(50, 90):
            api.ids[index] = f"SWD\\deep\\{index}"
            api.parents[index] = index + 1
        api.parents[1] = 50
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()

    def test_cleanup_failure_attempts_all_releases_and_poisoned_inventory_cannot_reobserve(self):
        for failure in ("release", "uninitialize"):
            with self.subTest(failure=failure):
                inventory, api = self.make()
                if failure == "release":
                    api.release_error = "mic"
                else:
                    api.uninitialize_error = OSError("Apartment cleanup unconfirmed")
                with self.assertRaises(endpoints.DriverEndpointInventoryError):
                    inventory.observe()
                self.assertEqual(api.released, ["mic", 1, "render", 0, "enumerator"])
                self.assertEqual(api.uninitialized, 1)
                self.assertFalse(inventory.cleanup_confirmed)
                previous = api.initialized
                with self.assertRaises(endpoints.DriverEndpointInventoryError):
                    inventory.observe()
                self.assertEqual(api.initialized, previous)
                if failure == "release":
                    self.assertEqual(inventory._unreleased, ["mic"])

    def test_nested_property_cleanup_failure_also_poisons_inventory(self):
        inventory, api = self.make()
        api.instance_id = Mock(side_effect=endpoints._MetadataCleanupError("Property value not freed"))
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()
        self.assertEqual(api.released, ["render", 0, "enumerator"])
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()
        self.assertEqual(api.initialized, 1)
        self.assertFalse(inventory.cleanup_confirmed)

    def test_cached_cleanup_is_false_during_observation_and_true_after_clean_mismatch(self):
        inventory, api = self.make()
        entered, release = threading.Event(), threading.Event()
        original_count = api.count
        def blocked_count(collection):
            entered.set()
            if not release.wait(1):
                raise AssertionError("Test failed to release observation")
            return original_count(collection)
        api.count = blocked_count
        failures = []
        worker = threading.Thread(target=lambda: self._observe_or_collect(inventory, failures))
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.assertFalse(inventory.cleanup_confirmed)
        finally:
            release.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertTrue(inventory.cleanup_confirmed)
        api.collections[1] = []
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()
        self.assertTrue(inventory.cleanup_confirmed)

    @staticmethod
    def _observe_or_collect(inventory, failures):
        try:
            inventory.observe()
        except BaseException as error:
            failures.append(error)

    def test_changed_apartment_mode_is_not_uninitialized_and_init_failure_acquires_nothing(self):
        for result in (0x80010106, -2147417850):
            inventory, api = self.make()
            api.init_result = result
            self.assertEqual(inventory.observe(), endpoints.DriverEndpointPair(RENDER, MIC))
            self.assertEqual(api.uninitialized, 0)
        inventory, api = self.make()
        api.init_result = 0x80004005
        with self.assertRaises(endpoints.DriverEndpointInventoryError):
            inventory.observe()
        self.assertEqual(api.released, [])
        self.assertEqual(api.uninitialized, 0)

    def test_fatal_primary_preserved_when_independent_cleanup_also_fails(self):
        class Fatal(BaseException):
            pass
        original = Fatal()
        inventory, api = self.make()
        api.instance_id = Mock(side_effect=original)
        api.uninitialize_error = OSError("Cleanup also failed")
        with self.assertRaises(Fatal) as raised:
            inventory.observe()
        self.assertIs(raised.exception, original)
        self.assertEqual(api.released, ["render", 0, "enumerator"])


class NativeMetadataBoundaryTests(unittest.TestCase):
    def test_sdk_property_key_and_propvariant_layout_are_pointer_safe(self):
        self.assertEqual(ct.sizeof(endpoints._PropertyKey), 20)
        self.assertEqual(endpoints._INSTANCE_KEY.id, 256)
        self.assertEqual(bytes(endpoints._INSTANCE_KEY.format),
            uuid.UUID("78c34fc8-104a-4aca-9ea4-524d52996e57").bytes_le)
        self.assertEqual(endpoints._PropVariant.value.offset, 8)
        self.assertEqual(ct.sizeof(endpoints._PropVariant), 24 if ct.sizeof(ct.c_void_p) == 8 else 16)

    def test_get_id_frees_os_allocation_even_when_hresult_fails(self):
        for result in (0, 0x80004005):
            with self.subTest(result=result):
                api = object.__new__(endpoints._WindowsApi)
                api._ole32 = Mock()
                text = ct.create_unicode_buffer(RENDER)
                address = ct.cast(text, ct.c_void_p).value
                def get_id(device, out):
                    ct.cast(out, ct.POINTER(ct.c_void_p))[0] = address
                    return result
                api._method = Mock(return_value=get_id)
                if result:
                    with self.assertRaises(endpoints._com.WindowsOutputError):
                        api.device_id(ct.c_void_p(1))
                else:
                    self.assertEqual(api.device_id(ct.c_void_p(1)), RENDER)
                self.assertEqual(api._method.call_args.args[1], 5)
                self.assertEqual(api._ole32.CoTaskMemFree.call_args.args[0].value, address)

    def test_property_store_uses_read_only_slot_and_clears_variant_on_every_outcome(self):
        for variant_type in (0, 31, 3):
            with self.subTest(variant_type=variant_type):
                api = object.__new__(endpoints._WindowsApi)
                api._ole32 = Mock()
                api._ole32.PropVariantClear.return_value = 0
                api._interface = Mock(return_value="properties")
                api.release = Mock()
                text = ct.create_unicode_buffer("SWD\\MMDEVAPI\\" + RENDER)
                def value(properties, key, out):
                    variant = ct.cast(out, ct.POINTER(endpoints._PropVariant)).contents
                    variant.vt = variant_type
                    if variant_type == 31:
                        variant.value.pointer = ct.cast(text, ct.c_void_p).value
                    return 0
                api._method = Mock(return_value=value)
                if variant_type == 3:
                    with self.assertRaises(endpoints.DriverEndpointInventoryError):
                        api.instance_id("device")
                else:
                    self.assertEqual(api.instance_id("device"), None if variant_type == 0 else text.value)
                self.assertEqual(api._interface.call_args.args, ("device", 4, (ct.c_uint32, 0)))
                self.assertEqual(api._method.call_args.args[1], 5)
                api._ole32.PropVariantClear.assert_called_once()
                api.release.assert_called_once_with("properties")

    def test_variant_cleanup_failure_still_releases_store_and_retains_failed_allocation(self):
        api = object.__new__(endpoints._WindowsApi)
        api._ole32 = Mock()
        api._ole32.PropVariantClear.return_value = 0x80004005
        api._interface = Mock(return_value="properties")
        api.release = Mock()
        api._method = Mock(return_value=lambda *args: 0)
        with self.assertRaises(endpoints._MetadataCleanupError):
            api.instance_id("device")
        api.release.assert_called_once_with("properties")
        self.assertEqual(len(api._metadata_retained), 1)
        self.assertIsInstance(api._metadata_retained[0], endpoints._PropVariant)


if __name__ == "__main__":
    unittest.main()
