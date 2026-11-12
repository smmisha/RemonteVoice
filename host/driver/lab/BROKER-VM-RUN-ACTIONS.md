# Proposed isolated broker/application run

Status: this exact guest application/ACL scope was approved by the owner as
PERM-028. The owner resumed guest testing after freeing memory on 26 October,
then confirmed that Delo was deleted. This plan retains its original target
and approved test boundaries; it does not authorize a replacement VM.
The replacement proposal is `REPLACEMENT-VM-PLAN.md`.
Earlier PERM-026/027 cover the signed lab
driver and its kernel tests. No additional authority comes from this document.

The original, now unavailable target was the Delo-Win10-22H2 guest and its
RemonteVoice-before-main-tests-20261025 snapshot. No other VM/OS download, physical
host installation or Windows host protection change is included.

## Bounded effects

1. Check VM identity, current memory, snapshot and power state before starting.
   Copy only the explicit, hash-bound RemonteVoicePair build into
   `C:\Program Files\RemonteVoice-Lab` in the guest. Preserve the exact source and
   build manifest; a repaired build may be substituted only with a fresh manifest
   and unchanged test scope. No private signing key, account password or unrelated
   source/configuration is transferred.
2. Set owner/ACL only on this new guest test directory and its descendants:
   SYSTEM/Administrators own and can maintain it, ordinary users may read/execute
   it. No broad Windows permission, firewall, antivirus, UAC or host setting is
   changed. No new Windows service, account or automatic login is created.
3. Launch the dedicated broker with the actual elevated linked token and the
   dedicated client with its actual non-elevated linked token. Use only the test
   user's own Windows token linkage. Do not weaken identity checks, use another
   account, or bypass an unsupported token policy. No authentication dialog is
   automated. Initial checks exchange only muted heartbeat/status over native IPC.
4. If muted protected admission succeeds, reuse the already accepted signed lab
   driver and public test certificate under the existing driver-test conditions.
   Trust/testsigning/reboot/reinstall remain guest-only and use the same approved
   driver security boundary. Send explicitly synthetic PCM through the application
   client/broker; capture only the verified virtual test microphone. Never record
   the laptop or phone microphone. Check silence/release and reconnect/failure.
5. Exercise app/guard route recovery only inside the guest, preserving recorded
   endpoint roles and mute. Retain bounded scalar reports outside the VM; screen
   content, credentials and audio recordings are excluded.
6. End test processes, remove the exact test artifacts/driver, restore the named
   snapshot, verify guest baseline/trust/testsigning, and power it off. Recheck
   that the physical Host/guard/mixer stayed unchanged.

The current local packaging candidate is `host/build/driver-pair-010`; its source
and file hashes plus three negative EXE launches passed. Reverify its build
report and complete file inventory before any future guest run. Pair005 previously
passed workspace rejection and a separate 2-GiB Sandbox package smoke test.
Pair010 includes the verified lease-release continuity and health-publication
race fixes; pairs008 and009 are superseded for this source. The current full
source suite ran 780 tests: 779 passed and 1 symlink-privilege skip. Pair010 has not
been run inside a guest or against the actual driver.
That Sandbox has a Default token without a linked
Full/Limited pair, so protected peer and actual driver PCM acceptance remain
unrun under this plan. Its account policy must not be relaxed to make it pass.
The updated explicit synthetic probe also checks actual post-disable silence.
Read-only Sandbox environment005 completed and was stopped: test signing is off,
EnableLUA=0, and both BCD CLI and WMI failed to open the system store. The earlier
environment004 Boolean OpenStore result was incorrectly interpreted as a numeric
success code; its store-opened flag is invalid. No guest setting was changed.
The original VM was deleted by the owner. Do not reuse its historical path or
snapshot identity for a new guest. Obtain authority for the prepared replacement
plan before creating or installing that new guest.
This is development acceptance, not a public release. Additional source/test
repairs must be recompiled and reidentified before use; successful muted IPC does
not imply actual audio or production readiness.
