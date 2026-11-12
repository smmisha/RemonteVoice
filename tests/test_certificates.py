import tempfile
import unittest
from pathlib import Path

from remontevoice.certificates import load_or_create_identity


class CertificateTests(unittest.TestCase):
    def test_identity_is_persistent_and_fingerprint_is_sha256_hex(self):
        with tempfile.TemporaryDirectory() as temporary:
            identity_dir = Path(temporary)
            _, first = load_or_create_identity(identity_dir)
            _, second = load_or_create_identity(identity_dir)

            self.assertEqual(first, second)
            self.assertEqual(len(first), 64)
            self.assertEqual(first, first.upper())


if __name__ == "__main__":
    unittest.main()
