"""Prepare a hash-bound muted-broker test plan; never install or launch anything."""
from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import uuid

from remotevoice.broker_package_manifest import MANIFEST_NAME, build_manifest, parse_manifest

SCRIPT = Path(__file__).resolve().parent / 'driver/lab/Invoke-HostAdmission.ps1'


def prepare(package, output, *, label, program_files=None):
    if type(label) is not str or not re.fullmatch(r'[0-9]{3}', label):
        raise ValueError('Expected a three-digit build label')
    package, output = Path(package).absolute(), Path(output).absolute()
    if os.path.lexists(output) or output.parent.resolve() != output.parent:
        raise ValueError('Expected a new output under a real existing parent')
    encoded = (package / MANIFEST_NAME).read_bytes()
    manifest = parse_manifest(encoded)
    if parse_manifest(build_manifest(package, manifest.driver_instance_id)) != manifest:
        raise ValueError('Package differs from its manifest')
    if output.is_relative_to(package) or package.is_relative_to(output):
        raise ValueError('Package and preparation directory must be separate')
    program_files = program_files or os.environ.get('ProgramFiles')
    if (type(program_files) is not str or not re.fullmatch(r'[A-Za-z]:\\[^\r\n"<>|?*]+', program_files)
            or ntpath.normpath(program_files) != program_files):
        raise ValueError('Expected an absolute local Program Files directory')
    files = dict(manifest.files)
    files[MANIFEST_NAME] = hashlib.sha256(encoded).hexdigest()
    plan = dict(schema_version=1, operation_id=str(uuid.uuid4()), label=label,
        source=str(package), target=ntpath.join(program_files, 'RemoteVoice-AdmissionLab-' + label),
        files=files, broker_args=['--lab-muted-only'], client_args=['--broker-lab-probe'],
        soft_stop_seconds=60, hard_exit_seconds=90, driver_install=False,
        microphone_enable=False, audio_route_changes=False,
        script_sha256=hashlib.sha256(SCRIPT.read_bytes()).hexdigest())
    output.mkdir()
    (output/'reports').mkdir()
    (output/'Invoke-HostAdmission.ps1').write_bytes(SCRIPT.read_bytes())
    payload=(json.dumps(plan, sort_keys=True, indent=2)+'\n').encode('utf-8')
    (output/'plan.json').write_bytes(payload)
    receipt=dict(plan_sha256=hashlib.sha256(payload).hexdigest(),package_files=len(files),
        prepared_only=True,installed=False,executed=False,operation_id=plan['operation_id'])
    (output/'prepared.json').write_text(json.dumps(receipt,indent=2)+'\n','utf-8')
    return output/'plan.json'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--label',required=True)
    args=parser.parse_args()
    print(prepare(args.package,args.output,label=args.label))


if __name__ == '__main__':
    main()
