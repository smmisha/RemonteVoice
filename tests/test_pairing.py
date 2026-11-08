import json
import tempfile
import unittest
from pathlib import Path

from remotevoice.pairing import PairingAttemptLimiter, PairingManager


class PairingTests(unittest.TestCase):
    def test_pin_is_single_use_and_credential_persists(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "paired.json"
            manager = PairingManager(token_path)
            pin = manager.pairing_pin

            self.assertIsNotNone(pin)
            self.assertIsNone(manager.authenticate("incorrect"))
            token = manager.authenticate(pin)

            self.assertIsNotNone(token)
            self.assertIsNone(manager.pairing_pin)
            self.assertIsNone(manager.authenticate(pin))
            self.assertEqual(manager.authenticate(token), token)
            self.assertEqual(json.loads(token_path.read_text(encoding="utf-8"))["token"], token)

            restored = PairingManager(token_path)
            self.assertEqual(restored.authenticate(token), token)
            self.assertIsNone(restored.pairing_pin)

    def test_new_pairing_pin_replaces_previous_phone_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = PairingManager(Path(temporary) / "paired.json")
            old_token = manager.authenticate(manager.pairing_pin)
            manager.begin_pairing()
            new_token = manager.authenticate(manager.pairing_pin)

            self.assertNotEqual(old_token, new_token)
            self.assertIsNone(manager.authenticate(old_token))
            self.assertEqual(manager.authenticate(new_token), new_token)

    def test_pairing_attempts_are_limited_per_address(self):
        limiter = PairingAttemptLimiter(max_failures=2, window_seconds=60)
        self.assertTrue(limiter.allowed("192.168.1.20"))
        limiter.failed("192.168.1.20")
        self.assertTrue(limiter.allowed("192.168.1.20"))
        limiter.failed("192.168.1.20")
        self.assertFalse(limiter.allowed("192.168.1.20"))
        self.assertTrue(limiter.allowed("192.168.1.21"))
        limiter.succeeded("192.168.1.20")
        self.assertTrue(limiter.allowed("192.168.1.20"))

    def test_malformed_credentials_cannot_change_pairing(self):
        malformed = (None, True, 12345678, {}, [], b"12345678", "", "тест", "１2345678", "a" * 129, "abc\x00def")
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "paired.json"
            manager = PairingManager(token_path)
            old_token = manager.authenticate(manager.pairing_pin)
            pin = manager.begin_pairing()
            stored = token_path.read_bytes()
            for credential in malformed:
                with self.subTest(kind=type(credential).__name__, length=len(credential) if isinstance(credential, str) else None):
                    self.assertFalse(manager.is_valid_credential(credential))
                    self.assertIsNone(manager.authenticate(credential))
                    self.assertEqual(manager.pairing_pin, pin)
                    self.assertEqual(token_path.read_bytes(), stored)
                    self.assertEqual(manager.authenticate(old_token), old_token)

    def test_credential_preflight_does_not_consume_pin_or_write_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            token_path = Path(temporary) / "paired.json"
            manager = PairingManager(token_path)
            pin = manager.pairing_pin
            self.assertTrue(manager.is_valid_credential(pin))
            self.assertTrue(manager.is_valid_credential(pin))
            self.assertEqual(manager.pairing_pin, pin)
            self.assertFalse(token_path.exists())
            token = manager.authenticate(pin)
            stored = token_path.read_bytes()
            new_pin = manager.begin_pairing()
            self.assertTrue(manager.is_valid_credential(token))
            self.assertTrue(manager.is_valid_credential(new_pin))
            self.assertEqual(manager.pairing_pin, new_pin)
            self.assertEqual(token_path.read_bytes(), stored)

    def test_invalid_persisted_token_starts_fresh_pairing(self):
        for token in ("т" * 43, "a" * 129, "x " * 24):
            with self.subTest(token_length=len(token)), tempfile.TemporaryDirectory() as temporary:
                token_path = Path(temporary) / "paired.json"
                token_path.write_text(json.dumps({"token": token}), encoding="utf-8")
                manager = PairingManager(token_path)
                self.assertFalse(manager.is_paired)
                self.assertIsNotNone(manager.pairing_pin)


if __name__ == "__main__":
    unittest.main()
