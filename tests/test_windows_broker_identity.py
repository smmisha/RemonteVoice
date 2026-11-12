import ctypes as ct
import hashlib
import ntpath
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

from remontevoice.mic_broker import PeerIdentity
from remontevoice.windows_broker_identity import (
    BrokerIdentityFatalError, PeerVerificationError, WindowsPeerVerifier,
    _Ace, _FileInfo, _Security, _TokenIdentity, _WindowsApi,
    _SidAndAttributes, _TokenGroups, _protected, read_current_process_token,
)


USER = "S-1-5-21-1000"
LOGON = "S-1-5-5-1-42"
TOKEN = _TokenIdentity(USER, LOGON, 2)
IMAGE = r"C:\Program Files\RemonteVoice\RemonteVoiceHost.exe"
DIGEST = "a" * 64
PIPE = 0x100000071
TRUSTED = _Security("S-1-5-18", True, (_Ace(0, 0, 0x10000000, "S-1-5-32-544"),))


class FakeApi:
    def __init__(self):
        self.calls = []
        self.peer_pid = 712
        self.token = self.thread_token = TOKEN
        self.image = IMAGE
        self.digest = DIGEST
        self.info_override = {}
        self.security_override = {}
        self.failure = None
        self.revert_ok = True
        self.alive = True
        self.next_handle = 0x100000100
        self.opened = {}
        self.closed = []
        self.close_fail = set()

    def call(self, operation, *args):
        self.calls.append((operation, *args))
        if self.failure == operation:
            raise OSError("secret-path-and-SID-must-not-escape")

    def pipe_peer_pid(self, handle, *, server_side):
        self.call("pid", handle, server_side)
        return self.peer_pid

    def open_process(self, pid):
        self.call("process", pid)
        self.opened[0x100000011] = "process"
        return 0x100000011

    def process_token_identity(self, process):
        self.call("process_token", process)
        return self.token

    def impersonate_pipe_client(self, pipe):
        self.call("impersonate", pipe)

    def thread_token_identity(self):
        self.call("thread_token")
        return self.thread_token

    def revert_to_self(self):
        self.call("revert")
        return self.revert_ok

    def process_image_path(self, process):
        self.call("image", process)
        return self.image

    def installation_roots(self):
        self.call("roots")
        return (r"C:\Windows", r"C:\Program Files")

    def open_image_component(self, path, *, directory):
        self.call("component", path, directory)
        self.next_handle += 1
        self.opened[self.next_handle] = path
        return self.next_handle

    def file_info(self, handle):
        self.call("info", handle)
        path = self.opened[handle]
        return self.info_override.get(path, _FileInfo(path, path != IMAGE.casefold(), False))

    def file_security(self, handle):
        self.call("security", handle)
        return self.security_override.get(self.opened[handle], TRUSTED)

    def hash_image(self, handle):
        self.call("hash", handle)
        self.assert_image_locked = handle in self.opened and handle not in self.closed
        return self.digest

    def process_alive(self, process):
        self.call("alive", process)
        return self.alive

    def close_handle(self, handle):
        self.calls.append(("close", handle))
        if handle in self.close_fail:
            raise OSError("cleanup-secret")
        self.closed.append(handle)


def verifier(api=None, **overrides):
    arguments = dict(expected_user_sid=USER, expected_logon_sid=LOGON,
                     expected_session_id=2, trusted_images={IMAGE: DIGEST}, api=api)
    arguments.update(overrides)
    return WindowsPeerVerifier(**arguments)


class PeerPolicyTests(unittest.TestCase):
    def test_constructor_is_inert_and_requires_explicit_valid_policy(self):
        with patch("remontevoice.windows_broker_identity._WindowsApi") as native:
            value = verifier()
            value.close()
        native.assert_not_called()
        for arguments in (
            {"trusted_images": {}},
            {"trusted_images": {r"relative.exe": DIGEST}},
            {"trusted_images": {r"\\server\share\host.exe": DIGEST}},
            {"trusted_images": {r"C:\apps\..\Host.exe": DIGEST}},
            {"trusted_images": {r"C:\apps\Host.exe:alternate": DIGEST}},
            {"trusted_images": {r"C:\apps\Host.exe ": DIGEST}},
            {"trusted_images": {IMAGE: "A" * 64}},
            {"expected_logon_sid": USER},
            {"expected_session_id": True},
        ):
            with self.subTest(arguments=arguments), self.assertRaises((ValueError, TypeError)):
                verifier(FakeApi(), **arguments)

    def test_native_observations_are_bound_and_resources_remain_until_disconnect(self):
        api = FakeApi()
        value = verifier(api)
        peer = value.verify_pipe_peer(PIPE, server_side=True)
        self.assertEqual(peer, PeerIdentity(USER, LOGON, 2, DIGEST))
        self.assertTrue(api.assert_image_locked)
        self.assertFalse(api.closed)
        operations = [call[0] for call in api.calls]
        self.assertLess(operations.index("revert"), operations.index("image"))
        self.assertEqual(api.calls[0], ("pid", PIPE, True))
        handles = list(api.opened)
        value.forget_pipe_peer(PIPE)
        self.assertEqual(api.closed, list(reversed(handles)))
        value.close()
        self.assertEqual(len(api.closed), len(handles))

    def test_client_side_uses_server_pid_and_never_impersonates(self):
        api = FakeApi()
        value = verifier(api)
        value.verify_pipe_peer(PIPE, server_side=False)
        self.assertEqual(api.calls[0], ("pid", PIPE, False))
        self.assertNotIn("impersonate", [call[0] for call in api.calls])
        self.assertNotIn("revert", [call[0] for call in api.calls])
        value.close()

    def test_identity_mismatches_and_last_read_impersonation_cannot_admit(self):
        for identity in (replace(TOKEN, user_sid="S-1-5-21-1001"),
                         replace(TOKEN, logon_sid="S-1-5-5-1-43"),
                         replace(TOKEN, session_id=3), None):
            for attribute in ("token", "thread_token"):
                with self.subTest(identity=identity, attribute=attribute):
                    api = FakeApi()
                    setattr(api, attribute, identity)
                    value = verifier(api)
                    with self.assertRaises(PeerVerificationError):
                        value.verify_pipe_peer(PIPE, server_side=True)
                    self.assertEqual([c[0] for c in api.calls].count("revert"), 1)
                    self.assertEqual(set(api.closed), set(api.opened))
                    self.assertNotIn("hash", [c[0] for c in api.calls])

    def test_native_failures_revert_close_and_do_not_expose_sid_or_path(self):
        for operation in ("pid", "process", "process_token", "impersonate",
                          "thread_token", "image", "roots", "component", "info", "security", "hash", "alive"):
            with self.subTest(operation=operation):
                api = FakeApi()
                api.failure = operation
                with self.assertRaises(PeerVerificationError) as raised:
                    verifier(api).verify_pipe_peer(PIPE, server_side=True)
                self.assertEqual(str(raised.exception), "Peer authentication rejected")
                self.assertIsNone(raised.exception.__cause__)
                self.assertEqual([c[0] for c in api.calls].count("revert"), 1)
                self.assertEqual(set(api.closed), set(api.opened))

    def test_reversion_failure_is_fatal_and_permanently_poisons_verifier(self):
        for failure in (None, "process", "impersonate", "thread_token"):
            with self.subTest(failure=failure):
                api = FakeApi()
                api.failure, api.revert_ok = failure, False
                value = verifier(api)
                with self.assertRaises(BrokerIdentityFatalError):
                    value.verify_pipe_peer(PIPE, server_side=True)
                self.assertEqual(set(api.closed), set(api.opened))
                api.failure, api.revert_ok = None, True
                with self.assertRaises(BrokerIdentityFatalError):
                    value.verify_pipe_peer(PIPE, server_side=True)

    def test_fatal_reversion_is_not_hidden_by_a_second_handle_cleanup_failure(self):
        api = FakeApi()
        api.revert_ok = False
        api.close_fail.add(0x100000011)
        value = verifier(api)
        with self.assertRaises(BrokerIdentityFatalError):
            value.verify_pipe_peer(PIPE, server_side=True)
        api.close_fail.clear()
        value.close()

    def test_image_hash_path_installation_root_and_case_policy(self):
        for path, digest in ((IMAGE, "b" * 64), (r"C:\Program Files\Other\Host.exe", DIGEST),
                             (r"C:\Users\User\Host.exe", DIGEST), (r"\\?\C:\Program Files\RemonteVoice\RemonteVoiceHost.exe", DIGEST)):
            with self.subTest(path=path):
                api = FakeApi()
                api.image, api.digest = path, digest
                pins = {path: DIGEST} if path.startswith(r"C:\Users") else {IMAGE: DIGEST}
                with self.assertRaises(PeerVerificationError):
                    verifier(api, trusted_images=pins).verify_pipe_peer(PIPE, server_side=True)
                self.assertEqual(set(api.closed), set(api.opened))
        api = FakeApi()
        api.image = IMAGE.upper()
        value = verifier(api)
        self.assertEqual(value.verify_pipe_peer(PIPE, server_side=True).image_sha256, DIGEST)
        value.close()

    def test_every_component_is_checked_for_reparse_wrong_canonical_path_and_acl(self):
        paths = ("c:\\", r"c:\program files", r"c:\program files\remontevoice", IMAGE.casefold())
        for path in paths:
            for problem in ("reparse", "path", "owner", "dacl"):
                with self.subTest(path=path, problem=problem):
                    api = FakeApi()
                    if problem in ("reparse", "path"):
                        api.info_override[path] = _FileInfo(path if problem == "reparse" else r"C:\Other",
                                                           path != IMAGE.casefold(), problem == "reparse")
                    else:
                        api.security_override[path] = (_Security(USER, True, ()) if problem == "owner"
                                                       else _Security("S-1-5-18", False, ()))
                    with self.assertRaises(PeerVerificationError):
                        verifier(api).verify_pipe_peer(PIPE, server_side=True)
                    self.assertNotIn("hash", [call[0] for call in api.calls])
                    self.assertEqual(set(api.closed), set(api.opened))

    def test_fast_validation_requires_initial_image_binding_and_rechecks_native_tokens(self):
        api = FakeApi()
        value = verifier(api)
        expected = PeerIdentity(USER, LOGON, 2, DIGEST)
        self.assertFalse(value.validate_pipe_token(PIPE, expected))
        peer = value.verify_pipe_peer(PIPE, server_side=True)
        api.calls.clear()
        self.assertTrue(value.validate_pipe_token(PIPE, peer, server_side=True))
        self.assertNotIn("hash", [call[0] for call in api.calls])
        self.assertNotIn("process", [call[0] for call in api.calls])
        self.assertIn(("process_token", 0x100000011), api.calls)
        self.assertEqual([call[0] for call in api.calls].count("revert"), 1)
        api.thread_token = replace(TOKEN, logon_sid="S-1-5-5-1-99")
        self.assertFalse(value.validate_pipe_token(PIPE, peer))
        api.thread_token = TOKEN
        api.peer_pid += 1
        self.assertFalse(value.validate_pipe_token(PIPE, peer))
        api.peer_pid -= 1
        api.alive = False
        self.assertFalse(value.validate_pipe_token(PIPE, peer))
        value.forget_pipe_peer(PIPE)
        self.assertFalse(value.validate_pipe_token(PIPE, peer))

    def test_pid_change_during_initial_verification_rejects_even_same_token(self):
        api = FakeApi()
        original_hash = api.hash_image
        def hash_then_change(handle):
            digest = original_hash(handle)
            api.peer_pid += 1
            return digest
        api.hash_image = hash_then_change
        with self.assertRaises(PeerVerificationError):
            verifier(api).verify_pipe_peer(PIPE, server_side=True)
        self.assertEqual(set(api.closed), set(api.opened))

    def test_close_attempts_all_handles_and_retains_failed_cleanup_for_retry(self):
        api = FakeApi()
        value = verifier(api)
        value.verify_pipe_peer(PIPE, server_side=True)
        failed = max(api.opened)
        api.close_fail.add(failed)
        with self.assertRaises(PeerVerificationError):
            value.forget_pipe_peer(PIPE)
        self.assertEqual(set(api.closed), set(api.opened) - {failed})
        api.close_fail.clear()
        value.close()
        self.assertEqual(set(api.closed), set(api.opened))

    def test_invalid_handles_and_wrong_binding_side_are_rejected(self):
        api = FakeApi()
        value = verifier(api)
        for handle in (None, True, 0, -1, (1 << (ct.sizeof(ct.c_void_p) * 8)) - 1, 1 << 65):
            with self.subTest(handle=handle), self.assertRaises(PeerVerificationError):
                value.verify_pipe_peer(handle, server_side=True)
        peer = value.verify_pipe_peer(PIPE, server_side=True)
        self.assertFalse(value.validate_pipe_token(PIPE, peer, server_side=False))
        self.assertFalse(value.validate_pipe_token(PIPE, replace(peer, image_sha256="b" * 64)))
        value.close()


class ProtectedImageAclTests(unittest.TestCase):
    def test_all_non_admin_mutation_rights_are_rejected_including_inherited_aces(self):
        for mask in (0x2, 0x4, 0x10, 0x40, 0x100, 0x10000, 0x40000, 0x80000,
                     0x10000000, 0x40000000):
            for flags in (0, 0x10):
                with self.subTest(mask=mask, flags=flags):
                    self.assertFalse(_protected(_Security("S-1-5-18", True, (_Ace(0, flags, mask, USER),))))

    def test_deny_does_not_cancel_untrusted_allow_and_unknown_ace_fails_closed(self):
        self.assertFalse(_protected(_Security("S-1-5-18", True,
                                             (_Ace(1, 0, 0x2, USER), _Ace(0, 0, 0x2, USER)))))
        for kind in (5, 9, 99):
            self.assertFalse(_protected(_Security("S-1-5-18", True, (_Ace(kind, 0, 0, ""),))))
        self.assertFalse(_protected(_Security(USER, True, ())))
        self.assertFalse(_protected(_Security("S-1-5-18", False, ())))

    def test_specific_add_subdirectory_is_allowed_only_on_independently_checked_ancestors(self):
        for mask in (0x4, 0x100004, 0x1200AD):
            security = _Security("S-1-5-18", True, (_Ace(0, 0, mask, USER),))
            self.assertTrue(_protected(security, ancestor=True))
            self.assertFalse(_protected(security))
        for mask in (0x6, 0x44, 0x104, 0x10004, 0x40004, 0x80004, 0x40000004, 0x10000004):
            with self.subTest(mask=mask):
                security = _Security("S-1-5-18", True, (_Ace(0, 0, mask, USER),))
                self.assertFalse(_protected(security, ancestor=True))

    def test_directory_add_subdir_exception_does_not_skip_next_component_or_leaf_checks(self):
        for bad_component in (r"c:\program files", IMAGE.casefold()):
            api = FakeApi()
            api.security_override["c:\\"] = _Security("S-1-5-18", True, (_Ace(0, 0, 0x100004, USER),))
            api.security_override[bad_component] = (
                _Security(USER, True, ()) if bad_component != IMAGE.casefold()
                else _Security("S-1-5-18", True, (_Ace(0, 0, 0x4, USER),)))
            with self.subTest(component=bad_component), self.assertRaises(PeerVerificationError):
                verifier(api).verify_pipe_peer(PIPE, server_side=True)
            self.assertNotIn("hash", [call[0] for call in api.calls])

    def test_known_trusted_mutation_and_nonmutating_user_aces_are_allowed(self):
        for owner in ("S-1-5-18", "S-1-5-32-544",
                      "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"):
            self.assertTrue(_protected(_Security(owner, True, (_Ace(0, 0, 0x10000000, owner),
                                                                _Ace(0, 0, 0xA0120089, USER)))))
        self.assertTrue(_protected(_Security("S-1-5-18", True, (_Ace(0, 0x08, 0x2, USER),))))


class NativeAdapterLayoutTests(unittest.TestCase):
    def test_token_groups_uses_native_pointer_alignment(self):
        self.assertEqual(_TokenGroups.groups.offset % ct.alignment(ct.c_void_p), 0)
        self.assertEqual(ct.sizeof(_SidAndAttributes), 16 if ct.sizeof(ct.c_void_p) == 8 else 8)

    def test_image_open_denies_write_delete_sharing_and_does_not_follow_final_reparse(self):
        from unittest.mock import MagicMock
        api = object.__new__(_WindowsApi)
        api.kernel = MagicMock()
        api.kernel.CreateFileW.return_value = 0x100000010
        api.open_image_component(IMAGE, directory=False)
        args = api.kernel.CreateFileW.call_args.args
        self.assertEqual(args[2], 1)
        self.assertTrue(args[5] & 0x00200000)
        self.assertFalse(args[5] & 0x02000000)
        api.open_image_component(r"C:\Program Files", directory=True)
        args = api.kernel.CreateFileW.call_args.args
        self.assertEqual(args[2] & 4, 0)
        self.assertTrue(args[5] & 0x02000000)


@unittest.skipUnless(os.name == "nt", "Read-only Windows token smoke")
class WindowsNativeSmokeTests(unittest.TestCase):
    def test_current_process_token_is_observed_without_pipe_or_microphone(self):
        token = read_current_process_token()
        self.assertIs(type(token), _TokenIdentity)
        self.assertRegex(token.user_sid, r"^S-\d+(?:-\d+)+$")
        self.assertRegex(token.logon_sid, r"^S-1-5-5-\d+-\d+$")
        self.assertIs(type(token.session_id), int)
        self.assertNotIn(token.user_sid, repr(token))

    def test_native_image_handle_hash_and_acl_read_are_real_and_do_not_admit_workspace_interpreter(self):
        api = _WindowsApi()
        process = api.open_process(os.getpid())
        image_handle = None
        try:
            path = api.process_image_path(process)
            image_handle = api.open_image_component(path, directory=False)
            info = api.file_info(image_handle)
            security = api.file_security(image_handle)
            self.assertFalse(info.directory)
            self.assertFalse(info.reparse)
            digest = api.hash_image(image_handle)
            with open(path, "rb") as source:
                self.assertEqual(digest, hashlib.file_digest(source, "sha256").hexdigest())
            roots = [root.casefold() + "\\" for root in api.installation_roots()]
            if not any(path.casefold().startswith(root) for root in roots):
                # A protected interpreter file can still live outside the
                # allowed install roots; its file ACL alone is not admission.
                self.assertIs(type(security), _Security)
                token = api.process_token_identity(process)
                value = WindowsPeerVerifier(token.user_sid, token.logon_sid, token.session_id, {path: digest}, api=api)
                handles = []
                with self.assertRaises(PeerVerificationError):
                    value._verify_image(api, process, handles)
                self.assertFalse(handles)
        finally:
            if image_handle is not None:
                api.close_handle(image_handle)
            api.close_handle(process)

    def test_native_volume_root_acl_is_checked_with_ancestor_specific_rights(self):
        api = _WindowsApi()
        handle = api.open_image_component("C:\\", directory=True)
        try:
            info = api.file_info(handle)
            security = api.file_security(handle)
            self.assertTrue(info.directory)
            self.assertFalse(info.reparse)
            # Actual root policies vary. The exception may change the answer
            # only when non-trusted mutation grants contain add-subdirectory.
            if _protected(security, ancestor=True) and not _protected(security):
                from remontevoice.windows_broker_identity import _MUTATION, _TRUSTED_OWNERS
                grants = [ace for ace in security.aces if ace.kind == 0 and not ace.flags & 8
                          and ace.sid not in _TRUSTED_OWNERS and ace.mask & _MUTATION]
                self.assertTrue(grants)
                self.assertTrue(all(ace.mask & _MUTATION == 0x4 for ace in grants))
        finally:
            api.close_handle(handle)

    def test_native_protected_windows_file_acl_and_hash_without_launching_a_process(self):
        api = _WindowsApi()
        path = ntpath.join(api.installation_roots()[0], "System32", "cmd.exe")
        handle = api.open_image_component(path, directory=False)
        try:
            digest = api.hash_image(handle)
        finally:
            api.close_handle(handle)
        value = verifier(api, trusted_images={path: digest})
        handles = []
        try:
            # Only the process-to-path observation is substituted. Every file,
            # ancestor ACL, canonical path, reparse and hash observation is real.
            # This is file-policy acceptance, not process/pipe authentication.
            with patch.object(api, "process_image_path", return_value=path):
                self.assertEqual(value._verify_image(api, 0, handles), digest)
            self.assertGreaterEqual(len(handles), 4)
        finally:
            value._close_handles(api, handles)


if __name__ == "__main__":
    unittest.main()
