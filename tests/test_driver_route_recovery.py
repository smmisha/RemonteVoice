import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from remontevoice.driver_route_recovery import (
    DriverRecoveryError, DriverRouteRecovery, load_driver_recovery, save_driver_recovery,
)


PLAN = DriverRouteRecovery('own-render', 'physical-speaker', 'own-mic', 'physical-mic')


class DriverRouteRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.target = self.folder / 'driver-route-recovery.json'

    def document(self):
        return dict(schema_version=1, output_remote='own-render', output_fallback='physical-speaker',
                    input_remote='own-mic', input_fallback='physical-mic')

    def test_absence_is_none_and_save_load_preserves_record_without_device_calls(self):
        self.assertIsNone(load_driver_recovery(self.folder))
        with patch('remontevoice.driver_route_recovery.os.fsync', wraps=__import__('os').fsync) as sync:
            save_driver_recovery(self.folder, PLAN)
        sync.assert_called_once()
        self.assertEqual(load_driver_recovery(self.folder), PLAN)
        self.assertTrue(self.target.is_file())
        self.assertEqual(list(self.folder.iterdir()), [self.target])
        self.assertNotIn('own-render', repr(PLAN))

    def test_strict_keys_schema_and_types(self):
        variants = []
        for key, value in [('schema_version', True), ('schema_version', 2), ('schema_version', '1'),
                           ('output_remote', ''), ('input_remote', 1), ('input_fallback', None),
                           ('output_fallback', 'OWN-RENDER'), ('input_fallback', 'OWN-MIC')]:
            document = self.document(); document[key] = value; variants.append(document)
        document = self.document(); del document['input_remote']; variants.append(document)
        document = self.document(); document['backend_ready'] = True; variants.append(document)
        variants += [[], None, True]
        for document in variants:
            with self.subTest(document_type=type(document).__name__):
                self.target.write_text(json.dumps(document), 'utf-8')
                with self.assertRaises(DriverRecoveryError) as error:
                    load_driver_recovery(self.folder)
                self.assertNotIn('own-render', str(error.exception))

    def test_duplicate_keys_malformed_encoding_json_and_oversize_rejected(self):
        duplicate = json.dumps(self.document())[:-1] + ',"input_remote":"different"}'
        for payload in (duplicate.encode(), b'{broken', b'\xff', b' ' * (16 * 1024 + 1), b'[' * 1200):
            self.target.write_bytes(payload)
            with self.assertRaises(DriverRecoveryError):
                load_driver_recovery(self.folder)
            self.assertTrue(self.target.exists())

    def test_endpoint_bounds_and_controls(self):
        for value in ('', ' ', ' leading', 'trailing ', 'x' * 1025, 'a\0b', 'a\nb', 'a\x7fb', 'a\x85b', '\ud800'):
            with self.subTest(length=len(value)):
                with self.assertRaises(DriverRecoveryError):
                    DriverRouteRecovery(value, 'fallback', 'mic', 'mic-fallback')
        plan = DriverRouteRecovery('x' * 1024, 'fallback', 'mic', 'mic-fallback')
        save_driver_recovery(self.folder, plan)
        self.assertEqual(load_driver_recovery(self.folder), plan)

    def test_atomic_replace_failure_preserves_original_and_removes_temp(self):
        save_driver_recovery(self.folder, PLAN)
        original = self.target.read_bytes()
        replacement = DriverRouteRecovery('new-render', 'speaker', 'new-mic', 'mic')
        with patch('remontevoice.driver_route_recovery.os.replace', side_effect=OSError('raw path secrets')):
            with self.assertRaises(DriverRecoveryError) as error:
                save_driver_recovery(self.folder, replacement)
        self.assertNotIn('raw path secrets', str(error.exception))
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [self.target])

    def test_fsync_failure_preserves_original_and_removes_temp(self):
        save_driver_recovery(self.folder, PLAN)
        original = self.target.read_bytes()
        with patch('remontevoice.driver_route_recovery.os.fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(DriverRecoveryError):
                save_driver_recovery(self.folder, PLAN)
        self.assertEqual(self.target.read_bytes(), original)
        self.assertEqual(list(self.folder.iterdir()), [self.target])


if __name__ == '__main__':
    unittest.main()
