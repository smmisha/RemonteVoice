# iPhone reliability checks

This folder contains isolated checks, outside the application target. It does not change Xcode project settings or sign/install the app.

## What ran on Windows

- `check_sources.py`: UTF-8/source delimiter checks and explicit structural safety assertions on the four modified Swift files. This is source inspection, **not Swift parsing, compilation, or an iOS runtime test**.
- Swift compiler discovery (`swift`, `swiftc`, conventional Windows install paths): unavailable on this machine.
- Review of ownership, suspended permission/ACK callbacks, terminal cleanup, bounded sender and audio rollback paths.
- The real-time tap uses a nonblocking configuration-lock attempt and drops a contended buffer. Player completions use a separate counter lock; native engine/player calls run outside that counter lock. This removes the identified source-level wait cycle; native deadlock/stress behavior remains untested.

`source-check-results.json` records the exact files and hashes inspected. Findings A04/A05/A06/A07/A12 have source patches; build/device acceptance is still open.

## Standalone endpoint probe — not executed here

On a machine with Swift and Foundation, from the repository root:

```sh
swiftc ios/RemonteVoice/PairingInfo.swift ios/tests/PairingInfoProbe.swift -o /tmp/remontevoice-pairing-probe
/tmp/remontevoice-pairing-probe
```

The probe exercises the actual PairingInfo implementation against 29 accepted/rejected endpoints and the persisted-data URL path. It has no network/audio/account access. Do not report it as passed until it actually executes.

## Required before iPhone acceptance

1. Build all app sources with the actual iOS 17 SDK and configured Swift language version. Source checks cannot detect type/SDK availability errors.
2. Fake delayed writer completions: at most one send, six pending 1,921-byte PCM messages and one bounded control; partial progress/drop-oldest; stale PCM discarded before dispatch and rechecked at actual send; blocked writer cancellation; no pending voice after mute; no duplicate failure callback.
3. Fake mic ACK: capture starts only after true ACK; unmatched ACK ignored; missing ACK after four seconds closes transport; mute stops capture before awaiting false ACK.
4. Controlled permission suspension: disconnect A, connect B, then return A permission/send/receive callbacks; neither B model state nor shared AVAudioSession may change.
5. Inject errors at audio category, voice processing, converter construction and engine.start; verify tap removal, converter reset, playback recovery or terminal transport shutdown, then repeated mute/disconnect.
   Run concurrent tap/completion callbacks during engine.stop/removeTap/voice-processing changes. Verify the tap drops rather than waits, counters remain generation-correct, and no callback waits for the configuration lock.
6. Repeated auth/send/receive failures: URLSession invalidated, tasks cancelled, references cleared, terminal callbacks emitted once and weak client references released.
7. Physical iPhone playback/capture, route change, interruptions, background/lock, speaker/headphones and broken-network recovery. A local send completion is **not** proof of Host playback or end-to-end delivery. Closing a transport cannot guarantee immediate Host detection of a network blackout.

## API references checked

- [Apple: microphone permission async API](https://developer.apple.com/documentation/avfaudio/avaudioapplication/requestrecordpermission(completionhandler:)) — permission waits for the user's decision.
- [Apple: WebSocket send completion](https://developer.apple.com/documentation/foundation/urlsessionwebsockettask/send(_:completionhandler:)) — completion reports send error/success; it is not an application playback ACK.
- [Apple: invalidateAndCancel](https://developer.apple.com/documentation/foundation/urlsession/invalidateandcancel()) — cancels tasks and invalidates the session, breaking delegate/callback references.
- [Apple: NSRecursiveLock.try](https://developer.apple.com/documentation/foundation/nsrecursivelock/try()) — attempts acquisition and returns immediately.
