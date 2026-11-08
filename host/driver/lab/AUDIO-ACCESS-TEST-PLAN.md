# Audio-access correction — approved for isolated Delo test

The user explicitly answered «Разрешить исправление и тест в Delo» on
2026-10-25. PERM-027 authorizes the exact isolated test below, including the
PDO ACL correction together with the private PCM kernel authorization guard.

The installed prototype started but both real WASAPI probes failed with
`0x80070005` before stream initialization. Its PDO ACL currently permits only
SYSTEM and administrators. This differs from Microsoft's SimpleAudioSample:
https://github.com/microsoft/Windows-driver-samples/blob/main/audio/simpleaudiosample/Source/Main/SimpleAudioSample.inx

This is evidence for an ACL compatibility hypothesis, not proof of the cause.
The guest also stopped answering VMware Tools; that cause is still unknown.
The original protective snapshot was restored before another test installation.

Prepared kernel change: `rv_mic_access.h` checks the actual originating subject
using a private SYSTEM/administrator DACL and `SeAccessCheck(UserMode)` before
every PCM BEGIN/WRITE/END/KEEPALIVE. Disabled administrator groups and restricted
tokens do not gain private injection rights merely by opening the audio filter.
The change compiles and links with `/W4 /WX`; actual kernel acceptance is pending.

Approved INF correction for this test:

- Current: `D:P(A;;GA;;;SY)(A;;GRGWGX;;;BA)`
- Proposed: `D:P(A;;GA;;;SY)(A;;GRGWGX;;;BA)(A;;GRGWGX;;;WD)(A;;GRGWGX;;;RC)`

The proposed descriptor restores standard audio filter read/write access for
Windows audio clients and restricted audio-engine tokens. The private injection
property keeps its narrower kernel access check. This changes the driver's
security boundary; automatic approval review rejected applying it without
specific permission. That permission has now been obtained as recorded above.

After approval: create a new separately identified local test package, sign it
offline, install only in Delo, test ordinary restricted-token PCM rejection and
administrator success, then real synthetic mic/loopback/lease behavior. Save
results outside the guest and restore its protective snapshot. Do not install
on the physical laptop or change its certificate stores or security settings.

Production broker authentication, Windows 11 acceptance and public distribution
remain separate gates regardless of this test's result.
