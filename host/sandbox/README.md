# Lightweight package check in Windows Sandbox

This is an alternative to starting Delo for **package-only negative checks**.
It does not install the driver or a protected broker, change ACLs/UAC/trust, open
audio endpoints, start a service, or prove the real driver path.

Prepare with the existing project interpreter:

```powershell
.\.venv\Scripts\python.exe host\prepare_windows_sandbox.py --package host\build\driver-pair-004\dist\RemonteVoicePair --output host\build\sandbox-smoke-001
```

The command only verifies the complete package inventory and creates a new
configuration/control/report directory. It never launches Sandbox or replaces an
earlier output. Inspect the generated `RemonteVoice-Smoke.wsb` before use.

The configuration uses 2048 MB, disables network, microphone, camera, clipboard,
printers and vGPU, and maps only the package/control as read-only plus a fresh
reports directory as writable. A guest script copies the package to its own
temporary directory, checks its hashes, observes scalar token facts and runs
three bounded rejection checks. It writes only `reports/result.json`; child
stdout/stderr are drained and discarded. The script refuses execution under a
username other than Sandbox's default `WDAGUtilityAccount`.

Before a run, check current RAM and `wsb list --raw`. Do not adopt or stop someone
else's Sandbox. Check both available physical RAM and commitment headroom:
the guest shares OS pages, so configured memory is not a requirement for that
much unused physical RAM. For this short lab, defer below 1024 MiB available
physical memory or 2560 MiB commitment headroom. These are project preflights,
not Microsoft system requirements. Bound the run and release its resources
immediately afterwards. Validate the scalar report and stop only the Sandbox
instance created for that run. Recheck the working Host, guard and mixer.
Each configuration is for one run: earlier reports are never overwritten. The
configuration itself does not shut down the guest; the run owner must stop it.

`account_token_type` and `linked_token_present` are observations, not acceptance
of the broker's exact Full/Limited account policy. If the observed account lacks
that policy, do not relax it to make a Sandbox test pass. Real protected peer,
synthetic driver PCM and recovery acceptance remain a separate approved Delo
test. Sandbox driver/testsigning/reboot compatibility is not established.

Observed on 26 October 2026 (local time), Sandbox client 0.8.107.0, paired build
`driver-pair-005`: all three rejection cases passed, child cleanup was confirmed,
and the guest and remote-session processes exited after the owned instance was
stopped. The observed token was `TokenElevationTypeDefault` (1), without a linked
token. This environment cannot exercise our current Full/Limited account policy;
no protected installation or synthetic driver/audio test was attempted there.

## Read-only driver-environment inspection

To prepare a separate metadata probe, without sharing either application or
driver files:

```powershell
.\.venv\Scripts\python.exe host\prepare_windows_sandbox.py --environment-only --output host\build\sandbox-environment-NEW
```

This configuration has no logon command. Its owner can use `wsb exec` in the
explicit guest `System` context without `wsb connect` or an interactive desktop.
Run only `C:\RemonteVoice-Control\inspect_environment.ps1`. The report contains
scalar observations of the live Code Integrity options, UAC setting, BCD read
result and audio services. Code Integrity is queried directly through the
documented `SystemCodeIntegrityInformation` structure; a registry or BCD value
alone is not proof of the running kernel's test-signing mode.

The probe never writes registry/BCD/trust settings, installs a driver or opens
audio. It refuses an unexpected control directory and creates a fresh report
only. Headless metadata runs use a separate short-run resource preflight: at
least 2560 MiB physical availability and 4096 MiB commitment headroom, with an
abort/teardown if physical availability after boot falls below 256 MiB. These
are operational project thresholds, not Microsoft requirements. Always record
the unique owned guest ID before launch, bind the report to its generated
`probe_id`, enforce command timeouts and stop that exact guest on every outcome.

Neither a successful BCD read nor a CI observation authorizes or proves a boot
setting change. A driver test requires its own bounded, reviewable guest plan.

The first headless run stopped in its wrapper before collecting a receipt; the
original exact failure cause was not captured. System-process start-time reads
were removed from its observer, and run 002 then completed: guest build 26100,
CI flags 8197 (kernel enforcement on, test-signing
off), EnableLUA=0, BCD CLI exit1, audio services running and PortCls present.
These facts do not prove that every possible boot-configuration path is
unsupported. The subsequent run 003, intended to inspect the WMI store too, was
aborted before guest execution: available host memory was measured at 5 MiB
after boot. Both failed/aborted guests and run 002 were stopped and process
teardown was verified. No setting, certificate or driver was changed.

The physical-memory preflight was increased from 512 to 2560 MiB after that
observation. The owner then explicitly deferred further Sandbox execution
(«Пока продолжай без Sandbox»). Do not launch further guests until that branch
is resumed. Current WMI/localized-error helper changes passed source tests but
have not run in a guest. Source/backend work continues independently.

Official references, checked 25 October 2026:

- [Configuration and memory](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-configure-using-wsb-file)
- [CLI lifecycle](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-cli)
- [Shared memory architecture](https://learn.microsoft.com/en-us/windows/security/application-security/application-isolation/windows-sandbox/windows-sandbox-architecture)
- [Code Integrity query and flags](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntquerysysteminformation)
