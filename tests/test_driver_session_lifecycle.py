"""Real loopback TLS/Windows IPC; admission, endpoints and audio are doubles.

These tests never enumerate/open physical audio devices or change Windows routes.
The restoration callback records intent only; hardware recovery is a separate gate.
"""
import asyncio
import json
import os
import secrets
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from remotevoice.broker_host import BrokerHost
from remotevoice.broker_mic_sink import BrokerMicSink
from remotevoice.broker_pipe_client import BrokerPipeClient
from remotevoice.driver_audio_bridge import DriverAudioBridge
from remotevoice.driver_backend_runtime import DriverBackendRuntime
from remotevoice.driver_mic import MicWriterState
from remotevoice.mic_broker import BrokerPolicy, PeerIdentity
from remotevoice.pairing import PairingManager
from remotevoice.protocol import MIC_FRAME, SYSTEM_FRAME, encode_audio_frame, encode_control
from remotevoice.server import RemoteVoiceServer
from remotevoice.windows_broker_identity import read_current_process_token
from remotevoice.windows_broker_pipe import NativePipeClient, NativePipeServer
from remotevoice.windows_driver_endpoints import DriverEndpointPair


PHONE_PCM = struct.pack('<480f', *([0.125] * 480))
DESKTOP_PCM = struct.pack('<960f', *([0.25] * 960))
PAIR = DriverEndpointPair('synthetic-render', 'synthetic-microphone')


class SyntheticWriter:
    def __init__(self):
        self.state = MicWriterState.IDLE
        self.cleaned = False
        self.frames = []
        self.stop_allowed = threading.Event()
        self.stop_allowed.set()

    @property
    def snapshot(self):
        return SimpleNamespace(state=self.state, cleanup_confirmed=self.cleaned,
                               worker_alive=not self.cleaned)

    def start(self):
        self.state = MicWriterState.STREAMING

    def submit(self, payload):
        self.frames.append(payload)
        return True

    def request_stop(self):
        self.state = MicWriterState.STOPPING

    def stop(self, timeout):
        if not self.stop_allowed.is_set():
            return  # Pending native cleanup; broker polling/IPC remain live.
        self.cleaned = True
        self.state = MicWriterState.CLOSED


class SyntheticCaptureBridge(DriverAudioBridge):
    """Replace only the physical recorder; keep actual sink and bridge lifetime."""
    def _capture_system_audio(self):
        packet = encode_audio_frame(SYSTEM_FRAME, DESKTOP_PCM)
        try:
            while not self._stop.is_set():
                future = asyncio.run_coroutine_threadsafe(self._send(packet), self.loop)
                future.result(timeout=1)
                self._mark_progress('capture')
                self._stop.wait(0.01)
        except Exception:
            if not self._stop.is_set():
                self._report_failure('Synthetic desktop transport stopped')


@unittest.skipUnless(os.name == 'nt', 'Requires real Windows local named pipes')
class DriverSessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sockets, self.clients, self.writers, self.bridges = [], [], [], []
        self.statuses, self.restorations, self.broker_errors = [], [], []
        self.server_task = self.broker_thread = self.runtime = self.broker = None
        self.broker_stop = threading.Event()
        temporary = tempfile.TemporaryDirectory(prefix='remotevoice-lifecycle-')
        self.addCleanup(temporary.cleanup)
        self.addAsyncCleanup(self.shutdown)
        root = Path(temporary.name)
        token = read_current_process_token()
        # This explicit double is not a protected Full/Limited admission test.
        peer = PeerIdentity(token.user_sid, token.logon_sid, token.session_id, 'a' * 64)

        class Verifier:
            cleanup_confirmed = True

            def verify_pipe_peer(self, handle, *, server_side):
                return peer

            def validate_pipe_token(self, handle, expected, *, server_side):
                return expected == peer

            def forget_pipe_peer(self, handle):
                pass

            def close(self):
                pass

        class Inventory:
            cleanup_confirmed = True

            def observe(self):
                return PAIR

        name = 'RemoteVoice-session-test-' + secrets.token_hex(6)

        def writer_factory():
            writer = SyntheticWriter()
            self.writers.append(writer)
            return writer

        self.broker = BrokerHost(NativePipeServer(name, token.logon_sid), Verifier(),
                                 BrokerPolicy(peer, lambda _: True), writer_factory)

        def serve_broker():
            try:
                self.broker.serve(self.broker_stop)
            except BaseException as error:
                self.broker_errors.append(error)

        self.broker_thread = threading.Thread(target=serve_broker, name='rv-test-broker', daemon=True)
        self.broker_thread.start()

        def client_factory():
            client = BrokerPipeClient(NativePipeClient.connect(name, 0.5), Verifier())
            self.clients.append(client)
            return client

        self.runtime = DriverBackendRuntime(Inventory(), lambda: BrokerMicSink(client_factory))
        await asyncio.to_thread(self.runtime.start)
        await self.wait(self.runtime.backend.is_available)
        self.assertEqual(len(self.clients), 1)
        self.assertEqual(self.writers, [])
        self.assertFalse(self.runtime.observe().mic_requested)

        def audio_factory(*args):
            bridge = SyntheticCaptureBridge(*args, driver_backend=self.runtime.backend,
                                             sink_factory=self.runtime.acquire_sink)
            self.bridges.append(bridge)
            return bridge

        self.ready = asyncio.Event()

        def status(value):
            self.statuses.append(value)
            if value == 'server_ready':
                if self.server._audio_session is None and self.bridges:
                    self.restorations.append((self.runtime.snapshot.session_active,
                                              self.server._client_active))
                self.ready.set()

        # Fresh loopback-only server, ephemeral port/identity/pairing, no Host UI.
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1', 0))
            port = reservation.getsockname()[1]
        self.pairing = PairingManager(root / 'paired.json')
        self.credential = self.pairing.pairing_pin
        self.server = RemoteVoiceServer('127.0.0.1', port, PAIR.capture_endpoint_id,
            PAIR.microphone_endpoint_id, root / 'identity', self.pairing, status,
            on_session_start=self.runtime.backend.is_available, audio_factory=audio_factory)
        capture = patch('remotevoice.driver_audio_bridge.sc.get_microphone',
            return_value=SimpleNamespace(id=PAIR.capture_endpoint_id, name='RemoteVoice Render'))
        capture.start()
        self.addCleanup(capture.stop)
        self.client_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self.client_ssl.check_hostname = False
        # Trust only this test's generated certificate; no system trust changes.
        self.uri = f'wss://127.0.0.1:{port}'
        self.server_task = asyncio.create_task(self.server.run())
        await asyncio.wait_for(self.ready.wait(), 5)
        self.client_ssl.load_verify_locations(cafile=str(root / 'identity' / 'host-certificate.pem'))

    async def wait(self, predicate, timeout=3):
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)

    async def phone(self):
        phone = await connect(self.uri, ssl=self.client_ssl, close_timeout=0.5)
        self.sockets.append(phone)
        await phone.send(encode_control({'type': 'auth', 'credential': self.credential,
                                         'audio_ready': True}))
        authenticated = json.loads(await asyncio.wait_for(phone.recv(), 2))
        self.assertEqual(authenticated['type'], 'auth_ok')
        self.assertIs(authenticated['microphone_enabled'], False)
        self.credential = authenticated['credential']
        await phone.send(encode_control({'type': 'audio_ready'}))
        await self.wait(lambda: self.bridges and self.bridges[-1]._started)
        self.assertFalse(self.bridges[-1].microphone_enabled)
        self.assertFalse(self.runtime.observe().mic_requested)
        return phone

    async def receive_control(self, phone):
        async with asyncio.timeout(2):
            while True:
                message = await phone.recv()
                if isinstance(message, str):
                    return json.loads(message)
                self.assertEqual(message, bytes([SYSTEM_FRAME]) + DESKTOP_PCM)

    async def enable_synthetic_mic(self, phone):
        await phone.send(encode_control({'type': 'mic_enabled', 'enabled': True}))
        self.assertEqual(await self.receive_control(phone), {'type': 'mic_state', 'enabled': True})
        await phone.send(encode_audio_frame(MIC_FRAME, PHONE_PCM))
        await self.wait(lambda: self.writers and self.writers[-1].frames)
        self.assertEqual(self.writers[-1].frames, [PHONE_PCM])

    async def shutdown(self):
        for writer in self.writers:
            writer.stop_allowed.set()
        for phone in self.sockets:
            phone.transport.abort()
        if self.server_task is not None:
            self.server.stop()
            await asyncio.wait_for(self.server_task, 5)
        if self.runtime is not None:
            await asyncio.to_thread(self.runtime.stop)
            self.assertTrue(self.runtime.snapshot.cleanup_confirmed)
        self.broker_stop.set()
        if self.broker_thread is not None:
            await asyncio.to_thread(self.broker_thread.join, 2)
            self.assertFalse(self.broker_thread.is_alive())
        if self.broker is not None:
            self.assertTrue(self.broker.cleanup_confirmed)
        self.assertEqual(self.broker_errors, [])
        self.assertTrue(all(client.cleanup_confirmed for client in self.clients))
        self.assertTrue(all(writer.cleaned for writer in self.writers))
        self.assertTrue(all(not bridge._threads for bridge in self.bridges))

    async def test_abrupt_phone_loss_restores_before_cleanup_and_reconnects_muted(self):
        for abrupt in (True, False, True):
            phone = await self.phone()
            desktop = await asyncio.wait_for(phone.recv(), 2)
            self.assertEqual(desktop, bytes([SYSTEM_FRAME]) + DESKTOP_PCM)
            await self.enable_synthetic_mic(phone)
            old_bridge = self.bridges[-1]
            restored = len(self.restorations)
            if abrupt:
                phone.transport.abort()  # No close frame: models a broken path.
            else:
                await phone.close()
            await self.wait(lambda: not self.server._client_lock.locked())
            self.assertEqual(len(self.restorations), restored + 1)
            self.assertEqual(self.restorations[-1], (True, False))
            self.assertEqual(old_bridge._threads, [])
            self.assertFalse(old_bridge.microphone_enabled)
            self.assertTrue(old_bridge._sink.snapshot.cleanup_confirmed)
            self.assertFalse(self.runtime.snapshot.session_active)
            self.assertFalse(self.runtime.observe().mic_requested)
            self.assertFalse(self.server.cleanup_failed)
            self.assertFalse(self.server._stop_requested.is_set())
            self.assertEqual(len(self.clients), 1)
        self.assertEqual(len(self.writers), 3)

    async def test_broker_loss_closes_phone_and_restores_without_readmitting_audio(self):
        phone = await self.phone()
        await self.enable_synthetic_mic(phone)
        with self.assertLogs('remotevoice.server', level='ERROR') as logged:
            self.broker_stop.set()
            async with asyncio.timeout(4):
                try:
                    while True:
                        await phone.recv()
                except ConnectionClosed:
                    pass
            await self.wait(lambda: not self.server._client_lock.locked())
        self.assertTrue(any('audio_session_failure stage=audio_cleanup' in entry for entry in logged.output))
        self.assertTrue(self.restorations)
        self.assertEqual(self.restorations[-1], (True, False))
        self.assertFalse(self.runtime.backend.is_available())
        self.assertTrue(self.server._stop_requested.is_set())
        self.assertTrue(self.server.cleanup_failed)
        self.assertIs(self.server._unclean_audio, self.bridges[0])
        self.assertFalse(self.bridges[0].microphone_enabled)
        self.assertTrue(all(writer.cleaned for writer in self.writers))
        self.assertEqual(len(self.clients), 1)
        await self.wait(self.server_task.done)
        with self.assertRaises(OSError):
            await connect(self.uri, ssl=self.client_ssl, open_timeout=1)
        self.assertEqual(len(self.bridges), 1)

    async def test_delayed_phone_release_keeps_transport_healthy_and_next_phone_muted(self):
        phone = await self.phone()
        await self.enable_synthetic_mic(phone)
        writer = self.writers[-1]
        writer.stop_allowed.clear()
        phone.transport.abort()
        await self.wait(lambda: self.runtime._lease is not None and self.runtime._lease._closing)
        try:
            await self.wait(lambda: not self.runtime.backend.is_available())
            # Span a 500-ms Host health tick while END is still confirming.
            deadline = time.monotonic() + 0.55
            while time.monotonic() < deadline:
                self.assertTrue(self.runtime.guard_is_available())
                self.assertFalse(self.runtime.backend.is_available())
                await asyncio.sleep(0.01)
            self.assertTrue(self.restorations)
            self.assertTrue(self.server._client_lock.locked())
            self.assertFalse(self.server._stop_requested.is_set())
            self.assertFalse(self.runtime._lease.submit(PHONE_PCM))
            replacement = await connect(self.uri, ssl=self.client_ssl, close_timeout=0.5)
            self.sockets.append(replacement)
            with self.assertRaises(ConnectionClosed) as rejected:
                await asyncio.wait_for(replacement.recv(), 1)
            self.assertEqual(rejected.exception.rcvd.code, 4002)
        finally:
            writer.stop_allowed.set()
        await self.wait(lambda: not self.server._client_lock.locked())
        self.assertTrue(writer.cleaned)
        self.assertFalse(self.server.cleanup_failed)
        self.assertFalse(self.server._stop_requested.is_set())
        self.assertTrue(self.runtime.guard_is_available())
        self.assertEqual(len(self.clients), 1)
        next_phone = await self.phone()
        self.assertFalse(self.bridges[-1].microphone_enabled)
        self.assertEqual(len(self.writers), 1)  # Reconnect never creates a writer.
        await next_phone.close()
        await self.wait(lambda: not self.server._client_lock.locked())


if __name__ == '__main__':
    unittest.main()
