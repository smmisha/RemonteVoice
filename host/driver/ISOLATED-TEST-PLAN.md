# Isolated Windows target — existing test VM selected

## Current plan, 25 October 2026

The user cancelled installing a new Windows VM and requested reuse of existing
Windows. The newly downloaded 26H2 ISO and its project partial were deleted
after exact path/size and exclusive-file checks with zero VMs running. Existing
VM disks, tools and user files were retained. The original `Downloads/Windows.iso`
is referenced by existing configurations but no longer exists on disk; the
already installed guest Windows does not need that ISO for ordinary startup.

`Delo-Win10-22H2-test` has the snapshot `RemonteVoice-before-tests-20261025`, but
resumed to an "operating system wasn't found" error and was powered off. The
user then explicitly selected the primary `Delo-Win10-22H2` with a snapshot.
`RemonteVoice-before-main-tests-20261025` was confirmed before resuming it; the
primary Windows desktop and running VMware Tools were observed. Tools metadata
identifies Windows 10 Home 22H2 x64 build 19045.6456. Both disk chains passed
read-only consistency checks. The initial automatic approval rejection due to
RAM was followed by explicit user acceptance of slowdowns/audio interruptions.
No guest driver, certificate trust or boot-security changes have occurred.

See [WINDOWS10-LAB.md](WINDOWS10-LAB.md) for the separate experimental Windows 10
package and compatibility limits. Driver loading, guest security/test-signing
changes and runtime audio acceptance remain separate gates. No new OS download
is needed for this plan.

## Historical new-VM preparation — cancelled

This plan covers driver testing only. It does not authorize changing the working
Windows installation or deploying the prototype to the user's laptop audio stack.

### Observed prerequisites, 24 October 2026

- VMware Workstation 26.0.1 build 25688693 is installed. `vmrun -T ws list`
  succeeded outside the restricted tool sandbox; it reported zero running VMs.
  The earlier sandbox vmrun failure is not evidence of a broken installation.
- C: had about 234.1 GB free. The former desktop project path resolves to M:
  through a junction and cannot be used as a genuine C: destination.
- M: has about 27.6 GB free. Do not place the VM and snapshots on M:.
- Only about 2.2 GB physical RAM was free at preflight. Do not start a 4 GB guest
  during the current session. Recheck immediately before any authorized start.

These observations are time-dependent, not reservations or proof of VM readiness.

### Authorized preparation, resumed 25 October 2026

The user approved preparing VM files and the official ISO on C:, without boot.
The actual destination is `C:/Users/User/RemonteVoice-TestVM`. Its resolved
drive is checked before writing. The partial download on M: was copied with hash
verification and retained, then resumed on actual C:. Full ISO acceptance requires
the published Microsoft SHA-256 match; the download receipt records its outcome.

The new configuration and empty 80 GB thin disk live in `machine/`, with 2 virtual
CPUs, 4 GB RAM and EFI. The configuration has not been accepted by a boot. Network,
sound, USB, shared folders, clipboard and drag/drop are disabled for this target.
No OS, guest tools or driver was installed. Existing user VMs are untouched.

On 25 October the user explicitly authorized completing this new VM's security
setup through Computer Use. VMware now reports partial encryption and a present
Trusted Platform Module; the saved configuration confirms `vtpm.present = TRUE`
and EFI Secure Boot. A fresh VM-only password was generated in memory and entered
masked, with VMware's Credential Manager option selected. No plaintext password
was written to project files or logs. No experimental auto-add or TPM bypass was
used, and no host security settings or existing VMs were changed.

The VM remains powered off. The post-setup observation found zero running VMs
and only about 0.5 GB free physical RAM. Recheck resources before an authorized
start; the encrypted configuration has not been accepted by a guest boot.

Do not run the VM. Starting it and
installing/loading this driver remain separate acceptance steps. Never close the
user's applications to free RAM, reboot the host or change host features,
Secure Boot, test-signing, certificates, drivers, default audio or firewall.

## Before driver loading

1. Confirm the test target identity from within the guest and make a clean
   snapshot. Keep host project/configuration folders unshared; transfer only the
   fixed driver/test bundle, without account data, pairing keys or real audio.
2. Obtain explicit authority for driver loading and any **guest-only** development
   signing/security settings. Prepare the signed test catalog/package in that
   guest; the current `.sys`/INF/catalog are unsigned and are not install-ready.
3. Prepare a root-device installation method and recovery/removal procedure.
   Adding an INF package alone is not proof that the root device was created.
4. Confirm that ordinary Windows audio clients can access the virtual endpoints.
   The current development direct-KS ACL is restricted to SYSTEM/admins; production
   service access and Windows audio-service compatibility are still unaccepted.

## Acceptance in the guest

- Verify endpoint identity and actual format negotiation, capture/playback and
  WASAPI loopback. Use synthetic PCM initially, never automatically open a
  physical/phone microphone. Cross-language DLL tests do not replace this step.
- Validate BEGIN/WRITE/END/lease expiry and ownership through real PortCls, not
  the test shim. Check malformed messages, concurrency, expiry and silence.
- Test user-mode client hang/unplug/termination, device removal, reboot/resume
  and cleanup. Apply Driver Verifier/HLK as appropriate in the isolated target.
- Only after endpoint acceptance, integrate the existing Host route guard and
  test every input/output role and saved mute on normal/network/process failures.
- Confirm both directions without installed Voicemeeter, then clean update and
  removal. No public release, Store approval or production signature is implied.

The authenticated production broker, final Host adapter and signing/distribution
remain open. VM creation is preparation, not completion of REQ-037/REQ-038.

## Fixed local package identities

The original 66-file test ZIP is retained unchanged. It predates TPM setup and
catalog generation; its bundled preparation notes are historical. The new
unsigned package is in `host/build/driver-catalog-development`, with a SHA-256
manifest and successful Inf2Cat log. The existing approved WDK supports x64
Windows 11 identifiers `10_CO_X64`, `10_NI_X64` and `10_GE_X64`; this catalog
check does not establish compatibility with the downloaded 26H2 guest ISO.
No test certificate, trust-store change, signing or installation was performed.
DevGen must not be redistributed in the app or test bundle; a guest root-device
test method remains to be prepared under the separate installation permission.
