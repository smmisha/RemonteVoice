"""Labels must stay readable without changing the persisted device identity."""
import unittest

from remontevoice.desktop_ui import device_choices


class DeviceChoiceTests(unittest.TestCase):
    def test_unique_names_hide_ids_but_preserve_identity(self):
        self.assertEqual(device_choices([{'id': 'private-guid', 'name': 'Speakers'}]),
                         {'Speakers': 'private-guid'})

    def test_duplicate_and_generated_name_collisions_do_not_drop_devices(self):
        devices = [{'id': 'one', 'name': 'USB Microphone'},
                   {'id': 'two', 'name': 'USB Microphone'},
                   {'id': 'three', 'name': 'USB Microphone · one'}]
        choices = device_choices(devices)
        self.assertEqual(len(choices), 3)
        self.assertEqual(set(choices.values()), {'one', 'two', 'three'})

    def test_capture_and_loopback_are_distinct_without_raw_id_in_normal_label(self):
        choices = device_choices([{'id': 'capture', 'name': 'Output', 'loopback': 'False'},
                                  {'id': 'loopback', 'name': 'Output', 'loopback': 'True'}], capture=True)
        self.assertEqual(choices, {'Output': 'capture', 'Output · системный звук': 'loopback'})


if __name__ == '__main__':
    unittest.main()
