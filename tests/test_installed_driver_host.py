import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from remontevoice.audio_backend import DriverBackendSnapshot
from remontevoice.installed_driver_host import (
    InstalledDriverHost, InstalledDriverHostError, InstalledDriverHostStopError,
)

INSTANCE = r"ROOT\MEDIA\0000"


class Manifest:
    def __init__(self, events):
        self.events = events
        self.is_verified = True
        self.cleanup_confirmed = False
        self.load_calls = self.close_calls = 0
        self.load_hook = self.close_error = None

    def load(self):
        self.load_calls += 1
        self.events.append("manifest load")
        if self.load_hook:
            self.load_hook()
        return SimpleNamespace(driver_instance_id=INSTANCE)

    def close(self):
        self.close_calls += 1
        self.events.append("manifest close")
        if self.close_error:
            raise self.close_error
        self.cleanup_confirmed = True


class Runtime:
    def __init__(self, events):
        self.events = events
        self.started = self.starting = self.stop_requested = self.cleanup_confirmed = False
        self.ready = True
        self.guard_ready = None
        self.start_hook = self.stop_error = None
        self.stop_calls = []
        self.start_calls = self.clients_created = 0
        self.lease = Mock()

    @property
    def snapshot(self):
        return SimpleNamespace(cleanup_confirmed=self.cleanup_confirmed)

    def start(self):
        self.start_calls += 1
        self.starting = True
        self.events.append("runtime start")
        try:
            if self.start_hook:
                self.start_hook()
            if self.stop_requested:
                raise RuntimeError("Runtime startup was cancelled")
            self.clients_created += 1
            self.started = True
        finally:
            self.starting = False

    def stop(self, timeout):
        self.stop_requested = True
        self.stop_calls.append(timeout)
        self.events.append("runtime stop")
        if self.starting or self.stop_error:
            raise RuntimeError("Runtime cleanup pending")
        self.cleanup_confirmed = True
        self.started = False

    def request_stop(self):
        self.stop_requested = True

    def observe(self):
        ready = self.started and self.ready and not self.stop_requested
        return DriverBackendSnapshot(1, ready, ready, "render-id", "mic-id", time.monotonic())

    def guard_is_available(self):
        return self.started and not self.stop_requested and (self.ready if self.guard_ready is None else self.guard_ready)

    def acquire_sink(self):
        if not self.started or self.stop_requested:
            raise RuntimeError("No active runtime")
        return self.lease


class InstalledDriverHostTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.manifest = Manifest(self.events)
        self.runtime = Runtime(self.events)
        self.inventory = object()
        self.patches = [
            patch("remontevoice.installed_driver_host.PinnedInstalledManifest", return_value=self.manifest),
            patch("remontevoice.installed_driver_host.WindowsDriverEndpointInventory", return_value=self.inventory),
            patch("remontevoice.installed_driver_host.DriverBackendRuntime", return_value=self.runtime),
            patch("remontevoice.installed_driver_host.BrokerMicSink"),
            patch("remontevoice.installed_driver_host.open_installed_broker_client"),
        ]
        self.make_manifest, self.make_inventory, self.make_runtime, self.make_sink, self.open_client = [
            patcher.start() for patcher in self.patches]
        for patcher in self.patches:
            self.addCleanup(patcher.stop)
        self.hosts = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        self.runtime.stop_error = self.manifest.close_error = None
        for host in self.hosts:
            if not host._construction_unknown:
                host.stop()
                self.assertTrue(host.snapshot.cleanup_confirmed)

    def host(self):
        host = InstalledDriverHost()
        self.hosts.append(host)
        return host

    def block(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def wait():
            entered.set()
            if not release.wait(1):
                raise RuntimeError("Test did not release startup")
        return entered, release, wait

    def start_thread(self, host):
        errors = []
        def start():
            try:
                host.start()
            except InstalledDriverHostError as error:
                errors.append(error)
        worker = threading.Thread(target=start)
        worker.start()
        return worker, errors

    def test_constructor_and_cached_unavailable_backend_are_inert(self):
        host = self.host()
        self.assertTrue(host.snapshot.cleanup_confirmed)
        self.assertFalse(host.snapshot.starting)
        self.assertFalse(host.guard_is_available())
        self.assertFalse(host.backend.is_available())
        self.assertFalse(host.observe().broker_authenticated)
        for make in (self.make_manifest, self.make_inventory, self.make_runtime, self.make_sink, self.open_client):
            make.assert_not_called()

    def test_start_uses_exact_pinned_instance_and_fixed_inert_client_factory(self):
        host = self.host()
        host.start()
        self.make_manifest.assert_called_once_with("client")
        self.make_inventory.assert_called_once_with(INSTANCE)
        self.assertIs(self.make_runtime.call_args.args[0], self.inventory)
        sink_factory = self.make_runtime.call_args.args[1]
        self.make_sink.assert_not_called()
        self.assertIs(sink_factory(), self.make_sink.return_value)
        self.make_sink.assert_called_once_with(self.open_client)
        self.open_client.assert_not_called()
        self.assertTrue(host.guard_is_available())
        self.assertIs(host.acquire_sink(), self.runtime.lease)
        self.assertFalse(host.snapshot.cleanup_confirmed)
        self.assertEqual(self.manifest.close_calls, 0)
        host.stop()
        self.assertEqual(self.events[-1], "manifest close")
        self.assertLess(self.events.index("runtime stop"), self.events.index("manifest close"))
        self.assertTrue(host.snapshot.cleanup_confirmed)
        self.assertFalse(host.guard_is_available())
        self.assertFalse(host.observe().broker_authenticated)
        with self.assertRaises(InstalledDriverHostError):
            host.start()

    def test_start_can_return_before_endpoint_monitor_readiness(self):
        self.runtime.ready = False
        host = self.host()
        host.start()
        self.assertFalse(host.backend.is_available())
        self.assertFalse(host.snapshot.starting)
        self.runtime.ready = True
        self.assertTrue(host.backend.is_available())

    def test_stop_request_is_inert_and_final_cleanup_remains_explicit(self):
        host = self.host()
        host.start()
        host.request_stop()
        self.assertTrue(self.runtime.stop_requested)
        self.assertFalse(host.backend.is_available())
        self.assertFalse(host.guard_is_available())
        self.assertEqual(self.runtime.stop_calls, [])
        self.assertEqual(self.manifest.close_calls, 0)
        self.assertFalse(host.snapshot.cleanup_confirmed)
        host.stop()
        self.assertTrue(host.snapshot.cleanup_confirmed)

    def test_pending_writer_guard_can_remain_healthy_while_new_lease_admission_stays_strict(self):
        host = self.host()
        host.start()
        self.runtime.ready = False
        self.runtime.guard_ready = True
        self.assertFalse(host.backend.is_available())
        self.assertTrue(host.guard_is_available())
        with self.assertRaises(InstalledDriverHostError):
            host.acquire_sink()
        self.runtime.ready = True
        self.assertIs(host.acquire_sink(), self.runtime.lease)
        host.stop()
        self.assertFalse(host.guard_is_available())

    def test_guard_failure_or_controller_error_never_admits_or_refreshes_health(self):
        host = self.host()
        host.start()
        self.runtime.guard_is_available = Mock(side_effect=OSError("Cached guard unavailable"))
        self.assertFalse(host.guard_is_available())
        self.runtime.guard_is_available = Mock(return_value=True)
        host._fail("controller_failed")
        self.assertFalse(host.guard_is_available())
        self.runtime.guard_is_available.assert_not_called()
        with self.assertRaises(InstalledDriverHostError):
            host.acquire_sink()

    def test_unverified_manifest_never_constructs_inventory_or_runtime(self):
        self.manifest.is_verified = False
        host = self.host()
        with self.assertRaises(InstalledDriverHostError):
            host.start()
        self.make_inventory.assert_not_called()
        self.make_runtime.assert_not_called()
        self.assertIs(host._manifest, self.manifest)
        self.assertFalse(host.snapshot.cleanup_confirmed)
        self.assertFalse(host.guard_is_available())
        host.stop()
        self.assertEqual(self.manifest.close_calls, 1)

    def test_stop_during_blocked_manifest_load_times_out_and_prevents_late_client(self):
        entered, release, wait = self.block()
        self.manifest.load_hook = wait
        host = self.host()
        worker, errors = self.start_thread(host)
        try:
            self.assertTrue(entered.wait(1))
            before = time.monotonic()
            with self.assertRaises(InstalledDriverHostStopError):
                host.stop(0.01)
            self.assertLess(time.monotonic() - before, 0.1)
            self.assertTrue(host.snapshot.starting)
            self.assertTrue(host.snapshot.stopping)
            self.assertFalse(host.snapshot.cleanup_confirmed)
            self.assertFalse(host.guard_is_available())
            self.assertEqual(self.manifest.close_calls, 0)
            self.make_runtime.assert_not_called()
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.make_inventory.assert_not_called()
            self.make_runtime.assert_not_called()
            host.stop()
            self.assertTrue(host.snapshot.cleanup_confirmed)
            self.assertEqual(self.manifest.close_calls, 1)
        finally:
            release.set()
            worker.join(1)

    def test_stop_during_runtime_start_signals_inner_cancellation_before_waiting_outer_lock(self):
        entered, release, wait = self.block()
        self.runtime.start_hook = wait
        host = self.host()
        worker, errors = self.start_thread(host)
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(InstalledDriverHostStopError):
                host.stop(0.01)
            self.assertTrue(self.runtime.stop_requested)
            self.assertIn(0, self.runtime.stop_calls)
            self.assertFalse(host.snapshot.cleanup_confirmed)
            self.assertEqual(self.manifest.close_calls, 0)
            release.set()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.runtime.clients_created, 0)
            self.assertEqual(len(errors), 1)
            host.stop()
            self.assertTrue(host.snapshot.cleanup_confirmed)
            self.make_runtime.assert_called_once()
        finally:
            release.set()
            worker.join(1)

    def test_runtime_cleanup_failure_retains_manifest_and_retries_only_same_objects(self):
        host = self.host()
        host.start()
        self.runtime.stop_error = OSError("Native inventory still owns COM")
        with self.assertRaises(InstalledDriverHostStopError):
            host.stop(0.1)
        self.assertIs(host._runtime, self.runtime)
        self.assertIs(host._manifest, self.manifest)
        self.assertFalse(host.snapshot.cleanup_confirmed)
        self.assertEqual(self.manifest.close_calls, 0)
        self.assertFalse(host.guard_is_available())
        with self.assertRaises(InstalledDriverHostError):
            host.acquire_sink()
        self.runtime.stop_error = None
        host.stop()
        self.assertTrue(host.snapshot.cleanup_confirmed)
        self.make_manifest.assert_called_once()
        self.make_runtime.assert_called_once()

    def test_successful_runtime_stop_without_confirmation_does_not_unlock_manifest(self):
        host = self.host()
        host.start()
        self.runtime.stop = Mock()  # A method return is not ownership evidence.
        with self.assertRaises(InstalledDriverHostStopError):
            host.stop(0.1)
        self.assertFalse(host.snapshot.cleanup_confirmed)
        self.assertEqual(self.manifest.close_calls, 0)
        self.runtime.cleanup_confirmed = True
        host.stop()
        self.assertTrue(host.snapshot.cleanup_confirmed)

    def test_manifest_cleanup_failure_keeps_old_owner_and_does_not_restart_clean_runtime(self):
        host = self.host()
        host.start()
        self.manifest.close_error = OSError("Manifest handle pending")
        with self.assertRaises(InstalledDriverHostStopError):
            host.stop()
        stopped = len(self.runtime.stop_calls)
        self.assertFalse(host.snapshot.cleanup_confirmed)
        self.assertTrue(host._runtime_clean)
        self.manifest.close_error = None
        host.stop()
        self.assertEqual(len(self.runtime.stop_calls), stopped)
        self.assertTrue(host.snapshot.cleanup_confirmed)
        self.make_runtime.assert_called_once()

    def test_stop_before_start_and_invalid_deadlines_do_not_construct_resources(self):
        host = self.host()
        for timeout in (-1, 1.1, True, float("nan"), float("inf"), "1"):
            with self.assertRaises(ValueError):
                host.stop(timeout)
        self.assertFalse(host.snapshot.stopping)
        host.stop()
        with self.assertRaises(InstalledDriverHostError):
            host.start()
        self.make_manifest.assert_not_called()
        self.make_runtime.assert_not_called()
        self.assertTrue(host.snapshot.cleanup_confirmed)

    def test_runtime_constructor_partial_owner_remains_retained_until_real_cleanup(self):
        error = RuntimeError("Runtime constructor failed")
        error.cleanup_owner = self.runtime
        self.make_runtime.side_effect = error
        self.runtime.stop_error = OSError("Partial cleanup pending")
        host = self.host()
        with self.assertRaises(InstalledDriverHostError):
            host.start()
        self.assertIs(host._runtime, self.runtime)
        with self.assertRaises(InstalledDriverHostStopError):
            host.stop()
        self.assertEqual(self.manifest.close_calls, 0)
        self.runtime.stop_error = None
        host.stop()
        self.assertTrue(host.snapshot.cleanup_confirmed)


if __name__ == "__main__":
    unittest.main()
