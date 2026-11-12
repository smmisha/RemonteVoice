import ctypes as ct
import os
import unittest
from dataclasses import FrozenInstanceError, replace
from unittest.mock import patch

from remontevoice.windows_broker_accounts import (
    AccountIdentity, BrokerAccountPair, BrokerAccountError, observe_broker_accounts, _AccountApi,
)

FULL = AccountIdentity("S-1-5-21-1000", "S-1-5-5-1-42", 2, 2, True, True, False)
LIMITED = AccountIdentity(FULL.user_sid, "S-1-5-5-1-43", 2, 3, False, False, True)


class Api:
    def __init__(self, current=FULL, linked=LIMITED):
        self.current, self.linked = current, linked
        self.closed = []
        self.calls = []
        self.failure = None
        self.close_fail = set()

    def open_current_token(self):
        self.calls.append("current")
        if self.failure == "current":
            raise OSError("secret-native-details")
        return 0x100000001

    def linked_token(self, token):
        self.calls.append("linked")
        if self.failure == "linked":
            raise OSError("secret-native-details")
        return 0x100000002

    def read_account(self, token):
        self.calls.append(("read", token))
        if self.failure == "read":
            raise OSError("secret-native-details")
        return self.current if token == 0x100000001 else self.linked

    def close_handle(self, token):
        self.closed.append(token)
        if token in self.close_fail:
            raise OSError("secret-cleanup-details")


class BrokerAccountTests(unittest.TestCase):
    def test_current_full_and_current_filtered_produce_explicit_same_pair(self):
        for current, linked, current_broker in ((FULL, LIMITED, True), (LIMITED, FULL, False)):
            api = Api(current, linked)
            result = observe_broker_accounts(api=api)
            self.assertEqual(result, BrokerAccountPair(FULL, LIMITED, current_broker))
            self.assertEqual(api.closed, [0x100000002, 0x100000001])
            self.assertEqual(result.broker.logon_sid, FULL.logon_sid)
            self.assertEqual(result.client.logon_sid, LIMITED.logon_sid)
            self.assertNotIn(FULL.user_sid, repr(result))
            with self.assertRaises(FrozenInstanceError):
                result.current_is_broker = False

    def test_equal_observed_logon_sids_are_preserved_without_normalizing_different_ones(self):
        linked = replace(LIMITED, logon_sid=FULL.logon_sid)
        result = observe_broker_accounts(api=Api(FULL, linked))
        self.assertEqual(result.client.logon_sid, result.broker.logon_sid)
        self.assertIs(result.client, linked)

    def test_different_user_session_system_and_session_zero_are_rejected(self):
        for linked in (replace(LIMITED, user_sid="S-1-5-21-1001"),
                       replace(LIMITED, session_id=3),
                       replace(LIMITED, user_sid="S-1-5-18")):
            api = Api(FULL, linked)
            with self.subTest(linked=linked), self.assertRaises(BrokerAccountError):
                observe_broker_accounts(api=api)
            self.assertEqual(len(api.closed), 2)
        for user in ("S-1-5-18", "S-1-5-19", "S-1-5-20"):
            with self.subTest(user=user), self.assertRaises(BrokerAccountError):
                observe_broker_accounts(api=Api(replace(FULL, user_sid=user), replace(LIMITED, user_sid=user)))
        with self.assertRaises(BrokerAccountError):
            observe_broker_accounts(api=Api(replace(FULL, session_id=0), replace(LIMITED, session_id=0)))

    def test_default_unfiltered_admin_standard_and_inconsistent_roles_are_unsupported(self):
        pairs = (
            (replace(FULL, elevation_type=1), LIMITED),
            (replace(LIMITED, elevation_type=1, administrator_deny_only=False), FULL),
            (FULL, FULL), (LIMITED, LIMITED),
            (replace(FULL, elevated=False), LIMITED),
            (replace(FULL, administrator_enabled=False), LIMITED),
            (FULL, replace(LIMITED, elevated=True)),
            (FULL, replace(LIMITED, administrator_deny_only=False)),
        )
        for current, linked in pairs:
            with self.subTest(pair=(current, linked)), self.assertRaises(BrokerAccountError):
                observe_broker_accounts(api=Api(current, linked))

    def test_no_link_and_native_failure_close_every_obtained_token_and_sanitize(self):
        for failure, count in (("current", 0), ("linked", 1), ("read", 2)):
            api = Api()
            api.failure = failure
            with self.subTest(failure=failure), self.assertRaises(BrokerAccountError) as raised:
                observe_broker_accounts(api=api)
            self.assertEqual(len(api.closed), count)
            self.assertNotIn("secret", str(raised.exception))
        for malformed in (None, {}, LIMITED.__dict__, "claims"):
            with self.subTest(malformed=malformed), self.assertRaises(BrokerAccountError):
                observe_broker_accounts(api=Api(FULL, malformed))

    def test_cleanup_failure_retains_retry_owner_and_attempts_both_handles(self):
        api = Api()
        api.close_fail.add(0x100000002)
        with self.assertRaises(BrokerAccountError) as raised:
            observe_broker_accounts(api=api)
        owner = raised.exception.cleanup_owner
        self.assertIs(owner.cleanup_confirmed, False)
        self.assertEqual(api.closed, [0x100000002, 0x100000001])
        self.assertEqual(owner.handles, [0x100000002])
        api.close_fail.clear()
        owner.close()
        self.assertIs(owner.cleanup_confirmed, True)
        self.assertFalse(owner.handles)
        self.assertEqual(api.closed[-1], 0x100000002)
        calls = list(api.closed)
        # The installed client's partial-owner wrapper requires explicit True
        # after retry; repeated observations and close issue no further native I/O.
        self.assertIs(owner.cleanup_confirmed, True)
        owner.close()
        self.assertEqual(api.closed, calls)

    def test_fatal_observation_survives_cleanup_failure(self):
        class Fatal(BaseException):
            pass
        fatal, api = Fatal(), Api()
        def read(token):
            raise fatal
        api.read_account = read
        api.close_fail.add(0x100000001)
        with self.assertRaises(Fatal) as raised:
            observe_broker_accounts(api=api)
        self.assertIs(raised.exception, fatal)
        self.assertEqual(len(api.closed), 2)
        self.assertEqual(fatal.cleanup_owner.handles, [0x100000001])
        api.close_fail.clear()
        fatal.cleanup_owner.close()

    def test_metadata_types_are_strict(self):
        for changes in ({"session_id": True}, {"elevation_type": True}, {"elevated": 1},
                        {"administrator_enabled": "yes"}, {"administrator_deny_only": 0},
                        {"logon_sid": "S-1-5-18"}, {"administrator_deny_only": True}):
            with self.subTest(changes=changes), self.assertRaises((TypeError, ValueError)):
                replace(FULL, **changes)

    def test_native_library_loading_is_lazy(self):
        with patch("remontevoice.windows_broker_accounts._AccountApi") as native:
            observe_broker_accounts(api=Api())
        native.assert_not_called()


@unittest.skipUnless(os.name == "nt", "Read-only Windows token observation")
class NativeAccountTests(unittest.TestCase):
    def test_actual_current_token_metadata_no_launch_or_elevation(self):
        api = _AccountApi()
        token = api.open_current_token()
        try:
            actual = api.read_account(token)
            self.assertIs(type(actual), AccountIdentity)
            self.assertNotIn(actual.user_sid, repr(actual))
            self.assertIn(actual.elevation_type, (1, 2, 3))
        finally:
            api.close_handle(token)


if __name__ == "__main__":
    unittest.main()
