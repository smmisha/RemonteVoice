import json
import unittest
import ctypes as ct
from types import SimpleNamespace

from remotevoice.windows_broker_manifest import PinnedInstalledManifest, InstalledManifestError, _ManifestApi
from remotevoice.broker_package_manifest import MAX_MANIFEST_BYTES
from remotevoice.windows_broker_identity import _Ace, _FileInfo, _Security

ROOT = r'c:\program files\remotevoice'
FILES = {'RemoteVoiceBroker.exe':'a'*64,'RemoteVoice.exe':'b'*64,
         '_internal/python314.dll':'c'*64,'_internal/base_library.zip':'d'*64}
PAYLOAD = {'schema_version':1,'broker':'RemoteVoiceBroker.exe','client':'RemoteVoice.exe',
           'driver_instance_id':r'ROOT\MEDIA\0000','files':FILES}
TRUST = _Security('S-1-5-18',True,(_Ace(0,0,0x10000000,'S-1-5-32-544'),))


class Api:
    def __init__(self):
        self.opened={};self.closed=[];self.reads=[];self.close_fails=False
        self.image=ROOT+r'\remotevoice.exe';self.writable_dir=None
        self.extra_file=False;self.wrong_hash=False
        self.files=dict(FILES)
    def open_process(self,pid):
        handle=len(self.opened)+1;self.opened[handle]=('process',False);return handle
    def process_image_path(self,handle):return self.image
    def process_alive(self,handle):return True
    def installation_roots(self):return (r'C:\Program Files',)
    def open_image_component(self,path,*,directory):
        handle=len(self.opened)+1;self.opened[handle]=(path,directory);return handle
    def file_info(self,handle):
        path,directory=self.opened[handle];return _FileInfo(path,directory,False)
    def file_security(self,handle):
        if self.opened[handle][0]==self.writable_dir:
            return _Security('S-1-5-18',True,(_Ace(0,0,4,'S-1-1-0'),))
        return TRUST
    def read_manifest(self,handle):
        self.reads.append(self.opened[handle][0]);return json.dumps({**PAYLOAD,'files':self.files}).encode()
    def children(self,path):
        if path==ROOT:
            result=[('RemoteVoiceBroker.exe',False),('RemoteVoice.exe',False),
                    ('remotevoice-installation.json',False),('_internal',True)]
            if self.extra_file:result.append(('extra.dll',False))
            return result
        return [('python314.dll',False),('base_library.zip',False)]
    def hash_image(self,handle):
        path=self.opened[handle][0]
        return '0'*64 if self.wrong_hash else {ROOT+'\\'+p.replace('/','\\').lower():v for p,v in self.files.items()}[path]
    def close_handle(self,handle):
        if self.close_fails:raise OSError('retained')
        self.closed.append(handle)


class InstalledManifestTests(unittest.TestCase):
    def test_complete_installed_manifest_pins_files_and_keeps_owner_until_close(self):
        api=Api();owner=PinnedInstalledManifest('client',api=api)
        self.assertEqual(api.opened,{})
        installation=owner.load()
        self.assertTrue(owner.is_verified)
        self.assertFalse(owner.cleanup_confirmed)
        self.assertEqual(installation.client_executable,api.image)
        self.assertEqual(len(installation.trusted_files),4)
        self.assertEqual(api.reads,[ROOT+r'\remotevoice-installation.json'])
        self.assertEqual(api.closed,[])
        owner.close()
        self.assertFalse(owner.is_verified);self.assertTrue(owner.cleanup_confirmed)
        self.assertEqual(len(api.closed),len(api.opened))
        with self.assertRaises(InstalledManifestError):owner.load()

    def test_workspace_or_wrong_executable_fails_before_manifest_read(self):
        for image in (r'C:\Python314\python.exe',r'C:\Users\User\RemoteVoice.exe'):
            api=Api();api.image=image;owner=PinnedInstalledManifest('client',api=api)
            with self.assertRaises(InstalledManifestError):owner.load()
            self.assertEqual(api.reads,[]);self.assertTrue(owner.cleanup_confirmed)

    def test_unexpected_file_wrong_hash_and_mutable_install_directory_rejected(self):
        for attr,value in [('extra_file',True),('wrong_hash',True),('writable_dir',ROOT),
                           ('writable_dir',ROOT+r'\_internal')]:
            api=Api();setattr(api,attr,value);owner=PinnedInstalledManifest('client',api=api)
            with self.subTest(attr=attr,value=value):
                with self.assertRaises(InstalledManifestError):owner.load()
                self.assertFalse(owner.is_verified);self.assertTrue(owner.cleanup_confirmed)

    def test_partial_failure_retains_native_handles_for_explicit_retry(self):
        api=Api();api.wrong_hash=api.close_fails=True
        owner=PinnedInstalledManifest('client',api=api)
        with self.assertRaises(InstalledManifestError) as raised:owner.load()
        self.assertIs(raised.exception.cleanup_owner,owner)
        self.assertFalse(owner.cleanup_confirmed)
        api.close_fails=False;owner.close();self.assertTrue(owner.cleanup_confirmed)

    def test_extra_empty_directory_and_case_alias_directory_are_rejected_before_scan(self):
        for entries in ([('_extra',True)], [('_internal',True)], [('_INTERNAL',True)]):
            api=Api();original=api.children;scanned=[]
            def children(path):
                scanned.append(path)
                if path==ROOT:return original(path)+entries
                if path==ROOT+r'\_extra':return []
                return original(path)
            api.children=children
            owner=PinnedInstalledManifest('client',api=api)
            with self.subTest(entries=entries):
                with self.assertRaises(InstalledManifestError):owner.load()
                self.assertFalse(owner.is_verified)
                self.assertTrue(owner.cleanup_confirmed)
                self.assertEqual(scanned,[ROOT])

    def test_legitimate_nested_runtime_directories_are_fully_checked_and_pinned(self):
        api=Api();api.files['_internal/pkg/helper.py']='e'*64
        original=api.children;scanned=[]
        def children(path):
            scanned.append(path)
            if path==ROOT+r'\_internal':return original(path)+[('pkg',True)]
            if path==ROOT+r'\_internal\pkg':return [('helper.py',False)]
            return original(path)
        api.children=children
        owner=PinnedInstalledManifest('client',api=api)
        installation=owner.load()
        self.assertTrue(owner.is_verified)
        self.assertIn(ROOT+r'\_internal\pkg',scanned)
        self.assertIn(ROOT+r'\_internal\pkg\helper.py',installation.trusted_files)
        self.assertFalse(owner.cleanup_confirmed)
        owner.close();self.assertTrue(owner.cleanup_confirmed)

    def test_role_cannot_accept_the_other_dedicated_executable(self):
        api=Api();owner=PinnedInstalledManifest('broker',api=api)
        with self.assertRaises(InstalledManifestError):owner.load()
        self.assertEqual(api.reads,[])
        self.assertTrue(owner.cleanup_confirmed)

    def test_native_manifest_reader_bounds_bytes_and_returns_exact_handle_contents(self):
        for data in (b'{"schema_version":1}', b'x'*(MAX_MANIFEST_BYTES+1)):
            offset=[0]
            def read(handle,buffer,size,count,overlapped):
                self.assertEqual(handle,77)
                self.assertLessEqual(size,65536)
                block=data[offset[0]:offset[0]+size]
                ct.memmove(buffer,block,len(block));count._obj.value=len(block)
                offset[0]+=len(block)
                return 1
            api=_ManifestApi.__new__(_ManifestApi);api.kernel=SimpleNamespace(ReadFile=read)
            if len(data)<=MAX_MANIFEST_BYTES:self.assertEqual(api.read_manifest(77),data)
            else:
                with self.assertRaises(InstalledManifestError):api.read_manifest(77)
                self.assertEqual(offset[0],MAX_MANIFEST_BYTES+1)
