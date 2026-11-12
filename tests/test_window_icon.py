"""The Windows window icon must resolve from the bundle, then the source tree."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from remotevoice import host_app


class WindowIconPathTests(unittest.TestCase):
    def test_source_tree_icon_is_available(self):
        icon = host_app.window_icon_path()
        self.assertIsNotNone(icon)
        self.assertEqual(icon.name, "RemoteVoice.ico")
        self.assertTrue(icon.is_file())

    def test_bundled_icon_wins_over_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundled = Path(temporary) / "RemoteVoice.ico"
            bundled.write_bytes(b"ico")
            with patch.object(host_app.sys, "_MEIPASS", temporary, create=True):
                self.assertEqual(host_app.window_icon_path(), bundled)

    def test_empty_bundle_falls_back_to_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(host_app.sys, "_MEIPASS", temporary, create=True):
                icon = host_app.window_icon_path()
        self.assertIsNotNone(icon)
        self.assertEqual(icon.name, "RemoteVoice.ico")
        self.assertTrue(icon.is_file())


if __name__ == '__main__':
    unittest.main()
