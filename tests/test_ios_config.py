import plistlib
import unittest
from pathlib import Path


class IOSConfigTests(unittest.TestCase):
    def test_plist_declares_audio_permissions_background_mode_and_pairing_scheme(self):
        plist_path = Path(__file__).parents[1] / "ios" / "RemoteVoice" / "Info.plist"
        with plist_path.open("rb") as source:
            info = plistlib.load(source)

        self.assertTrue(info["NSMicrophoneUsageDescription"])
        self.assertTrue(info["NSLocalNetworkUsageDescription"])
        self.assertIn("audio", info["UIBackgroundModes"])
        schemes = {
            scheme
            for url_type in info["CFBundleURLTypes"]
            for scheme in url_type["CFBundleURLSchemes"]
        }
        self.assertIn("remotevoice", schemes)


if __name__ == "__main__":
    unittest.main()
