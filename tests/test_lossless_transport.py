"""Negotiate lossless PCM explicitly, retaining old clients and authentication."""
import asyncio
import json
import os
from pathlib import Path
import socket
import ssl
import tempfile
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from remontevoice.certificates import load_or_create_identity
from remontevoice.pairing import PairingManager
from remontevoice.server import RemonteVoiceServer


class LosslessTransportTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, enabled, requested=True, supported=True):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); identity=root/'identity'; load_or_create_identity(identity)
            tls=ssl.create_default_context(cafile=str(identity/'host-certificate.pem'))
            pairing=PairingManager(root/'paired.json'); ready=asyncio.Event(); audio_instances=[]

            class InertAudio:
                def __init__(self,*args):
                    self.microphone_enabled=False;self.compression=False;audio_instances.append(self)
                    if not supported:self.enable_system_compression=None
                def enable_system_compression(self):self.compression=True
                def start(self):pass
                def stop(self):pass
                def assert_progress(self):pass
                def set_microphone_enabled(self,value):self.microphone_enabled=value

            with socket.socket() as reservation:
                reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1]
            server=RemonteVoiceServer('127.0.0.1',port,'inert','inert',identity,pairing,
                lambda status:ready.set() if status=='server_ready' else None,audio_factory=InertAudio)
            with patch.dict(os.environ,{'REMONTEVOICE_AUDIO_COMPRESSION_EXPERIMENTAL':'1' if enabled else '0'}):
                task=asyncio.create_task(server.run())
                try:
                    await asyncio.wait_for(ready.wait(),5)
                    async with connect(f'wss://127.0.0.1:{port}',ssl=tls,server_hostname='remontevoice.local',
                                       compression=None,proxy=None) as client:
                        message={'type':'auth','credential':pairing.pairing_pin}
                        if requested:message['system_deflate']=True
                        await client.send(json.dumps(message))
                        reply=json.loads(await client.recv());self.assertEqual(reply['type'],'auth_ok')
                        expected=enabled and requested and supported
                        self.assertEqual(reply.get('system_deflate',False),expected)
                        self.assertEqual(audio_instances[0].compression,expected)
                        self.assertIs(reply['microphone_enabled'],False)
                        await client.send('{"type":"ping"}')
                        self.assertEqual(json.loads(await client.recv()),{'type':'pong'})
                finally:
                    server.stop();await asyncio.wait_for(task,5)

    async def test_requested_supported_experiment_is_explicitly_acknowledged(self):await self.exercise(True)
    async def test_experiment_disabled_keeps_existing_format(self):await self.exercise(False)
    async def test_legacy_client_receives_no_new_field(self):await self.exercise(True,requested=False)
    async def test_uncapable_audio_factory_falls_back_without_false_ack(self):await self.exercise(True,supported=False)
