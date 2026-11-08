import unittest
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock, patch
from remotevoice.audio_backend import AudioBackendError

import test_host_output_routing as fixtures
from test_host_driver_mode import controller


class ForcedClose(BaseException):
    pass


class HostCloseLifecycleTests(fixtures.HeadlessHostTests):
    def test_final_sdk_close_happens_after_route_restore_and_before_window_destroy(self):
        window = fixtures.headless_window()
        events = []
        window.output_route.restore.side_effect = lambda: events.append('restore')
        window.destroy.side_effect = lambda: events.append('destroy')
        with patch('remotevoice.host_app.close_audio_backend', side_effect=lambda: events.append('sdk_close')) as close:
            window._on_close()
        close.assert_called_once_with()
        self.assertIn('restore', events)
        self.assertLess(events.index('restore'), events.index('sdk_close'))
        self.assertLess(events.index('sdk_close'), events.index('destroy'))
        window._host_lease.release.assert_called_once_with()

    def test_sdk_close_failure_is_visible_without_late_route_rewrites_or_blocked_exit(self):
        window = fixtures.headless_window()
        events = []
        window.output_route.restore.side_effect = lambda: events.append('restore')
        window.destroy.side_effect = lambda: events.append('destroy')
        def fail_close():
            events.append('sdk_close')
            raise AudioBackendError('PRIVATE NATIVE DETAIL')
        with patch('remotevoice.host_app.close_audio_backend', side_effect=fail_close) as close, \
             self.assertLogs('remotevoice.ui', level='WARNING') as logs:
            window._on_close()
        close.assert_called_once_with()
        self.assertEqual(events[events.index('sdk_close') + 1:], ['destroy'])
        self.assertNotIn('PRIVATE', '\n'.join(logs.output))
        self.assertIn('logout was not confirmed', '\n'.join(logs.output))
        window.destroy.assert_called_once_with()

    def test_stalled_server_close_is_bounded_and_keeps_guard_and_host_ownership(self):
        window = fixtures.headless_window()
        window.server_thread = Mock(is_alive=Mock(return_value=True))
        with patch('remotevoice.host_app.time.monotonic', return_value=10):
            window._on_close()
        window.destroy.assert_not_called()
        with patch('remotevoice.host_app.time.monotonic', return_value=30), \
             patch('remotevoice.host_app.os._exit', side_effect=ForcedClose) as native_exit:
            with self.assertRaises(ForcedClose):window._close_when_stopped()
        native_exit.assert_called_once_with(70)
        window.destroy.assert_not_called()
        window._host_lease.release.assert_not_called()
        window._safety_guard.disarm.assert_not_called()
        window.output_route.restore.assert_called()
        window.server.stop.assert_called_once()

    def test_stalled_driver_close_is_bounded_without_claiming_native_cleanup(self):
        window = fixtures.headless_window()
        window.server_running = False
        window.server = None
        owner = controller()
        window._driver_runtime_factory = Mock()
        window._driver_runtime = owner
        window._driver_startup_worker = Mock(is_alive=Mock(return_value=True))
        with patch('remotevoice.host_app.time.monotonic', return_value=10):
            window._on_close()
        owner.request_stop.assert_called_once()
        window.destroy.assert_not_called()
        with patch('remotevoice.host_app.time.monotonic', return_value=30), \
             patch('remotevoice.host_app.os._exit', side_effect=ForcedClose) as native_exit:
            with self.assertRaises(ForcedClose):window._close_when_stopped()
        native_exit.assert_called_once_with(70)
        window.destroy.assert_not_called()
        self.assertFalse(window._driver_cleanup_confirmed())
        owner.stop.assert_not_called()  # Never run native cleanup on the UI thread.
        window._host_lease.release.assert_not_called()
        window._safety_guard.disarm.assert_not_called()

    def test_repeated_close_requests_do_not_extend_waiting_forever(self):
        window = fixtures.headless_window()
        window.server_thread = Mock(is_alive=Mock(return_value=True))
        with patch('remotevoice.host_app.time.monotonic', return_value=10):
            window._on_close()
        with patch('remotevoice.host_app.time.monotonic', return_value=14):
            window._on_close()
        with patch('remotevoice.host_app.time.monotonic', return_value=16), \
             patch('remotevoice.host_app.os._exit', side_effect=ForcedClose) as native_exit:
            with self.assertRaises(ForcedClose):window._close_when_stopped()
        native_exit.assert_called_once_with(70)
        window.destroy.assert_not_called()
        window._host_lease.release.assert_not_called()

    def test_clean_close_releases_guard_and_lease_without_a_delay(self):
        window = fixtures.headless_window()
        guard = window._safety_guard
        window._on_close()
        window.destroy.assert_called_once()
        window._host_lease.release.assert_called_once()
        guard.disarm.assert_called_once()

    def test_unconfirmed_cleanup_requires_process_exit_even_after_worker_disappears(self):
        for failed_owner in ('server', 'driver'):
            with self.subTest(owner=failed_owner):
                window = fixtures.headless_window()
                if failed_owner == 'server':
                    window.server.cleanup_failed = True
                else:
                    window._driver_runtime_factory = Mock()
                    window._driver_runtime = controller()
                    window._driver_cleanup_error = 'native cleanup unconfirmed'
                with patch('remotevoice.host_app.os._exit', side_effect=ForcedClose) as native_exit:
                    with self.assertRaises(ForcedClose):window._on_close()
                native_exit.assert_called_once_with(70)
                window.destroy.assert_not_called()
                window._host_lease.release.assert_not_called()
                window._safety_guard.disarm.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Real hidden Windows Tk event loop')
    def test_hidden_tk_process_exits_after_deadline_with_a_stalled_executor(self):
        root = Path(__file__).resolve().parents[1]
        code = '''
import threading
import time
import tkinter as tk
import os
import sys
from concurrent.futures import ThreadPoolExecutor
import remotevoice.host_app as app
from test_host_output_routing import headless_window
root=tk.Tk();root.withdraw()
window=headless_window()
block=threading.Event()
pool=ThreadPoolExecutor(max_workers=1)
pending=pool.submit(block.wait)
worker=threading.Thread(target=pending.result,daemon=True)
worker.start()
window.server_thread=worker
if sys.argv[1]=='unconfirmed':
    window.server_thread=None
    window.server.cleanup_failed=True
window.after=root.after
window.destroy=root.destroy
guard=window._safety_guard
started=time.monotonic()
real_exit=os._exit
def verified_exit(code):
    elapsed=time.monotonic()-started
    minimum=0.0 if sys.argv[1]=='unconfirmed' else 4.5
    if (code!=70 or not minimum<=elapsed<9.0 or
            window._host_lease.release.called or guard.disarm.called):
        real_exit(99)
    os.write(1,b'owned_hidden_tk_close_ok')
    real_exit(code)
app.os._exit=verified_exit
window._on_close()
root.mainloop()
'''
        for mode in ('tracked', 'unconfirmed'):
            with self.subTest(mode=mode):
                checked = subprocess.run([sys.executable, '-c', code, mode], cwd=root,
                    env={**os.environ, 'PYTHONPATH': os.pathsep.join((str(root/'host'), str(root/'tests')))},
                    capture_output=True, timeout=12,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                self.assertEqual(checked.returncode, 70, checked.stderr)
                self.assertIn(b'owned_hidden_tk_close_ok', checked.stdout)


if __name__ == '__main__':
    unittest.main()
