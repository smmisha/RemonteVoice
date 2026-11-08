# Next guest-only driver test — approval required

Target: the running `Delo-Win10-22H2` guest, Windows 10 Home 22H2 x64 build
19045.6456. Its pre-test state is preserved in
`RemoteVoice-before-main-tests-20261025`. The separate `-test` VM did not boot
Windows and is powered off. No new Windows installation/download is planned.

The current unsigned lab package is
`host/build/driver-windows10-lab-startupfix`. Its hash manifest binds the SYS,
derived INF, CAT, generator and source build. InfVerif and Inf2Cat succeeded.
This is preparation for an experimental Windows 10 test, not release acceptance.

## Proposed effects

1. Create a fresh disposable development certificate/key locally in the ignored
   project test area, without adding trust to the host. Sign the SYS, regenerate
   the catalog against the signed SYS, then sign the catalog. Keep private-key
   material local and exclude it from source control and transferred test files.
2. Prepare a self-contained guest preflight/device-test helper using the existing
   compiler and SDK. Require VMware guest identity before any installation,
   confirm the expected target/version/admin context and package hashes, and
   show the proposed device/service identifiers. Transfer only fixed test inputs
   and the public test certificate to the guest.
3. In the guest only, trust that public certificate for this development test and
   enable test-signed-driver loading, then restart the guest. The target uses
   BIOS firmware; no host Secure Boot, HVCI, antivirus or firewall changes are
   included. If an additional security change proves necessary, stop that step.
4. Register the root test device `ROOT\RemoteVoiceAudio` and install the signed
   package/service `RemoteVoiceAudio` in the guest. Use a lab SetupAPI helper;
   do not distribute Microsoft's non-redistributable DevGen in the app/bundle.
5. Check actual endpoint identity, format negotiation, shared WASAPI loopback,
   synthetic PCM capture, BEGIN/WRITE/END/lease/underflow behavior and user-mode
   failure handling. No physical or phone microphone is opened automatically.
   Initial checks use explicit endpoints without changing host default audio.
6. Check removal and recovery in the guest. Restore the saved snapshot when the
   test is finished unless the user asks to keep the lab state; retain the
   bounded technical results outside the guest first.

The current working RemoteVoice Host still uses its existing mixer. Selecting
this driver in the real app, production broker authentication, real host route
guard integration and Windows 11/public-release acceptance remain later gates.

## Authority boundary

The user approved reuse/resume of this VM and accepted low-memory performance
risk. Certificate trust, boot test mode and driver loading were not included in
those approvals. Obtain explicit authorization for the effects above before
performing them. Existing project-harness rules also require authority for
installation and material changes.
