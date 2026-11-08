"""Undeployed per-user linked-UAC composition, never a client configuration API.

A dedicated protected executable must supply its trusted complete installation
pins. No service install, elevation, credentials, microphone enable or route
change occurs here. SYSTEM hosting is unsupported. Supplying a pin dictionary
from a workspace script is not a protected deployment or loader attestation.
"""
from __future__ import annotations

import hashlib
import ntpath
import re
import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from collections.abc import Mapping

from .broker_host import BrokerHost
from .broker_pipe_client import BrokerPipeClient, BrokerClientOpenError
from .driver_mic import DriverMicWriter
from .mic_broker import BrokerPolicy, PeerIdentity
from .windows_broker_accounts import observe_broker_accounts
from .windows_broker_bundle import WindowsProtectedBundle
from .windows_broker_identity import WindowsPeerVerifier, _canonical_path
from .windows_broker_pipe import NativePipeServer, NativePipeClient
from .windows_driver_transport import WindowsDriverMicTransport, _instance


class BrokerCompositionError(RuntimeError):
    pass


@dataclass(frozen=True, repr=False)
class BrokerInstallation:
    broker_executable: str
    client_executable: str
    trusted_files: Mapping[str, str] = field(repr=False)
    driver_instance_id: str

    def __post_init__(self):
        broker, client = (_canonical_path(p) for p in (self.broker_executable, self.client_executable))
        if (broker == client or not broker.endswith('.exe') or not client.endswith('.exe')
                or ntpath.dirname(broker) != ntpath.dirname(client)):
            raise ValueError('Dedicated broker and client must share a protected installation')
        if not isinstance(self.trusted_files, Mapping) or not 2 <= len(self.trusted_files) <= 4096:
            raise ValueError('Complete trusted installation pins required')
        root = ntpath.dirname(broker) + '\\'
        pins = {}
        for path, digest in self.trusted_files.items():
            path = _canonical_path(path)
            if (path in pins or not path.startswith(root) or path.endswith('\\')
                    or type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest)):
                raise ValueError('Invalid installation file inventory')
            pins[path] = digest
        if broker not in pins or client not in pins:
            raise ValueError('Both executable identities must be pinned')
        instance = _instance(self.driver_instance_id)
        if instance is None:
            raise ValueError('Exact driver instance required')
        object.__setattr__(self, 'broker_executable', broker)
        object.__setattr__(self, 'client_executable', client)
        object.__setattr__(self, 'trusted_files', MappingProxyType(pins))
        object.__setattr__(self, 'driver_instance_id', instance)


def broker_pipe_name(accounts) -> str:
    """Same deterministic name from either *observed* linked-token direction."""
    client = accounts.client
    material = f'{client.user_sid}|{client.logon_sid}|{client.session_id}'.encode('ascii')
    return 'RemoteVoice-Mic-' + hashlib.sha256(material).hexdigest()[:32]


class InstalledBrokerApplication:
    """Inert owner returned before native acquisition; run through process boundary."""

    def __init__(self, installation: BrokerInstallation, *, lab_muted_only: bool = False):
        if type(installation) is not BrokerInstallation:
            raise TypeError('Explicit trusted installation policy required')
        if type(lab_muted_only) is not bool:
            raise TypeError('Explicit laboratory mode required')
        self.installation = installation
        self._lab_muted_only = lab_muted_only
        self._used = self._finished = self._fatal = False
        self._bundle = self._verifier = self._listener = self._host = None
        self._partial_owner = None
        self._cleanup_errors = []

    @property
    def cleanup_confirmed(self) -> bool:
        return (self._finished and not self._fatal and self._partial_owner is None
                and not self._cleanup_errors
                and (self._bundle is None or self._bundle.cleanup_confirmed is True)
                and (self._host is None or self._host.cleanup_confirmed is True))

    def serve(self, stop: threading.Event):
        if self._used:
            raise BrokerCompositionError('Broker application is single-use')
        self._used = True
        original = None
        try:
            accounts = observe_broker_accounts()
            if accounts.current_is_broker is not True:
                raise BrokerCompositionError('An explicitly elevated linked broker process is required')
            self._bundle = WindowsProtectedBundle(self.installation.trusted_files)
            self._bundle.verify()
            self._bundle.verify_current_executable(self.installation.broker_executable)
            peer = PeerIdentity(accounts.client.user_sid, accounts.client.logon_sid,
                                accounts.client.session_id,
                                self.installation.trusted_files[self.installation.client_executable])
            self._verifier = WindowsPeerVerifier(peer.user_sid, peer.logon_sid, peer.session_id,
                {self.installation.client_executable: peer.image_sha256})
            self._listener = NativePipeServer(broker_pipe_name(accounts), peer.logon_sid)
            # Full runtime pins remain held until after listener/session cleanup.
            policy = BrokerPolicy(peer, lambda actual: self._bundle.is_verified and actual == peer)
            self._host = BrokerHost(self._listener, self._verifier, policy, self._make_writer)
            self._host.serve(stop)
        except BaseException as error:
            original = error
            self._fatal = not isinstance(error, Exception)
            if getattr(error, 'cleanup_owner', None) not in (None, self._bundle):
                self._partial_owner = error.cleanup_owner
        finally:
            if not self._fatal:
                # BrokerHost owns session/listener/verifier once constructed.
                resources = [] if self._host is not None else [self._listener, self._verifier]
                for resource in resources:
                    if resource is None:
                        continue
                    try:
                        resource.close()
                    except BaseException as error:
                        self._cleanup_errors.append(error)
                # Keep runtime pins while a retained session/listener may still
                # execute its code. Unknown cleanup requires process exit, not
                # an unlocked runtime or a replacement host.
                try:
                    pins_releasable = (not self._cleanup_errors and self._partial_owner is None
                        and (self._host is None or self._host.cleanup_confirmed is True))
                    if self._bundle is not None and pins_releasable:
                        self._bundle.close()
                except BaseException as error:
                    self._cleanup_errors.append(error)
            self._finished = True
        if original is not None and not isinstance(original, Exception):
            raise original
        fatal_cleanup = next((e for e in self._cleanup_errors if not isinstance(e, Exception)), None)
        if fatal_cleanup is not None:
            self._fatal = True
            raise fatal_cleanup
        if original is not None:
            raise original
        if self._cleanup_errors:
            raise BrokerCompositionError('Broker installation cleanup unconfirmed') from None

    def _make_writer(self):
        if self._lab_muted_only:
            raise BrokerCompositionError('Microphone disabled in admission-only laboratory mode')
        return DriverMicWriter(lambda: WindowsDriverMicTransport(self.installation.driver_instance_id))


class InstalledBrokerClient:
    """Worker-owned mutual-authentication composition for the persistent sink.

    Construction is inert; open() is single-use on the sink's worker. Runtime
    pins remain owned until client and verifier cleanup have been attempted.
    A failed open exposes this owner when cleanup cannot be confirmed.
    """

    def __init__(self, installation: BrokerInstallation):
        if type(installation) is not BrokerInstallation:
            raise TypeError('Explicit trusted installation policy required')
        self.installation = installation
        self._used = self._closed = self._confirmed = False
        self._thread = None
        self._bundle = self._verifier = self._pipe = self._client = self._partial = None

    @property
    def cleanup_confirmed(self):
        return self._confirmed

    def open(self, timeout: float = 0.25):
        if self._used:
            raise BrokerCompositionError('Installed client is single-use')
        self._used, self._thread = True, threading.get_ident()
        try:
            accounts = observe_broker_accounts()
            if accounts.current_is_broker is not False:
                raise BrokerCompositionError('A linked non-elevated client process is required')
            self._bundle = WindowsProtectedBundle(self.installation.trusted_files)
            self._bundle.verify()
            self._bundle.verify_current_executable(self.installation.client_executable)
            identity = accounts.broker
            self._verifier = WindowsPeerVerifier(identity.user_sid, identity.logon_sid,
                identity.session_id, {self.installation.broker_executable:
                    self.installation.trusted_files[self.installation.broker_executable]})
            self._pipe = NativePipeClient.connect(broker_pipe_name(accounts), timeout)
            self._client = BrokerPipeClient(self._pipe, self._verifier, timeout=timeout)
            return self
        except BaseException as original:
            self._partial = getattr(original, 'cleanup_owner', None)
            cleanup_error = None
            try:
                self.close()
            except BaseException as error:
                cleanup_error = error
            fatal = (original if not isinstance(original, Exception) else
                     cleanup_error if cleanup_error is not None and not isinstance(cleanup_error, Exception) else None)
            if fatal is not None:
                fatal.cleanup_owner = self
                raise fatal
            if not self._confirmed:
                raise BrokerClientOpenError(self) from None
            raise

    def _owner(self):
        if self._thread != threading.get_ident() or self._closed or self._client is None:
            raise BrokerCompositionError('Installed broker client worker is unavailable')

    @property
    def observation(self):
        return self._client.observation if self._client is not None and not self._closed else None

    def observe(self):
        self._owner()
        return self._client.observe()

    def heartbeat(self):
        self._owner()
        return self._client.heartbeat()

    def set_microphone_enabled(self, enabled):
        self._owner()
        return self._client.set_microphone_enabled(enabled)

    def submit(self, payload):
        self._owner()
        return self._client.submit(payload)

    def close(self):
        if self._thread is not None and self._thread != threading.get_ident():
            raise BrokerCompositionError('Installed client cleanup belongs to its worker')
        self._closed, self._confirmed = True, False
        errors = []
        # Each owner is retained on failure. Never allow a failed constructor to
        # disappear just because its variable assignment did not complete.
        owners = []
        for owner in (self._client, self._partial):
            if owner is not None and all(owner is not existing for existing in owners):
                owners.append(owner)
        for owner in owners:
            try:
                owner.close()
                if owner.cleanup_confirmed is not True:
                    raise BrokerCompositionError('Broker connection cleanup unconfirmed')
            except BaseException as error:
                errors.append(error)
        for owner in (self._pipe, self._verifier):
            if owner is not None:
                try:
                    owner.close()
                except BaseException as error:
                    errors.append(error)
        if not errors and self._bundle is not None:
            try:
                self._bundle.close()
            except BaseException as error:
                errors.append(error)
        if errors:
            fatal = next((e for e in errors if not isinstance(e, Exception)), None)
            raise fatal if fatal is not None else BrokerCompositionError('Installed client cleanup unconfirmed')
        self._confirmed = True
