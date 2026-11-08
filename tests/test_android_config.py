import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


class AndroidConfigTests(unittest.TestCase):
    def test_scanner_overrides_library_landscape_orientation(self):
        manifest = Path(__file__).resolve().parents[1] / "android/app/src/main/AndroidManifest.xml"
        root = ET.parse(manifest).getroot()
        android = "{http://schemas.android.com/apk/res/android}"
        tools = "{http://schemas.android.com/tools}"
        scanner = [activity for activity in root.findall("application/activity")
                   if activity.get(android + "name") == "com.journeyapps.barcodescanner.CaptureActivity"]
        self.assertEqual(len(scanner), 1)
        self.assertEqual(scanner[0].get(android + "screenOrientation"), "portrait")
        self.assertIn("android:screenOrientation", scanner[0].get(tools + "replace", ""))
