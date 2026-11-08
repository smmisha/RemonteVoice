import unittest
from unittest.mock import patch

from remotevoice.windows_broker_bundle import WindowsProtectedBundle, ProtectedBundleError
from remotevoice.windows_broker_identity import _Ace, _FileInfo, _Security

ROOT = r'C:\Program Files\RemoteVoice'
EXE = ROOT + r'\RemoteVoiceBroker.exe'
DLL = ROOT + r'\_internal\python314.dll'
PINS = {EXE: 'a' * 64, DLL: 'b' * 64}
TRUSTED = _Security('S-1-5-18', True, (_Ace(0, 0, 0x10000000, 'S-1-5-32-544'),))


class Api:
    def __init__(self):
        self.opened = {}; self.closed = []; self.fail_close = False
        self.bad_path = self.writable = self.reparse = None
        self.wrong_hash = False
        self.process_path = EXE
        self.alive = True
    def installation_roots(self): return (r'C:\Program Files',)
    def open_image_component(self, path, *, directory):
        handle = len(self.opened) + 1
        self.opened[handle] = (path, directory)
        return handle
    def file_info(self, handle):
        path, directory = self.opened[handle]
        return _FileInfo(self.bad_path or path, directory, path == self.reparse)
    def file_security(self, handle):
        if self.opened[handle][0] == self.writable:
            return _Security('S-1-5-18', True, (_Ace(0, 0, 0x40000000, 'S-1-1-0'),))
        return TRUSTED
    def hash_image(self, handle):
        path = self.opened[handle][0]
        return '0' * 64 if self.wrong_hash else {p.casefold(): h for p, h in PINS.items()}[path]
    def close_handle(self, handle):
        if self.fail_close: raise OSError('sensitive path')
        self.closed.append(handle)
    def open_process(self, pid):
        self.opened[1000] = ('process', False)
        return 1000
    def process_image_path(self, handle): return self.process_path
    def process_alive(self, handle): return self.alive


class ProtectedBundleTests(unittest.TestCase):
    def test_inert_validation_requires_explicit_unique_canonical_pins(self):
        with patch('remotevoice.windows_broker_bundle._WindowsApi') as native:
            bundle = WindowsProtectedBundle(PINS)
            self.assertFalse(bundle.is_verified)
            self.assertTrue(bundle.cleanup_confirmed)
            bundle.close()
        native.assert_not_called()
        for pins in ({}, {EXE: 'A' * 64}, {'relative.exe': 'a' * 64}, {EXE: 'a'*64, EXE.lower(): 'a'*64}):
            with self.subTest(pins=list(pins)):
                with self.assertRaises(ValueError): WindowsProtectedBundle(pins)

    def test_runtime_and_executable_both_checked_and_handles_retained(self):
        api = Api(); bundle = WindowsProtectedBundle(PINS, api=api)
        bundle.verify()
        self.assertTrue(bundle.is_verified)
        self.assertFalse(bundle.cleanup_confirmed)
        self.assertEqual(api.closed, [])
        self.assertEqual({p for p, d in api.opened.values() if not d}, {p.lower() for p in PINS})
        bundle.close()
        self.assertTrue(bundle.cleanup_confirmed)
        self.assertFalse(bundle.is_verified)
        self.assertEqual(len(api.closed), len(api.opened))
        with self.assertRaises(ProtectedBundleError): bundle.verify()

    def test_writable_ancestor_runtime_reparse_and_wrong_hash_rejected(self):
        for option, value in [('writable', ROOT.lower()), ('reparse', DLL.lower()), ('wrong_hash', True), ('bad_path', r'C:\alias')]:
            api = Api(); setattr(api, option, value)
            bundle = WindowsProtectedBundle(PINS, api=api)
            with self.subTest(option=option):
                with self.assertRaisesRegex(ProtectedBundleError, '^Protected installation rejected$'): bundle.verify()
                self.assertFalse(bundle.is_verified)
                self.assertTrue(bundle.cleanup_confirmed)

    def test_unprotected_workspace_is_rejected_before_opening_files(self):
        api = Api(); bundle = WindowsProtectedBundle({r'C:\Users\User\broker.exe': 'a'*64}, api=api)
        with self.assertRaises(ProtectedBundleError): bundle.verify()
        self.assertEqual(api.opened, {})

    def test_partial_failure_with_unconfirmed_cleanup_keeps_owner_for_retry(self):
        api = Api(); api.wrong_hash = api.fail_close = True
        bundle = WindowsProtectedBundle(PINS, api=api)
        with self.assertRaises(ProtectedBundleError) as raised: bundle.verify()
        self.assertIs(raised.exception.cleanup_owner, bundle)
        self.assertFalse(bundle.cleanup_confirmed)
        self.assertFalse(bundle.is_verified)
        api.fail_close = False
        bundle.close()
        self.assertTrue(bundle.cleanup_confirmed)

    def test_failed_close_revokes_verification_and_can_retry(self):
        api = Api(); bundle = WindowsProtectedBundle(PINS, api=api); bundle.verify()
        api.fail_close = True
        with self.assertRaises(ProtectedBundleError): bundle.close()
        self.assertFalse(bundle.is_verified)
        self.assertFalse(bundle.cleanup_confirmed)
        api.fail_close = False; bundle.close()
        self.assertTrue(bundle.cleanup_confirmed)

    def test_running_executable_must_match_pinned_image_and_live_process(self):
        for path, alive in ((EXE, True), (r'C:\Python314\python.exe', True), (EXE, False)):
            api=Api();api.process_path=path;api.alive=alive
            bundle=WindowsProtectedBundle(PINS,api=api);bundle.verify()
            if path == EXE and alive:
                bundle.verify_current_executable(EXE)
                self.assertTrue(bundle.is_verified)
            else:
                with self.assertRaises(ProtectedBundleError): bundle.verify_current_executable(EXE)
                self.assertFalse(bundle.is_verified)
            bundle.close(); self.assertTrue(bundle.cleanup_confirmed)
