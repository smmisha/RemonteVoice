# Native duplex PCM prototype

This is a tested **in-process user-mode buffer DLL**, not an installed audio
driver, shared-memory driver protocol, WASAPI endpoint or replacement mixer.
The live Host does not load it. It makes no device, registry, network or file
calls. Its tests use generated PCM only.

Build with the already installed x64 Visual C++ tools and Windows SDK:

```powershell
./host/native_audio/build.ps1
./.venv/Scripts/python.exe host/native_audio/test_duplex.py host/build/native-audio-prototype/remotevoice_audio_buffer.dll -v
```

The build script does not install tools. Output must stay inside this project.
No WDK is needed for this user-mode prototype; the future Windows driver needs
its own toolchain, delivery/signing decision and isolated native acceptance.

## Contract

All exports use C linkage and the default x64 C calling convention. ABI version
is `1`; handles are process-local owned pointers. They must never be accepted
from an untrusted client or passed across processes. Caller buffers must remain
valid for the complete call. This is **not** a kernel/user memory interface.

- `rv_audio_create(generation)` returns a handle or null. Generation zero is
  invalid. Create a new handle for every session; there is no reset/reuse path.
- `rv_audio_write(handle, direction, generation, sequence, samples, count)`
  copies exactly one finite float32 packet, without normalization or gain.
- `rv_audio_read(handle, direction, generation, samples, count, &sequence)`
  returns one packet. A correctly sized buffer is zeroed for an empty, stale or
  closed read. Invalid pointers/counts/direction are caller errors; they are not
  written through. Success only means buffer delivery, not audible playback.
- `rv_audio_close(handle)` prevents subsequent reads/writes and is idempotent.
  An operation already in progress may complete. Join all workers before calling
  `rv_audio_destroy(handle)`. Destruction must not race with any call; using an
  arbitrary or destroyed handle is invalid.

Direction `0` is system audio, 48 kHz stereo, 480 frames/960 samples per packet,
with 24 packet slots (240 ms maximum queued PCM). Direction `1` is the phone
microphone, 48 kHz mono, 480 frames/samples per packet, with 10 slots (100 ms).
These prototype capacities do not change the running Host/Android buffers.

There must be **one producer and one consumer per direction**. Independent
directions can run concurrently. Release/acquire cursor publication protects
complete packets; no reader sees a partially written packet. A full ring rejects
the incoming packet, leaves accepted PCM intact and reports backpressure to its
owner. No blocking wait, allocation or logging occurs in read/write. Sequence is
opaque metadata preserved from the writer, not an authentication credential.

Return values: `0` delivered/accepted, `1` empty, `2` full, `-1` invalid arguments,
`-2` generation mismatch, `-3` closed. Only create/destroy allocate/free memory.
The two directions have separate storage and cannot feed each other.

## Still required

Connect an independently implemented and verified virtual render/capture driver
to real PCM. Define an authenticated, validated kernel/user transfer mechanism;
this pointer API must never be reused as an IOCTL payload. Implement endpoint
discovery and health checks, audio-clock pacing, mute/flush policy, bounded
format conversion if needed, cleanup and driver security. Integrate through the
Host backend boundary while keeping existing route snapshots and crash guard.
No system installation or Store readiness is claimed by these unit tests.
