import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from remotevoice.broker_package_manifest import MANIFEST_NAME, build_manifest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('prepare_broker_admission',ROOT/'host/prepare_broker_admission.py')
builder=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


class AdmissionPreparationTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        self.package,self.output=self.root/'package with spaces',self.root/'prepared'
        self.package.mkdir()
        for name in ('RemoteVoice.exe','RemoteVoiceBroker.exe','_internal/base_library.zip','_internal/python314.dll'):
            path=self.package/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(name.encode())
        (self.package/MANIFEST_NAME).write_bytes(build_manifest(self.package,r'ROOT\MEDIA\0000'))

    def prepare(self,**options):
        return builder.prepare(self.package,self.output,label=options.pop('label','012'),
                               program_files=options.pop('program_files',r'C:\Program Files'),**options)

    def test_preparation_binds_full_package_and_inert_fixed_commands(self):
        path=self.prepare();plan=json.loads(path.read_text('utf-8'))
        receipt=json.loads((self.output/'prepared.json').read_text('utf-8'))
        self.assertEqual(plan['target'],r'C:\Program Files\RemoteVoice-AdmissionLab-012')
        self.assertEqual(plan['broker_args'],['--lab-muted-only'])
        self.assertEqual(plan['client_args'],['--broker-lab-probe'])
        self.assertFalse(plan['driver_install'] or plan['microphone_enable'] or plan['audio_route_changes'])
        self.assertFalse(receipt['installed'] or receipt['executed'])
        self.assertEqual(receipt['plan_sha256'],hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(set(plan['files']),{p.relative_to(self.package).as_posix() for p in self.package.rglob('*') if p.is_file()})
        self.assertEqual((self.output/'Invoke-HostAdmission.ps1').read_bytes(),builder.SCRIPT.read_bytes())
        self.assertEqual(list((self.output/'reports').iterdir()),[])

    def test_tampered_or_extra_package_file_rejected_before_preparation(self):
        for extra in (False,True):
            with self.subTest(extra=extra):
                target=self.package/('extra.bin' if extra else 'RemoteVoice.exe')
                original=target.read_bytes() if target.exists() else None
                target.write_bytes(b'changed')
                with self.assertRaises(ValueError):self.prepare()
                self.assertFalse(self.output.exists())
                if original is None:target.unlink()
                else:target.write_bytes(original)

    def test_existing_output_and_unsafe_target_labels_are_preserved_or_rejected(self):
        for label in ('../012','012/x','12','0124','012\n'):
            with self.subTest(label=label),self.assertRaises(ValueError):self.prepare(label=label)
        for program_files in ('relative',r'\\server\share',r'C:\Program Files\..\Windows'):
            with self.subTest(program_files=program_files),self.assertRaises(ValueError):self.prepare(program_files=program_files)
        self.output.mkdir();sentinel=self.output/'keep';sentinel.write_text('unchanged')
        with self.assertRaises(ValueError):self.prepare()
        self.assertEqual(sentinel.read_text(),'unchanged')

    @unittest.skipUnless(os.name=='nt','Requires Windows PowerShell 5.1')
    def test_real_powershell_preflight_accepts_current_copy_and_rejects_tampering(self):
        self.prepare(program_files=os.environ['ProgramFiles'])
        receipt=json.loads((self.output/'prepared.json').read_text('utf-8'))
        powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        def run(expected):
            return subprocess.run([str(powershell),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                '-File',str(self.output/'Invoke-HostAdmission.ps1'),'-ExpectedPlanHash',expected],
                capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
        success=run(receipt['plan_sha256'])
        self.assertEqual(success.returncode,0,success.stderr.decode(errors='replace'))
        self.assertTrue(json.loads(success.stdout)['prepared_only'])
        self.assertNotEqual(run('0'*64).returncode,0)
        script=self.output/'Invoke-HostAdmission.ps1'
        script.write_bytes(script.read_bytes()+b'\n# changed\n')
        self.assertNotEqual(run(receipt['plan_sha256']).returncode,0)
        self.assertFalse((self.output/'reports/run.json').exists())


if __name__=='__main__':unittest.main()
