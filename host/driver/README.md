# RemonteVoice audio driver — development only

This is a development prototype derived from Microsoft's
`audio/simpleaudiosample`, pinned at
`bb68ed5e770bce04dc5395ec9258dafab034184d` in
[Windows-driver-samples](https://github.com/microsoft/Windows-driver-samples/tree/bb68ed5e770bce04dc5395ec9258dafab034184d/audio/simpleaudiosample).
Microsoft's notices remain in the source; see [LICENSE-MS-PL.txt](LICENSE-MS-PL.txt).
The original sample projects and `.inx` remain for provenance. Only `build.py`
is the supported development build entry point here.

On 2026-10-25 a separately test-signed package passed synthetic microphone,
shared render/loopback and per-request authorization checks in the existing
Windows 10 x64 Delo VM. It was then removed successfully. The physical Windows
host has no installation of this driver; production integration and Windows 11
acceptance remain open. See [the lab record](lab/README.md).

## Implemented boundary

- A virtual speaker from the sample and a capture endpoint adapted to 48 kHz,
  PCM16 mono, with DEFAULT, COMMUNICATIONS and RAW mode declarations.
- Capture reads `RvPcmBridge`, not the sample's tone generator. Render packets
  are discarded by the virtual hardware; the planned app uses WASAPI loopback
  to read the virtual speaker. Synthetic shared loopback passed in the lab guest.
- A fixed 1000-byte KS SET message, with no embedded pointers. Its payload is
  one 10 ms microphone packet (480 mono PCM16 samples). Commands: BEGIN, WRITE,
  END and KEEPALIVE; version 1, nonzero session and 16-byte nonce required.
- A fixed nonpaged 100 ms FIFO, guarded by a spin lock. Overflow rejects the
  packet; underflow produces silence. END/reset wipes queued PCM. Queued audio
  expires after 200 ms and ownership after 2 seconds without a valid request.
- PID/session/nonce bind the lease. **This is not a production authorization
  design.** The audio PDO permits standard Windows audio-engine access. Each
  private PCM command separately checks SYSTEM/admin rights using
  `SeAccessCheck(UserMode)`, including restricted/deny-only token behavior.
  Real guest tests denied every command through both new and borrowed handles
  under a restricted token. An authenticated service and cross-user/session
  access review remain required for production.
- `savedata.cpp` and `ToneGenerator.cpp` are excluded from the explicit object
  list. No PCM file sink is used. The build rejects file-sink/user-CRT imports.
- Render startup returns the endpoint installation result. Optional ETW discovery
  cannot overwrite that result. The sample's dummy power requests and temporary
  registration of the caller's thread as a streaming resource have been removed.
  Actual startup and audio transfer passed in the isolated Windows 10 guest.

The running Host still uses Voicemeeter. A source-only user-mode KS writer now
exists in `host/remontevoice/driver_mic.py` and `windows_driver_transport.py`; it
is not selected by the Host. The production service, Windows 11 acceptance,
release signing and installer remain open. Do not select this as a ready backend.

## User-mode microphone client

`DriverMicWriter` converts one 480-sample float32 mono packet to PCM16 and writes
the fixed wire message on its own thread. Construction opens no device. BEGIN
must follow the user's microphone action, readiness follows its acknowledgement,
and every activation has a fresh identity. Queued voice is bounded to 10 packets
and expires after 100 ms. A full driver FIFO drops that packet without ending
the session. END is attempted even if BEGIN's acknowledgement was lost.

Native calls remain synchronous. A watchdog marks a stalled call unavailable;
bounded stop reports a still-live worker, keeps its handle/buffers owned by that
worker and does not claim cleanup. A failed close retains the transport and
reports unconfirmed cleanup. This detects a hang; it cannot cancel a defective
kernel driver. Connecting this health signal to the existing Windows route guard
is still required before selecting this backend.

`WindowsDriverMicTransport` discovers the exact root hardware ID and wave-filter
reference through SetupAPI, requires one match and refuses ambiguous metadata.
It does not elevate or use a display name as device identity. Commands and close
must run on the handle's owner thread. This is a development direct-KS client,
not a privileged broker or an accepted cross-user authorization boundary.

## Local build and tests

Existing MSVC 14.44.35207 and project-local WDK/SDK NuGet 10.0.26100.6584
are required. The approved tools were downloaded from Microsoft's NuGet feed
and their complete package signatures verified using official NuGet 6.14.0.
`toolchain-manifest.json` records archive identities. Tools live under ignored
`.tools/`; source remains local. The default build does not install or sign a
driver. Separate `lab/` helpers can sign and install a private guest test payload
only within the explicitly authorized isolated test plan. They do not install
the driver or change trust/security settings on the physical host.

From the repository root:

```powershell
.venv/Scripts/python.exe host/driver/build.py
& ./host/driver/test.ps1
$env:PYTHONPATH='host'
$env:RV_REQUIRE_DRIVER_TEST_DLL='1'
.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_driver_mic*.py' -v
```

Output: `host/build/driver-development/RemonteVoiceAudio.sys`, INF, import list,
compiler/linker/verifier logs and SHA-256 input/build manifest. The INF refers
to `RemonteVoiceAudio.cat`; passing `infverif /u` does not create that catalog or
certify the driver. A separate local preparation generated an unsigned catalog
in `host/build/driver-catalog-development`, with an Inf2Cat log and hash manifest.
Its x64 targets are `10_CO_X64`, `10_NI_X64`, `10_GE_X64`; actual guest/26H2
acceptance remains open. A separate private test-signed package exists under
ignored `host/build/driver-vm-lab-audiopath`; its key stays in protected ignored
evidence and is never part of transferred or committed files. It is not release
signing or an installer for the physical host.
`/W4 /WX` remains enabled; only intentional WDF C4324 alignment is suppressed.

`test.ps1` compiles **the actual `rv_pcm_bridge.cpp`** in user mode with small
substitutes for clock, IRQL, spin lock and request metadata. It checks PCM,
silence, lease/nonce ownership, ring wrap/full behavior, END/reset, expiry,
malformed sizes/commands and sample alignment. It does not emulate PortCls,
prove kernel synchronization, validate ACLs or substitute for device tests.
It also builds a test-only DLL and an input/hash manifest. Python/native tests
load that DLL only when its source hashes match. They exercise the actual bridge
with Python-generated wire messages; no driver is loaded and no Windows audio
endpoint is opened. Full Python verification passed 443 tests, including 36 new
client/identity/native checks. Restricted-sandbox asyncio socketpair stalled;
the full suite passed outside that restriction without changing audio code.

VM preparation is separately described in [ISOLATED-TEST-PLAN.md](ISOLATED-TEST-PLAN.md).
The current lab target is the user's existing Windows 10 test VM. Its separate
experimental INF/catalog preparation is described in [WINDOWS10-LAB.md](WINDOWS10-LAB.md);
the source Windows 11 INF and working Host are not changed by that preparation.

## Acceptance before switching the app

1. Implement and test user-mode KS ingress, service authentication and crash
   recovery. Confirm the runtime format and capture topology, including shared
   WASAPI/Discord, concurrent consumers, power transitions and restart.
2. Use a separately authorized, isolated Windows target and a snapshot. Loading
   a development driver or changing its security configuration is not authorized
   by permission to download build tools. Do not test-install it on the user's
   working laptop or interrupt the existing session.
3. Run Driver Verifier/HLK as applicable, with malformed/concurrent requests,
   process death, unload, network loss, sleep and memory/IRQL/authorization checks.
   Verify both real PCM directions without echo or Voicemeeter installed.
4. Integrate the existing route guard only after endpoints and stream readiness
   are confirmed. Verify all Windows input/output roles and original mute return
   after disconnect and every failure; the phone microphone requires user action.
5. Resolve production signing, distribution, update/removal, license notices and
   applicable Store acceptance. None is established by this local build.

REQ-037/REQ-038, TASK-053 and clean-Windows acceptance remain open.
