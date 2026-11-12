# Private VMware driver lab

These are development helpers, not a product installer. Use only with the
explicitly approved VM and the protected pre-test snapshot in VM-TEST-ACTIONS.md.
No private signing material belongs in this directory or in transferred files.

`build.ps1` builds the guarded C# helper with the existing .NET Framework compiler.
`prepare_signed_payload.ps1` creates the local disposable signing payload;
`refresh_signed_helper.ps1` is a one-shot refresh and refuses an existing refresh
artifact. Inspect earlier effects before any further refresh.

`guest-setup.ps1` is a guest-only BCD CLI fallback. It checks the exact VMware BIOS
identity, OS build, administrator token, package hashes and public certificate.
It changes only guest certificate trust and TESTSIGNING. Invocation through the
existing authenticated Guest API succeeded; machine execution policy was not changed.

## Initial package — historical result

Observed on 2026-10-25: Windows 10 build 19045 guest rebooted with kernel test
signing active. Signed RemonteVoice driver installed as ROOT\MEDIA\0000 with
CMResult=0, problem=0 and DN_STARTED set; no restart required. These observations
do not establish Windows 11 compatibility, production acceptance or PCM quality.

`build_probes.ps1` builds the synthetic tests with the existing approved MSVC/SDK.
`run-guest-probe.ps1` checks the exact guest and binary SHA256 before bounded
execution through the already configured, read-only test-input share.

`pcm_probe.cpp` selects the exact RemonteVoice hardware/filter and PnP ancestry,
tests a 900 Hz signal, underflow/resume, END and lease expiry. `loopback_probe.cpp`
checks synthetic render/loopback and zero-render silence. Neither opens physical
microphones nor changes default audio routes. Both compile with `/W3 /WX`.

Actual guest runs found a localized friendly name and missing MMDevice instance
property; probe selection now uses verified MMDevice-ID/PnP ancestry. The corrected
tests selected the expected devices but failed with `0x80070005` before stream
initialization. PCM/loopback/lease acceptance therefore has NOT passed. Initial
file-transfer errors were reconciled: a later guest read confirmed the complete
original binary hash, and the read-only share was verified working after runtime
activation. The exact cause of the earlier transient copy failures is unknown.

The guest subsequently stopped answering Tools RPC and ordinary shutdown. The
saved pre-test snapshot was restored. The follow-up guest report confirms zero
RemonteVoice drivers/devices, zero matching test certificates in guest machine
Root/TrustedPublisher, and no TESTSIGNING BCD entry. The guest clock reverted to
the snapshot's earlier date; the host observation is dated 2026-10-25. Final
VMware listing confirms zero running VMs and no configured temporary shares.
VMware reports graphics incompatibility when resuming the saved memory state;
the cause of the earlier Tools interruption remains unproven. No new Windows
image or VM is needed.

## Corrected isolated test, 2026-10-25

After explicit PERM-027 approval, the standard audio PDO descriptor was restored
while `src/Main/rv_mic_access.h` independently authorizes every private PCM
command as SYSTEM/administrator. The fresh package is identified separately as
`host/build/driver-vm-lab-audiopath`. Kernel compilation/linking/InfVerif and all
three probe compilations passed with warnings treated as errors.

The corrected package passed actual guest tests:

- Microphone: all 100 PCM packets accepted, 48000 capture frames, peak 0.183105,
  900 Hz energy ratio 0.998017, no stream discontinuities. Underflow and END
  produced measured silence; capture resumed after underflow. Expired lease
  tokens were rejected and a new lease succeeded.
- Render/loopback: 48000 tone frames submitted, peak 0.18, no discontinuities;
  14880 frames of subsequent zero-render capture were silent.
- `acl_probe.cpp`: an administrator lease remained valid while a token with
  Administrators disabled opened the standard filter. All four private commands
  returned ERROR_ACCESS_DENIED on both newly opened and borrowed admin handles.
  Returning to the administrator token allowed KEEPALIVE/END on the original
  lease. Token and handle cleanup passed.
- Exact published INF identity was verified before removal. PnP device and
  package removal returned exit0; no present RemonteVoice devices, driver service
  or published INF remained.

The earlier access failure is retained as historical evidence, not a current
passing test. These short synthetic checks do not prove long-run/network quality,
process-death recovery, Windows 11 compatibility or production broker security.
The working Host still uses its existing mixer; this driver was tested only in
Delo. Final snapshot-restoration evidence is maintained in project Harness.
After this corrected test, an actual guest query confirms no driver/devices or
matching test trust, no TESTSIGNING BCD entry, and kernel test-signing disabled
(code-integrity query status0). The original snapshot is retained for recovery.

Current detailed evidence is in the ignored project harness. Existing Delo guest
password and local private signing key must never be printed, committed or shared.
