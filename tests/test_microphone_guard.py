import json
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from remontevoice.microphone_guard import MicrophoneGuardError, MicrophoneMuteGuard, _Journal


class MemoryJournal:
    def __init__(self):
        self.saved = None
        self.locked = False

    def acquire(self):
        if self.locked:
            raise MicrophoneGuardError("another Host owns recovery")
        self.locked = True

    def release(self):
        self.locked = False

    def read(self):
        return self.saved

    def write(self, endpoint, previous):
        self.saved = (endpoint, previous)

    def clear(self):
        self.saved = None


class MicrophoneGuardTests(unittest.TestCase):
    def setUp(self):
        self.journal = MemoryJournal()
        factory = patch("remontevoice.microphone_guard._Journal", return_value=self.journal)
        self.addCleanup(factory.stop)
        factory.start()
        self.muted = False
        self.changes = []

    def get_mute(self, endpoint):
        self.assertEqual(endpoint, "physical-mic")
        return self.muted

    def set_mute(self, endpoint, muted):
        self.assertEqual(endpoint, "physical-mic")
        self.changes.append(muted)
        self.muted = muted

    def guard(self):
        return MicrophoneMuteGuard("physical-mic", journal_path=Path("unused.json"),
                                   get_mute=self.get_mute, set_mute=self.set_mute)

    def test_mutes_and_restores_original_enabled_state(self):
        guard = self.guard()
        guard.activate()
        self.assertTrue(self.muted)
        self.assertEqual(self.journal.saved, ("physical-mic", False))
        guard.restore()
        self.assertFalse(self.muted)
        self.assertEqual(self.changes, [True, False])
        self.assertIsNone(self.journal.saved)
        self.assertFalse(self.journal.locked)

    def test_preexisting_mute_is_never_unmuted(self):
        self.muted = True
        guard = self.guard()
        guard.activate()
        guard.restore()
        self.assertTrue(self.muted)
        self.assertEqual(self.changes, [])

    def test_repeated_status_does_not_capture_our_mute_as_original(self):
        guard = self.guard()
        guard.activate()
        guard.activate()
        guard.restore()
        guard.restore()
        self.assertEqual(self.changes, [True, False])

    def test_journal_is_saved_before_native_mutation(self):
        guard = self.guard()
        original = guard._set

        def checked_set(endpoint, muted):
            self.assertEqual(self.journal.saved, ("physical-mic", False))
            original(endpoint, muted)

        guard._set = checked_set
        guard.activate()
        guard.restore()

    def test_failed_mute_rolls_back_side_effect(self):
        guard = self.guard()

        def partial_set(endpoint, muted):
            self.set_mute(endpoint, muted)
            if muted:
                raise OSError("native error after mutation")

        guard._set = partial_set
        with self.assertRaises(MicrophoneGuardError):
            guard.activate()
        self.assertFalse(self.muted)
        self.assertIsNone(self.journal.saved)

    def test_unconfirmed_native_noop_is_failure(self):
        guard = self.guard()
        guard._set = lambda endpoint, muted: None
        with self.assertRaises(MicrophoneGuardError):
            guard.activate()
        self.assertFalse(self.muted)
        self.assertIsNone(self.journal.saved)

    def test_failed_restore_keeps_original_and_journal_for_retry(self):
        guard = self.guard()
        guard.activate()
        guard._set = MagicMock(side_effect=OSError("device unavailable"))
        with self.assertRaises(MicrophoneGuardError):
            guard.restore()
        self.assertEqual(self.journal.saved, ("physical-mic", False))
        self.assertTrue(self.journal.locked)
        guard._set = self.set_mute
        guard.restore()
        self.assertFalse(self.muted)

    def test_failed_journal_clear_after_unmute_blocks_reactivation_until_recovered(self):
        guard = self.guard()
        guard.activate()
        clear = self.journal.clear
        self.journal.clear = MagicMock(side_effect=OSError("journal is busy"))
        with self.assertRaises(MicrophoneGuardError):
            guard.restore()
        self.assertFalse(self.muted)
        self.assertEqual(self.journal.saved, ("physical-mic", False))
        with self.assertRaises(MicrophoneGuardError):
            guard.activate()
        self.assertFalse(self.muted)
        self.journal.clear = clear
        guard.restore()
        guard.activate()
        self.assertTrue(self.muted)
        guard.restore()

    def test_partial_unmute_failure_blocks_reactivation(self):
        guard = self.guard()
        guard.activate()

        def partial_unmute(endpoint, muted):
            self.set_mute(endpoint, muted)
            if not muted:
                raise OSError("native error after unmute")

        guard._set = partial_unmute
        with self.assertRaises(MicrophoneGuardError):
            guard.restore()
        self.assertFalse(self.muted)
        with self.assertRaises(MicrophoneGuardError):
            guard.activate()
        self.assertEqual(self.journal.saved, ("physical-mic", False))
        guard._set = self.set_mute
        guard.restore()

    def test_manual_unmute_is_preserved(self):
        guard = self.guard()
        guard.activate()
        self.muted = False
        guard.restore()
        self.assertEqual(self.changes, [True])

    def test_old_journal_is_not_overwritten_by_new_activation(self):
        self.journal.saved = ("other-mic", False)
        with self.assertRaises(MicrophoneGuardError):
            self.guard().activate()
        self.assertEqual(self.journal.saved, ("other-mic", False))
        self.assertEqual(self.changes, [])

    def test_recovery_does_not_change_another_running_hosts_microphone(self):
        guard = self.guard()
        guard.activate()
        with self.assertRaises(MicrophoneGuardError), patch(
            "remontevoice.microphone_guard.set_microphone_mute"
        ) as native_set:
            MicrophoneMuteGuard.recover(Path("unused.json"))
        native_set.assert_not_called()
        guard.restore()

    def test_recovery_after_simulated_process_exit_restores_journal(self):
        self.journal.saved = ("physical-mic", False)
        self.muted = True
        with patch("remontevoice.microphone_guard.get_microphone_mute", self.get_mute), patch(
            "remontevoice.microphone_guard.set_microphone_mute", self.set_mute
        ):
            MicrophoneMuteGuard.recover(Path("unused.json"))
        self.assertFalse(self.muted)
        self.assertIsNone(self.journal.saved)
        self.assertFalse(self.journal.locked)

    def test_recovery_failure_retains_journal_and_releases_lock(self):
        self.journal.saved = ("physical-mic", False)
        with patch("remontevoice.microphone_guard.get_microphone_mute", side_effect=OSError("gone")):
            with self.assertRaises(MicrophoneGuardError):
                MicrophoneMuteGuard.recover(Path("unused.json"))
        self.assertEqual(self.journal.saved, ("physical-mic", False))
        self.assertFalse(self.journal.locked)

    def test_failed_journal_write_never_changes_microphone(self):
        self.journal.write = MagicMock(side_effect=OSError("read only"))
        with self.assertRaises(MicrophoneGuardError):
            self.guard().activate()
        self.assertEqual(self.changes, [])
        self.assertFalse(self.journal.locked)


class JournalValidationTests(unittest.TestCase):
    def test_only_strict_endpoint_and_boolean_records_are_accepted(self):
        for data in (
            None, [], {"version": 1},
            {"version": True, "endpoint_id": "mic", "previous_mute": False},
            {"version": 1, "endpoint_id": "", "previous_mute": False},
            {"version": 1, "endpoint_id": "mic", "previous_mute": "false"},
            {"version": 1, "endpoint_id": "mic", "previous_mute": False, "extra": 1},
        ):
            with self.subTest(data=data), patch.object(Path, "read_text", return_value=json.dumps(data)):
                with self.assertRaises(MicrophoneGuardError):
                    _Journal(Path("unused.json")).read()

    def test_valid_record_is_returned(self):
        record = {"version": 1, "endpoint_id": "physical-mic", "previous_mute": False}
        with patch.object(Path, "read_text", return_value=json.dumps(record)):
            self.assertEqual(_Journal(Path("unused.json")).read(), ("physical-mic", False))


class JournalFileTests(unittest.TestCase):
    """Real application journal I/O and locking; no audio or COM calls."""

    def setUp(self):
        self.folder = Path(__file__).resolve().parents[1] / "android/.tools/guard-tests"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / (uuid.uuid4().hex + ".json")
        self.journals = []

    def tearDown(self):
        for journal in self.journals:
            journal.release()
        for path in (self.path, self.path.with_suffix(".json.tmp"), self.path.with_suffix(".json.lock")):
            self.assertEqual(path.resolve().parent, self.folder.resolve())
            path.unlink(missing_ok=True)

    def journal(self):
        journal = _Journal(self.path)
        self.journals.append(journal)
        return journal

    def test_atomic_record_roundtrip_and_clear(self):
        journal = self.journal()
        journal.acquire()
        self.assertIsNone(journal.read())
        journal.write("test-capture", False)
        self.assertEqual(journal.read(), ("test-capture", False))
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())
        journal.clear()
        self.assertIsNone(journal.read())

    def test_live_lease_blocks_second_owner_and_releases_on_close(self):
        first, second = self.journal(), self.journal()
        first.acquire()
        with self.assertRaises(MicrophoneGuardError):
            second.acquire()
        first.release()
        second.acquire()
        second.write("test-capture", True)
        self.assertEqual(second.read(), ("test-capture", True))


if __name__ == "__main__":
    unittest.main()
