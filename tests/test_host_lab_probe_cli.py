import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from remotevoice.host_app import main


class LabProbeCliTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.folder.cleanup)
        self.output=Path(self.folder.name).resolve()/'report.json'

    def test_muted_and_explicit_synthetic_probe_bypass_gui_and_write_only_scalar_report(self):
        for synthetic in (False,True):
            output=self.output.with_name('synthetic.json' if synthetic else 'muted.json')
            args=['--broker-lab-probe','--broker-lab-report',str(output)]
            if synthetic:args+=['--broker-lab-synthetic']
            with patch('remotevoice.broker_lab_probe.run_probe',return_value={'success':True,'cleanup_confirmed':True}) as run, \
                 patch('remotevoice.host_app.HostWindow') as window:
                with self.assertRaises(SystemExit) as ended:main(args)
            self.assertEqual(ended.exception.code,0)
            run.assert_called_once_with(synthetic_pcm=synthetic)
            window.assert_not_called()
            self.assertEqual(json.loads(output.read_text('utf-8')),{'success':True,'cleanup_confirmed':True})

    def test_existing_report_is_never_overwritten_and_gui_flags_are_rejected(self):
        self.output.write_text('original','utf-8')
        with patch('remotevoice.broker_lab_probe.run_probe') as run:
            for args in (['--broker-lab-probe','--broker-lab-report',str(self.output)],
                         ['--broker-lab-synthetic'],
                         ['--broker-lab-probe','--safety-guard']):
                with self.assertRaises(SystemExit) as ended:main(args)
                self.assertEqual(ended.exception.code,2)
            run.assert_not_called()
        self.assertEqual(self.output.read_text('utf-8'),'original')

    def test_unknown_native_cleanup_writes_report_then_requires_hard_process_exit(self):
        with patch('remotevoice.broker_lab_probe.run_probe',return_value={'success':False,'cleanup_confirmed':False}), \
             patch('remotevoice.host_app.os._exit',side_effect=RuntimeError('test exit boundary')) as hard_exit:
            with self.assertRaisesRegex(RuntimeError,'test exit boundary'):
                main(['--broker-lab-probe','--broker-lab-report',str(self.output)])
        hard_exit.assert_called_once_with(70)
        self.assertFalse(json.loads(self.output.read_text('utf-8'))['cleanup_confirmed'])

    def test_report_cannot_add_an_unmanifested_file_to_the_frozen_installation(self):
        with patch('remotevoice.host_app.sys.frozen',True,create=True), \
             patch('remotevoice.host_app.sys.executable',str(self.output.parent/'RemoteVoice.exe')), \
             patch('remotevoice.broker_lab_probe.run_probe') as run:
            with self.assertRaises(SystemExit) as ended:
                main(['--broker-lab-probe','--broker-lab-report',str(self.output)])
        self.assertEqual(ended.exception.code,2);run.assert_not_called()
