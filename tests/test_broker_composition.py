import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from remontevoice.broker_composition import (
    BrokerInstallation, InstalledBrokerApplication, BrokerCompositionError, broker_pipe_name,
    InstalledBrokerClient,
)
from remontevoice.mic_broker import BrokerPolicy, MicBroker, PeerIdentity

BROKER = r'C:\Program Files\RemonteVoice\RemonteVoiceBroker.exe'
CLIENT = r'C:\Program Files\RemonteVoice\RemonteVoice.exe'
RUNTIME = r'C:\Program Files\RemonteVoice\_internal\python314.dll'
PINS = {BROKER:'a'*64, CLIENT:'b'*64, RUNTIME:'c'*64}
def installation(): return BrokerInstallation(BROKER, CLIENT, PINS, r'ROOT\MEDIA\0000')
def accounts(elevated=True):
    user = 'S-1-5-21-1000'
    return SimpleNamespace(current_is_broker=elevated,
        broker=SimpleNamespace(user_sid=user, logon_sid='S-1-5-5-1-20', session_id=1),
        client=SimpleNamespace(user_sid=user, logon_sid='S-1-5-5-1-21', session_id=1))


class CompositionTests(unittest.TestCase):
    def test_lab_client_cannot_create_driver_writer_even_after_explicit_enable(self):
        peer=PeerIdentity('S-1-5-21-1000','S-1-5-5-1-21',1,'b'*64)
        owner=InstalledBrokerApplication(installation(),lab_muted_only=True)
        broker=MicBroker(authenticate=lambda:peer,policy=BrokerPolicy(peer,lambda _:True),
                         writer_factory=owner._make_writer)
        with patch('remontevoice.broker_composition.DriverMicWriter') as writer, \
             patch('remontevoice.broker_composition.WindowsDriverMicTransport') as driver:
            capability=broker.connect()
            self.assertTrue(broker.is_available())
            self.assertTrue(broker.set_microphone_enabled(capability,True))
            broker.poll()
            self.assertFalse(broker.is_available())
            self.assertFalse(broker.status().connected)
            self.assertTrue(broker.cleanup_confirmed)
            writer.assert_not_called();driver.assert_not_called()

    def test_normal_writer_factory_remains_inert_and_lab_flag_is_strict(self):
        with patch('remontevoice.broker_composition.DriverMicWriter') as writer, \
             patch('remontevoice.broker_composition.WindowsDriverMicTransport') as driver:
            owner=InstalledBrokerApplication(installation())
            self.assertIs(owner._make_writer(),writer.return_value)
            writer.assert_called_once();driver.assert_not_called()
        with self.assertRaises(TypeError):
            InstalledBrokerApplication(installation(),lab_muted_only=1)

    def test_inert_immutable_policy_requires_two_exact_exes_and_same_root(self):
        pins = dict(PINS); policy = BrokerInstallation(BROKER, CLIENT, pins, r'ROOT\MEDIA\0000')
        pins[CLIENT] = '0'*64
        self.assertEqual(policy.trusted_files[CLIENT.lower()], 'b'*64)
        with self.assertRaises(TypeError): policy.trusted_files[CLIENT] = '0'*64
        for args in ((BROKER,BROKER,PINS,r'ROOT\MEDIA\0000'),
                     (BROKER,CLIENT,{BROKER:'a'*64},r'ROOT\MEDIA\0000'),
                     (BROKER,CLIENT,PINS,None),
                     (BROKER,CLIENT,{**PINS,r'C:\elsewhere\x.dll':'0'*64},r'ROOT\MEDIA\0000')):
            with self.subTest(args=args[:2]):
                with self.assertRaises(ValueError): BrokerInstallation(*args)

    def mocks(self):
        bundle = Mock(is_verified=True, cleanup_confirmed=True)
        host = Mock(cleanup_confirmed=True)
        verifier = Mock(); listener = Mock()
        return bundle,host,verifier,listener

    def test_only_observed_client_identity_used_with_protected_bundle_lifetime(self):
        bundle,host,verifier,listener = self.mocks()
        events=[]
        bundle.verify.side_effect=lambda:events.append('pin')
        host.serve.side_effect=lambda _:events.append('serve')
        bundle.close.side_effect=lambda:events.append('unpin')
        with patch('remontevoice.broker_composition.observe_broker_accounts',return_value=accounts()), \
             patch('remontevoice.broker_composition.WindowsProtectedBundle',return_value=bundle), \
             patch('remontevoice.broker_composition.WindowsPeerVerifier',return_value=verifier) as verify, \
             patch('remontevoice.broker_composition.NativePipeServer',return_value=listener) as listen, \
             patch('remontevoice.broker_composition.BrokerHost',return_value=host) as make_host, \
             patch('remontevoice.broker_composition.DriverMicWriter') as writer:
            owner=InstalledBrokerApplication(installation()); owner.serve(threading.Event())
            verify.assert_called_once_with('S-1-5-21-1000','S-1-5-5-1-21',1,{CLIENT.lower():'b'*64})
            self.assertEqual(listen.call_args.args[1],'S-1-5-5-1-21')
            writer.assert_not_called()
            self.assertTrue(make_host.call_args.args[2].allows(make_host.call_args.args[2].expected))
        self.assertEqual(events,['pin','serve','unpin'])
        self.assertTrue(owner.cleanup_confirmed)
        listener.close.assert_not_called();verifier.close.assert_not_called()

    def test_non_elevated_does_not_create_pipe_or_open_driver(self):
        with patch('remontevoice.broker_composition.observe_broker_accounts',return_value=accounts(False)), \
             patch('remontevoice.broker_composition.NativePipeServer') as listener, \
             patch('remontevoice.broker_composition.WindowsProtectedBundle') as bundle:
            owner=InstalledBrokerApplication(installation())
            with self.assertRaises(BrokerCompositionError): owner.serve(threading.Event())
            listener.assert_not_called();bundle.assert_not_called()
        self.assertTrue(owner.cleanup_confirmed)

    def test_failed_bundle_never_admits_and_unconfirmed_close_is_retained(self):
        bundle,_,_,_=self.mocks()
        bundle.verify.side_effect=RuntimeError('bad pin')
        bundle.close.side_effect=RuntimeError('still open')
        bundle.cleanup_confirmed=False
        with patch('remontevoice.broker_composition.observe_broker_accounts',return_value=accounts()), \
             patch('remontevoice.broker_composition.WindowsProtectedBundle',return_value=bundle), \
             patch('remontevoice.broker_composition.NativePipeServer') as listener:
            owner=InstalledBrokerApplication(installation())
            with self.assertRaises(RuntimeError): owner.serve(threading.Event())
            listener.assert_not_called()
        self.assertFalse(owner.cleanup_confirmed)
        self.assertIs(owner._bundle,bundle)

    def test_fatal_host_does_not_run_bundle_cleanup_and_unknown_session_blocks_clean_return(self):
        class Fatal(BaseException):pass
        for error in (Fatal(),RuntimeError('unknown session')):
            bundle,host,verifier,listener=self.mocks()
            host.serve.side_effect=error;host.cleanup_confirmed=False
            with patch('remontevoice.broker_composition.observe_broker_accounts',return_value=accounts()), \
                 patch('remontevoice.broker_composition.WindowsProtectedBundle',return_value=bundle), \
                 patch('remontevoice.broker_composition.WindowsPeerVerifier',return_value=verifier), \
                 patch('remontevoice.broker_composition.NativePipeServer',return_value=listener), \
                 patch('remontevoice.broker_composition.BrokerHost',return_value=host):
                owner=InstalledBrokerApplication(installation())
                with self.assertRaises(type(error)) as raised: owner.serve(threading.Event())
            self.assertIs(raised.exception,error)
            self.assertFalse(owner.cleanup_confirmed)
            bundle.close.assert_not_called()

    def test_pipe_name_uses_client_observation_from_both_link_directions(self):
        self.assertEqual(broker_pipe_name(accounts(True)),broker_pipe_name(accounts(False)))
        self.assertTrue(broker_pipe_name(accounts()).startswith('RemonteVoice-Mic-'))


class ClientCompositionTests(unittest.TestCase):
    def setUp(self):
        self.bundle=Mock(is_verified=True,cleanup_confirmed=True)
        self.verifier=Mock();self.pipe=Mock(cleanup_confirmed=True)
        self.client=Mock(cleanup_confirmed=True)
        self.patches=[
            patch('remontevoice.broker_composition.observe_broker_accounts',return_value=accounts(False)),
            patch('remontevoice.broker_composition.WindowsProtectedBundle',return_value=self.bundle),
            patch('remontevoice.broker_composition.WindowsPeerVerifier',return_value=self.verifier),
            patch('remontevoice.broker_composition.NativePipeClient.connect',return_value=self.pipe),
            patch('remontevoice.broker_composition.BrokerPipeClient',return_value=self.client)]
        self.mocks=[p.start() for p in self.patches]
        for p in self.patches:self.addCleanup(p.stop)

    def test_mutual_identity_uses_full_token_and_pins_running_client_before_connect(self):
        owner=InstalledBrokerClient(installation()).open()
        self.bundle.verify_current_executable.assert_called_once_with(CLIENT.lower())
        self.mocks[2].assert_called_once_with('S-1-5-21-1000','S-1-5-5-1-20',1,{BROKER.lower():'a'*64})
        self.client.set_microphone_enabled.assert_not_called()
        self.bundle.close.assert_not_called()
        self.assertIs(owner.observe(),self.client.observe.return_value)
        owner.close()
        self.assertTrue(owner.cleanup_confirmed)
        for resource in (self.client,self.pipe,self.verifier,self.bundle):resource.close.assert_called_once()

    def test_failed_handshake_retains_partial_client_until_retry_confirmed(self):
        from remontevoice.broker_pipe_client import BrokerClientOpenError
        partial=Mock(cleanup_confirmed=False)
        partial.close.side_effect=RuntimeError('pending')
        self.mocks[4].side_effect=BrokerClientOpenError(partial)
        owner=InstalledBrokerClient(installation())
        with self.assertRaises(BrokerClientOpenError) as raised:owner.open()
        self.assertIs(raised.exception.cleanup_owner,owner)
        self.assertFalse(owner.cleanup_confirmed)
        partial.close.side_effect=None;partial.cleanup_confirmed=True
        owner.close();self.assertTrue(owner.cleanup_confirmed)

    def test_wrong_thread_cannot_use_or_close_client(self):
        owner=InstalledBrokerClient(installation()).open();errors=[]
        def wrong():
            for operation in (owner.observe,owner.close):
                try:operation()
                except BrokerCompositionError:errors.append(True)
        worker=threading.Thread(target=wrong);worker.start();worker.join(1)
        self.assertEqual(errors,[True,True])
        self.client.close.assert_not_called();owner.close()

    def test_bad_running_image_blocks_pipe_and_releases_bundle(self):
        self.bundle.verify_current_executable.side_effect=RuntimeError('wrong executable')
        owner=InstalledBrokerClient(installation())
        with self.assertRaises(RuntimeError):owner.open()
        self.mocks[3].assert_not_called()
        self.bundle.close.assert_called_once()
        self.assertTrue(owner.cleanup_confirmed)

    def test_fatal_constructor_type_survives_failed_cleanup(self):
        class Fatal(BaseException):pass
        fatal=Fatal()
        self.mocks[4].side_effect=fatal;self.pipe.close.side_effect=RuntimeError('pending')
        owner=InstalledBrokerClient(installation())
        with self.assertRaises(Fatal) as raised:owner.open()
        self.assertIs(raised.exception,fatal)
        self.assertIs(fatal.cleanup_owner,owner)
        self.assertFalse(owner.cleanup_confirmed)
