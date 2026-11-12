import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from remontevoice.deployment import build_identity, write_receipt
from remontevoice.host_app import main


class DeploymentTests(unittest.TestCase):
    def test_source_receipt_binds_actual_current_files_and_has_no_config(self):
        identity = build_identity()
        self.assertEqual(identity['kind'], 'source_snapshot')
        for entry in identity['files']:
            path = Path(entry['path'])
            self.assertEqual(entry['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertIn('host_lease.py', {Path(e['path']).name for e in identity['files']})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'runtime.json'
            write_receipt(path)
            receipt = json.loads(path.read_text('utf-8'))
            self.assertEqual(receipt['build']['sha256'], identity['sha256'])
            self.assertNotIn('config', receipt)
            self.assertNotIn('pairing', receipt)
            self.assertFalse(path.with_suffix('.json.tmp').exists())

    def test_packaged_receipt_hashes_executable(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'RemonteVoice.exe'
            executable.write_bytes(b'fake test executable')
            with patch('remontevoice.deployment.sys.frozen', True, create=True), patch(
                'remontevoice.deployment.sys.executable', str(executable)
            ):
                identity = build_identity()
            self.assertEqual(identity['kind'], 'executable')
            self.assertEqual(identity['sha256'], hashlib.sha256(executable.read_bytes()).hexdigest())

    def test_normal_launcher_has_opt_in_autostart_without_diagnostic_wrapper(self):
        app = MagicMock()
        with patch('remontevoice.host_app.HostWindow', return_value=app), patch(
            'remontevoice.host_app.logging.basicConfig'
        ):
            main([])
            app.after.assert_not_called()
            main(['--autostart'])
        app.after.assert_called_once_with(100, app._start_host)
        self.assertEqual(app.mainloop.call_count, 2)


if __name__ == '__main__':
    unittest.main()
