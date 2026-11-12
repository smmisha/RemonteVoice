"""Window supervision for the Voicemeeter Banana mixer (hide it, reveal on request)."""
import unittest

from remontevoice import voicemeeter_window as vm


class SelectWindowTests(unittest.TestCase):
    def test_picks_the_mixer_window_among_decoys(self):
        rows = [
            (11, "chrome.exe", "Chrome_WidgetWin_1", True),
            (22, "voicemeeterpro.exe", "GDI+ Hook Window Class", True),
            (33, "voicemeeterpro.exe", "VBCABLE0Voicemeeter0MainWindow0", True),
            (44, "voicemeeterpro.exe", "IME", True),
        ]
        self.assertEqual(vm.select_window(rows), 33)

    def test_other_products_and_classes_are_ignored(self):
        rows = [
            (51, "voicemeeter.exe", "VBCABLE0Voicemeeter0MainWindow0", True),
            (52, "someapp.exe", "VBCABLE0Voicemeeter0MainWindow0", True),
            (53, "voicemeeterpro.exe", "NotOurWindow", True),
        ]
        # the standard Voicemeeter (voicemeeter.exe) is still the same engine family
        self.assertEqual(vm.select_window(rows), 51)
        self.assertEqual(vm.select_window([(52, "someapp.exe", "VBCABLE0Voicemeeter0MainWindow0", True)]), 0)
        self.assertEqual(vm.select_window([(53, "voicemeeterpro.exe", "NotOurWindow", True)]), 0)

    def test_no_mixer_returns_zero(self):
        self.assertEqual(vm.select_window([]), 0)


class FakeApi:
    def __init__(self, shown=True):
        self.calls = []
        self._shown = shown

    def show_window(self, hwnd, command):
        self.calls.append(("show", hwnd, command))
        return self._shown

    def foreground(self, hwnd):
        self.calls.append(("foreground", hwnd))
        return True


class HideShowTests(unittest.TestCase):
    def test_hide_sends_sw_hide(self):
        api = FakeApi()
        self.assertTrue(vm.hide(handle=77, api=api))
        self.assertEqual(api.calls, [("show", 77, vm.SW_HIDE)])

    def test_show_restores_and_raises_the_window(self):
        api = FakeApi()
        self.assertTrue(vm.show(handle=88, api=api))
        self.assertEqual(api.calls, [("show", 88, 9), ("foreground", 88)])

    def test_missing_handle_never_touches_the_desktop(self):
        api = FakeApi()
        self.assertFalse(vm.hide(handle=0, api=api))
        self.assertFalse(vm.show(handle=0, api=api))
        self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
