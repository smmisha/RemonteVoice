from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest

from remontevoice.broker_process import (
    BrokerProcessTerminationRequired, run_broker_process,
)


class FatalTokenFailure(BaseException):
    pass


class Host:
    def __init__(self, clean=True, error=None):
        self.clean, self.error = clean, error
        self.serves = self.observations = 0

    def serve(self, stop):
        self.serves += 1
        if self.error:
            raise self.error

    @property
    def cleanup_confirmed(self):
        self.observations += 1
        if isinstance(self.clean, BaseException):
            raise self.clean
        return self.clean


class BrokerProcessTests(unittest.TestCase):
    def setUp(self):
        self.exits = []
        self.stop = threading.Event()

    def run_host(self, host):
        return run_broker_process(lambda: host, self.stop, hard_exit=self.exits.append)

    def test_clean_return_is_zero_and_serves_once(self):
        host = Host()
        self.assertEqual(self.run_host(host), 0)
        self.assertEqual((host.serves, host.observations, self.exits), (1, 1, []))

    def test_ordinary_failure_returns_one_only_after_confirmed_cleanup(self):
        host = Host(error=RuntimeError('rejected client'))
        self.assertEqual(self.run_host(host), 1)
        self.assertEqual((host.observations, self.exits), (1, []))

    def test_unknown_cleanup_requires_process_termination(self):
        for clean in (False, None, 1, RuntimeError('query'), FatalTokenFailure()):
            with self.subTest(clean=type(clean).__name__):
                host = Host(clean=clean)
                exits = []
                with self.assertRaises(BrokerProcessTerminationRequired) as raised:
                    run_broker_process(lambda: host, self.stop, hard_exit=exits.append)
                self.assertIs(raised.exception.cleanup_owner, host)
                self.assertEqual(exits, [70])

    def test_fatal_does_not_read_cleanup_or_invoke_general_cleanup(self):
        host = Host(error=FatalTokenFailure())
        with self.assertRaises(BrokerProcessTerminationRequired):
            self.run_host(host)
        self.assertEqual(host.observations, 0)
        self.assertEqual(self.exits, [70])

    def test_factory_failure_requires_exit_without_claiming_no_resources(self):
        for error in (RuntimeError('partial allocation'), FatalTokenFailure()):
            exits = []
            def factory():
                raise error
            with self.assertRaises(BrokerProcessTerminationRequired) as raised:
                run_broker_process(factory, self.stop, hard_exit=exits.append)
            self.assertIsNone(raised.exception.cleanup_owner)
            self.assertEqual(exits, [70])

    def test_failed_hard_exit_adapter_does_not_retry_or_escape_as_exception(self):
        host = Host(clean=False)
        calls = []
        def broken_exit(code):
            calls.append(code)
            raise RuntimeError('not terminated')
        with self.assertRaises(BrokerProcessTerminationRequired) as raised:
            run_broker_process(lambda: host, self.stop, hard_exit=broken_exit)
        self.assertIs(raised.exception.cleanup_owner, host)
        self.assertEqual(calls, [70])
        self.assertEqual(host.observations, 1)

    def test_real_child_exits_on_fatal_without_continuing_or_running_finalizers(self):
        code = '''
import threading
from remontevoice.broker_process import run_broker_process
class Fatal(BaseException): pass
class Host:
    def serve(self, stop): raise Fatal()
    @property
    def cleanup_confirmed(self):
        print("UNSAFE_READ", flush=True)
        return True
try:
    run_broker_process(Host, threading.Event())
finally:
    print("UNSAFE_FINALIZER", flush=True)
print("UNSAFE_CONTINUATION", flush=True)
'''
        root = Path(__file__).resolve().parents[1]
        environment = dict(os.environ, PYTHONPATH=str(root / 'host'))
        result = subprocess.run([sys.executable, '-c', code], cwd=root, env=environment,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 70)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')


if __name__ == '__main__':
    unittest.main()
