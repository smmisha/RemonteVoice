"""A select popup must not outlive its minimized or hidden owner window."""
import unittest
from tkinter import TclError

from remotevoice.desktop_widgets import RoundedSelect


class FakeOwner:
    def __init__(self, state='normal', viewable=True, fail=False):
        self._state = state
        self._viewable = viewable
        self._fail = fail

    def state(self):
        if self._fail:
            raise TclError('owner destroyed')
        return self._state

    def winfo_viewable(self):
        if self._fail:
            raise TclError('owner destroyed')
        return self._viewable


class SelectOwnerAvailabilityTests(unittest.TestCase):
    def test_normal_visible_owner_keeps_popup_available(self):
        self.assertTrue(RoundedSelect._owner_available(FakeOwner()))

    def test_iconified_owner_closes_popup(self):
        self.assertFalse(RoundedSelect._owner_available(FakeOwner(state='iconic')))

    def test_withdrawn_owner_closes_popup(self):
        self.assertFalse(RoundedSelect._owner_available(FakeOwner(state='withdrawn')))

    def test_unviewable_owner_closes_popup(self):
        self.assertFalse(RoundedSelect._owner_available(FakeOwner(viewable=False)))

    def test_destroyed_owner_closes_popup(self):
        self.assertFalse(RoundedSelect._owner_available(FakeOwner(fail=True)))


if __name__ == '__main__':
    unittest.main()
