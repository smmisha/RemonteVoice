# Next own-driver application acceptance

Status: preparation only. No executable, service or driver from this stage has
been deployed to the physical laptop. The working Host/mixer remains selected.
Existing isolated-driver permissions and snapshot recovery obligations remain;
this file does not grant new installation or Windows security authority.

## Artifacts required before a guest run

1. Build separate onedir broker and client executables with the existing local
   PyInstaller. The broker entry point must use `run_broker_process`; it must not
   load arbitrary scripts, plugins, SID claims or driver paths from IPC/CLI.
2. Produce a complete installation inventory for both executables and all runtime
   files. Read its fixed protected location through a pinned native file handle,
   reject reparse paths and untrusted mutation ACLs, bound JSON/entry sizes and
   verify the exact file set. Do not treat a caller-supplied dictionary as this
   completed bootstrap. Include runtime/data files and account for DLL loading
   before application code; an EXE hash alone does not attest that runtime.
3. Stage only the explicit built artifact set in a dedicated guest Program Files
   lab directory. No shared folder or writable search path may supply code.
   Preserve immutable install handles until connection/session cleanup confirms.
4. Implement Host runtime startup/stop integration and its bounded microphone
   transition coordination. The existing guard remains the sole route-restoration
   owner. Do not expose a production own-driver UI choice based on metadata alone.

## Existing Delo guest sequence

- Confirm the existing named pre-test snapshot and copy exact artifact hashes.
  Check memory before powering on; never download another OS/VM.
- Observe actual linked Full/Limited token identities inside the guest. Preserve
  their exact logon SIDs even when different. If the linked account policy is
  unsupported, report it and stop that path; do not weaken it to same-user matching
  or invent a SYSTEM logon identity.
- Verify the running broker/client executable and all protected runtime pins.
  Accept a genuinely protected peer over native IPC; reject workspace, wrong
  executable, changed pin and wrong-account peers. Start muted and prove no
  driver writer was opened before an explicit synthetic test activation.
- Use the previously accepted signed lab driver/package, exact root instance and
  current endpoint inventory. Exercise explicit BEGIN/PCM/END through the actual
  application broker/sink; capture the virtual microphone with the existing
  synthetic frequency/silence probes. Use synthetic PCM, never the physical mic.
- Exercise three phone-session lifetimes using one client connection, explicit
  mute between sessions, reconnect, heartbeat loss, driver disappearance and
  broker/Host death. Verify actual driver silence/lease release and existing
  Windows output/input/default-role/mute recovery. A source-only test double does
  not pass these checks. Preserve a manually muted microphone state.
- Confirm playback and screen/video behavior with the packaged Host after the
  backend acceptance; automatic tests alone do not establish perceived audio or
  physical phone screen quality.
- End all lab processes, confirm handles/workers are gone, remove the exact test
  driver/package and lab artifact set, restore the named snapshot, verify guest
  baseline/trust/testsigning and power off. Recheck physical Host/guard/mixer.

Windows 11 driver, long-run and public-release/store acceptance remain separate.
Do not install on the physical laptop or change its protection as a shortcut.

## References informing the package boundary

- [Microsoft linked UAC logon sessions](https://learn.microsoft.com/en-us/troubleshoot/windows-client/networking/mapped-drives-not-available-from-elevated-command)
- [PyInstaller onedir/onefile operation](https://pyinstaller.org/en/stable/operating-mode.html)
- [PyInstaller runtime paths](https://pyinstaller.org/en/stable/runtime-information.html)
- [Microsoft DLL search security](https://learn.microsoft.com/en-us/windows/win32/dlls/dynamic-link-library-security)
