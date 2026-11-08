"""Source integration checks for the Android service startup transaction."""

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AndroidAudioReadyIntegrationTests(unittest.TestCase):
    def test_service_acknowledges_only_its_started_audio_before_publishing_connected(self):
        text = (ROOT / "android/app/src/main/java/dev/remotevoice/android/BridgeService.java").read_text(encoding="utf-8")
        authenticated = text.split("public void onAuthenticated(String token)", 1)[1].split("public void onAudio", 1)[0]
        self.assertLess(authenticated.index("sessionAudio.startPlayback();"), authenticated.index("active.markPlaybackReady()"))
        self.assertLess(authenticated.index("active.markPlaybackReady()"), authenticated.index("connected = true;"))
        self.assertIn("if (generation != session", authenticated)
        self.assertIn("close(", authenticated)


if __name__ == "__main__":
    unittest.main()
