import os
import subprocess
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from remontevoice.broker_entry import BrokerBootstrapHost, _run_muted_lab, main


class BootstrapEntryTests(unittest.TestCase):
    def setup_owners(self):
        manifest=Mock(is_verified=True,cleanup_confirmed=True)
        application=Mock(cleanup_confirmed=True)
        return manifest,application

    def test_manifest_owner_outlives_clean_application_and_constructor_is_inert(self):
        manifest,application=self.setup_owners();events=[]
        application.serve.side_effect=lambda _:events.append('serve')
        manifest.close.side_effect=lambda:events.append('unpin')
        with patch('remontevoice.broker_entry.PinnedInstalledManifest',return_value=manifest) as create, \
             patch('remontevoice.broker_entry.InstalledBrokerApplication',return_value=application):
            owner=BrokerBootstrapHost();create.assert_not_called()
            owner.serve(threading.Event())
        self.assertEqual(events,['serve','unpin'])
        self.assertTrue(owner.cleanup_confirmed)

    def test_unknown_application_cleanup_retains_manifest(self):
        manifest,application=self.setup_owners();application.cleanup_confirmed=False
        application.serve.side_effect=RuntimeError('retained worker')
        with patch('remontevoice.broker_entry.PinnedInstalledManifest',return_value=manifest), \
             patch('remontevoice.broker_entry.InstalledBrokerApplication',return_value=application):
            owner=BrokerBootstrapHost()
            with self.assertRaises(RuntimeError):owner.serve(threading.Event())
        manifest.close.assert_not_called();self.assertFalse(owner.cleanup_confirmed)

    def test_fatal_application_skips_arbitrary_cleanup(self):
        class Fatal(BaseException):pass
        manifest,application=self.setup_owners();fatal=Fatal();application.serve.side_effect=fatal
        with patch('remontevoice.broker_entry.PinnedInstalledManifest',return_value=manifest), \
             patch('remontevoice.broker_entry.InstalledBrokerApplication',return_value=application):
            owner=BrokerBootstrapHost()
            with self.assertRaises(Fatal) as raised:owner.serve(threading.Event())
        self.assertIs(raised.exception,fatal)
        manifest.close.assert_not_called();self.assertFalse(owner.cleanup_confirmed)

    def test_invalid_cli_does_not_load_any_policy_or_create_process_host(self):
        with patch('remontevoice.broker_entry.sys.argv',['broker.exe','--policy','untrusted.json']), \
             patch('remontevoice.broker_entry.run_broker_process') as run:
            self.assertEqual(main(),64)
            run.assert_not_called()

    def test_only_exact_lab_flag_selects_bounded_muted_mode(self):
        for args in (['--lab-muted-only'], ['--lab-muted-only', '120'], ['--policy', 'x']):
            with self.subTest(args=args), patch('remontevoice.broker_entry.sys.argv', ['broker.exe']+args), \
                 patch('remontevoice.broker_entry.signal.signal'), \
                 patch('remontevoice.broker_entry._run_muted_lab', return_value=0) as lab, \
                 patch('remontevoice.broker_entry.run_broker_process') as normal:
                self.assertEqual(main(), 0 if len(args)==1 else 64)
                self.assertEqual(lab.call_count, int(len(args)==1))
                normal.assert_not_called()

    def test_muted_mode_propagates_through_same_verified_bootstrap(self):
        manifest,application=self.setup_owners()
        with patch('remontevoice.broker_entry.PinnedInstalledManifest',return_value=manifest), \
             patch('remontevoice.broker_entry.InstalledBrokerApplication',return_value=application) as create:
            owner=BrokerBootstrapHost(lab_muted_only=True)
            owner.serve(threading.Event())
            create.assert_called_once_with(manifest.load.return_value,lab_muted_only=True)
            self.assertTrue(owner.cleanup_confirmed)

    def test_lab_deadlines_are_cancelled_after_return_or_exception(self):
        for failed in (False,True):
            stop=threading.Event()
            timers=[]
            def timer(delay,action):
                value=Mock();timers.append((delay,action,value));return value
            def run(factory,event):
                self.assertIs(event,stop)
                self.assertTrue(factory()._lab_muted_only)
                timers[0][1]()
                self.assertTrue(stop.is_set())
                if failed:raise RuntimeError('startup failed')
                return 0
            with patch('remontevoice.broker_entry.threading.Timer',side_effect=timer), \
                 patch('remontevoice.broker_entry.run_broker_process',side_effect=run):
                if failed:
                    with self.assertRaises(RuntimeError):_run_muted_lab(stop)
                else:self.assertEqual(_run_muted_lab(stop),0)
            self.assertEqual([t[0] for t in timers],[60.0,90.0])
            for _,_,t in timers:
                t.start.assert_called_once();t.cancel.assert_called_once()
                self.assertTrue(t.daemon)

    def test_actual_lab_watchdog_exits_child_when_bootstrap_never_returns(self):
        code='''
import threading,time
import remontevoice.broker_entry as entry
entry._LAB_STOP_SECONDS=0.03
entry._LAB_EXIT_SECONDS=0.08
def blocked(factory,stop):
    time.sleep(30)
entry.run_broker_process=blocked
entry._run_muted_lab(threading.Event())
raise SystemExit(99)
'''
        result=subprocess.run([sys.executable,'-c',code],timeout=5,capture_output=True,
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.assertEqual(result.returncode,70)
