# Own-driver integration — current boundary

Current admission-only preparation: `broker_entry` accepts the exact flag
`--lab-muted-only`. It uses the same protected bootstrap and peer policy, disables
the writer factory before any driver acquisition, requests stop after 60 seconds,
and hard-exits an unresponsive lab process after 90 seconds. The normal entry point
keeps its existing behavior. `host/prepare_broker_admission.py` prepares an exact
file inventory and a hash-bound PowerShell worker; its default Preflight is read-only.
Run/Cleanup require separately approved physical-host staging and folder ACL scope.
See [HOST-MUTED-ADMISSION-PLAN.md](../driver/lab/HOST-MUTED-ADMISSION-PLAN.md).
Preparation and unit tests do not establish positive protected admission.

Implemented locally: `mic_broker.py` owns the writer lifecycle; Windows native
pipe and peer-verification adapters now exist, with worker/client framing,
status sequence and a bounded microphone sink. `DriverAudioBridge` can be injected
into the server through `audio_factory` and uses the sink rather than a generic
playback speaker. HostWindow rechecks cached readiness immediately before
mute/routing and uses its existing restore path on failure. Microphone readiness
waits off the WebSocket event loop and is not acknowledged before confirmation.

The installed Host remains unchanged and still uses its mixer. There is no
deployed dedicated broker/service, protected packaged executable admission test,
enabled driver choice in the UI, or real own-driver Host audio acceptance yet.
A test-double authentication callback is not production authentication.

`broker_composition.py` now has inert installation/application/client owners.
Both sides observe their actual linked UAC tokens and keep separate exact logon
identities. Only the Full elevated side can host; only its linked Limited side
can use the client composition. `windows_broker_bundle.py` checks protected
file/ancestor ACLs, hashes and reparse status, retains deny-write/delete file
handles, and checks the actual running executable before opening a pipe. Runtime
pins remain held while any session/connection cleanup is unknown. The installation
inventory can be supplied by trusted code; the fixed installed manifest/bootstrap
source stage is described below. Client executable integration is still pending.
These primitives do not establish what a generic interpreter loaded before verification.

Next source stage: `broker_package_manifest.py` supplies the bounded strict schema
and read-only build inventory generator. `windows_broker_manifest.py` reads the
fixed manifest beside the actual dedicated executable through protected retained
handles, verifies the exact file set and pins runtime files. Within the install
root even untrusted ADD_SUBDIRECTORY is rejected, so a new importable package
cannot appear after inspection. `broker_entry.py` retains this bootstrap owner
through application cleanup and is used by `host/broker_launcher.py`.
The client/Host source integration is now available for explicit lab mode, as
described below. Protected installed acceptance and loader
trust before Python application code remain open. A workspace-built broker is
not a deployed protected installation or a completed two-executable package.

`broker_host.py` now provides a single-use sequential accept/session loop: clean
rejections may reconnect, but unknown session/resource cleanup stops admission and
retains ownership. `broker_process.py` is the dedicated process exit boundary;
fatal failure or unknown cleanup forces exit 70 without further dispatch or
general cleanup callbacks. A factory failure before returning its owner is also
unknown. Only exact cached cleanup confirmation permits a normal return. These
are source building blocks, not a deployed executable/service or protected
composition. Tests include actual child-process hard exit; protected packaging,
account policy and real app/driver acceptance remain open.

## Trust and lifecycle

- `connect()` accepts no identity payload. A trusted server-side transport must
  obtain Windows user SID, logon SID, session and verified executable identity.
  An exact configured policy plus explicit admission callback must accept it.
  Unconfigured/failed authentication denies admission. The executable must be
  in a protected, verified installation; hashing a user-writable path is not enough.
- The broker creates an opaque per-connection capability and owns all driver
  session/nonce values. The transport must bind the capability to its original
  connection; it must not accept a copied capability from another pipe connection.
- Connecting is muted. Only an explicit authorized enable starts a fresh writer.
  Readiness follows BEGIN acknowledgement, not the enable request. Never mute or
  reroute the physical input while waiting for acknowledgement.
- The service worker calls `poll()` at most 50 ms apart. Missing client heartbeat
  expires ingress after 750 ms by default. Disconnection closes admission before
  bounded cleanup. Unconfirmed cleanup retains the writer and blocks replacement.
- UI reads cached status, never native I/O. Snapshot age over 750 ms, failed broker
  authentication, incorrect endpoints, pending cleanup or missing expected writer
  make the driver backend unavailable. Muted/idle operation requires no mic writer.
- Output/input/mute restoration remains exclusively in existing Host route/guard
  code. No second restore owner is introduced. The independent guard must also
  recover from Host death; service process death must invalidate readiness.

## Implemented Windows transport and remaining deployment gates

`windows_broker_pipe.py` creates a local-only named pipe with explicit SYSTEM/logon
ACL, remote-client rejection, first-instance protection and identification SQOS.
Client permissions omit FILE_CREATE_PIPE_INSTANCE. Overlapped timeouts retain
buffers/events/handles until cancellation completion is confirmed; an unknown
completion-query error is not treated as safe cleanup. Server disconnect waits
for close-response receipt/EOF because disconnect discards unread output.

`windows_broker_identity.py` verifies both sides from actual Windows process
and token handles; it does not trust SID/PID/path/hash fields sent over the wire.
The verifier checks user/logon/session, retained process identity and pinned
protected image/ancestor ACLs, canonical path, reparse rejection and handle-bound
hashing. It rechecks the last-message token on every request. Failed impersonation
reversion remains fatal even if independent cleanup also fails. Failed constructors
expose their cleanup owner so a sink cannot report abandoned native I/O as clean.

Deployment must pin dedicated protected executables and their runtime, not a
workspace Python interpreter (a pinned interpreter does not attest its scripts).
Same-user/logon handle borrowing and arbitrary same-user code injection are not
solved by a file hash. SYSTEM tokens without a logon SID are currently rejected;
an explicitly designed service-account policy is still required for SYSTEM hosting.
The current linked-UAC composition is a lab path for split-token administrators,
not support for standard-user accounts or a deployed SYSTEM service. It observes
TokenLinkedToken and enabled/deny-only administrator membership; it never requests
elevation, credentials, new logon or Windows security changes.

The bounded versioned frame format exposes only status, explicit mic enable,
480-frame float32 PCM, heartbeat and close. It does not expose raw KS commands, paths,
process launching, route changes or arbitrary driver handles through IPC. The
transport must enforce deadlines/size limits and close on malformed frames.

`broker_protocol.py` implements these control commands and connection-local
capability dispatch. Invalid size,
version, command, Boolean or nonfinite PCM closes ingress. ACK is acceptance only.
Status includes a service-poll sequence. An unchanged sequence cannot renew the
client's observation timestamp; receiving an ACK never implies driver readiness.

`DriverAudioBridge` checks a trusted backend's exact endpoint IDs before opening
system loopback and forwards phone mono PCM through `BrokerMicSink`. It never
opens a phone-voice playback speaker. Its startup is muted, controls wait for
observed readiness, stop cannot be undone by a late enable, and failed cleanup
retains resources. The default `AudioBridge` and UI selection remain unchanged.

Before enabling the new backend, implement the protected broker process/service
packaging and connect it to Host startup/stop. Source endpoint inventory and one
consistent broker-connection lifecycle now exist, as described below. The status source cannot claim
readiness from a test-double or from mere installed-device discovery. Integrate
its readiness into Host startup and all route roles/mute recovery; accept the real
application+driver path in isolation, including process death and reconnect.
Physical-host driver/service installation requires separately authorized deployment.

`windows_driver_endpoints.py` enumerates active render/capture endpoints and
checks their PnP ancestry against the configured exact root instance, hardware ID
and driver service. The strict GetId/SWD compatibility fallback is limited to the
layout previously exercised in the lab when the instance property is absent;
unknown layouts fail closed. Both endpoints are rechecked before publication.
This is metadata only; it never activates IAudioClient or changes defaults.
COM/property cleanup failures keep the inventory poisoned and its cached cleanup
confirmation false, including after its worker exits.

`DriverBackendRuntime` owns one muted `BrokerMicSink` before a phone connects.
Separate inventory/health workers publish cached readiness using the oldest
underlying inventory/broker/progress timestamp; UI reads cannot renew it. Each
phone session borrows an exclusive sink lease. Releasing it confirms mute before
another phone is admitted, while the runtime alone closes the actual pipe.
Inventory stalls, changed endpoint identities, failed release and late enable
cannot silently create a replacement connection. Native COM ownership must also
be confirmed before runtime stop reports cleanup. The existing Host UI does not
select this runtime; coordination with its global guard during an enable
transition remains an integration gate.

Tests cover the core, client/worker/sink/subclass with inert audio adapters and
Host handlers with inert routes. Actual Windows tests read tokens/file ACLs/hash,
reject an unprotected workspace peer over a real pipe, and verify real local pipe
roundtrips, exclusive creation, timeout and cleanup. Native worker/sink pipeline
tests substitute protected-image admission and the driver writer; they prove
transport/lifecycle behavior, not production client admission or driver audio.
Real protected application admission, service crash, end-to-end own-driver Host
audio and Windows 11 acceptance remain unverified.

The persistent-runtime native smoke ran three synthetic session leases through
one actual pipe/client connection, with confirmed mute between cycles and thirty
in-memory PCM frames. Protected admission, endpoint inventory and driver writer
were substituted. Separate read-only native checks observed a supported linked
account pair, verified protected system-file pins, rejected the workspace process
as a different pinned executable and completed endpoint inventory cleanup without
returning the configured pair. No returned pair is not a diagnosis of driver
absence and none of these checks is application/driver audio acceptance.

The next bounded VM/package acceptance is specified in
[`BROKER-APPLICATION-TEST-PLAN.md`](../driver/lab/BROKER-APPLICATION-TEST-PLAN.md).

## Paired package and lab Host integration

`host/RemoteVoicePair.spec` builds two executables from one dependency graph and
one shared COLLECT/runtime. `host/build_windows_pair.py --output NEW_DIRECTORY`
creates a new build, strict installation manifest and separate build report;
existing output is never overwritten. Any source change during build rejects the
result instead of labelling it current. No installer, signing or elevation runs.

`BootstrapBrokerClient` retains the fixed client manifest through inner client
cleanup. `InstalledDriverHost` retains another outer installation owner through
endpoint inventory and the entire runtime. The explicit, hidden lab Host mode
uses inert constructor injection and starts the controller on a worker before
opening the phone server or activating routes. Exact render/capture IDs bypass
legacy mixer pairing; legacy device choices remain stored separately. Its timer
can tolerate a bounded, fresh explicit mic transition; immediate route activation
still requires strict readiness. Stop restores routes first, closes the server
and its audio lease, then closes runtime/pins off the UI thread. Unknown cleanup
blocks replacement and prevents releasing the Host lease/recovery guard.

The restore-only `driver-route-recovery.json` is saved atomically before any own
route activation and read under Host ownership even if broker startup is broken.
It cannot authorize the backend. It supplements the existing Host restoration
methods; no independent route writer was introduced.

The hidden `--broker-lab-probe` mode exchanges only muted status/heartbeat by
default. Explicit `--broker-lab-synthetic` additionally tests tone PCM and captures
only the inventory-verified virtual microphone, reducing audio to scalar metrics.
Reports must be new files outside the installation; unknown native cleanup
requires process exit 70. The explicit synthetic probe now measures post-disable
silence after draining the capture buffer; continuing PCM despite muted status
fails. The source test oracle is verified with inert recorders; actual driver
post-disable silence through the packaged app remains a separate acceptance.

PERM-028 authorizes [`BROKER-VM-RUN-ACTIONS.md`](../driver/lab/BROKER-VM-RUN-ACTIONS.md).
The owner subsequently deferred VM execution because only 0.41 GiB of physical
RAM was free for a 3 GiB guest. That Delo branch has not resumed. Separate
package/read-only Sandbox runs subsequently completed, but protected peer/driver
deployment was not attempted there. A later Sandbox run hit the memory abort
threshold, and the owner explicitly deferred further Sandbox execution too.
One-click broker lifecycle/elevation/service delivery, loader trust and real
protected application/driver acceptance remain open before production selection.

## Cancellation and process exit, 26 October 2026

`request_stop()` is a nonblocking signal on the installed controller/runtime;
it performs no native cleanup or join. Host calls it before route restoration,
including while the startup worker is blocked. Previously the signal waited for
that worker to finish, allowing a cancelled manifest load to start the runtime.
Admission closes immediately; final cleanup still uses the existing owners.

Window closure has one five-second deadline which repeated close requests do
not renew. Destroying Tk alone cannot stop a stalled executor's interpreter-exit
hook. On deadline expiry or when native cleanup is reported as unconfirmed, the
process exits70 without disarming recovery or releasing the Host lease early.
Clean closure still uses the ordinary path. Source tests include real hidden Tk
children with stalled executors, both tracked and already-retired workers; guard
and lease behavior is observed using inert owners. These checks do not certify
actual driver or route recovery. Physical Host0.1.22 remains unchanged.

## Native API notes for the next implementation

- [CreateNamedPipeW](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-createnamedpipew)
  provides first-instance and overlapped operation flags. Use an explicit security
  descriptor and reject remote clients; do not fall back to weaker creation flags.
- [ImpersonateNamedPipeClient](https://learn.microsoft.com/en-us/windows/win32/api/namedpipeapi/nf-namedpipeapi-impersonatenamedpipeclient)
  uses the security context of the last message read. Authenticate after the
  bounded initial read, check its return value and reject on failure; never
  continue under the privileged server token after failed impersonation. Check
  the actual request identity against the bound peer before dispatch as well.
- [OpenThreadToken](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-openthreadtoken)
  with OpenAsSelf permits token inspection at SecurityIdentification level.
  Query only required identity fields, close the token and revert on every path.
- [GetNamedPipeServerProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-getnamedpipeserverprocessid)
  identifies the pipe server process. A PID is an identity input, not proof of
  trust: bind it to open process/token handles, service identity and the protected
  verified installation. The client performs this mutual check before its handshake.
