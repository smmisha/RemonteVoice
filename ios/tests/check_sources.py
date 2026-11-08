"""Limited source inspection only; this does not parse/compile or execute Swift."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ios" / "RemoteVoice"
NAMES = ("PairingInfo", "BridgeClient", "BridgeModel", "PhoneAudioEngine")
sources = {name: (SOURCE / f"{name}.swift").read_text(encoding="utf-8") for name in NAMES}


def balanced_delimiters(source: str) -> bool:
    """Check delimiters outside ordinary strings/comments, not Swift grammar."""
    stack: list[str] = []
    pairs = {"(": ")", "[": "]", "{": "}"}
    index = 0
    comment_depth = 0
    in_string = False
    while index < len(source):
        current = source[index]
        following = source[index : index + 2]
        if comment_depth:
            if following == "/*":
                comment_depth += 1
                index += 2
            elif following == "*/":
                comment_depth -= 1
                index += 2
            else:
                index += 1
        elif in_string:
            if current == "\\":
                index += 2
            else:
                if current == '"':
                    in_string = False
                index += 1
        elif following == "//":
            newline = source.find("\n", index)
            index = len(source) if newline < 0 else newline + 1
        elif following == "/*":
            comment_depth = 1
            index += 2
        elif current == '"':
            in_string = True
            index += 1
        else:
            if current in pairs:
                stack.append(pairs[current])
            elif current in ")]}":
                if not stack or stack.pop() != current:
                    return False
            index += 1
    return not stack and not in_string and comment_depth == 0


client = sources["BridgeClient"]
model = sources["BridgeModel"]
audio = sources["PhoneAudioEngine"]
pairing = sources["PairingInfo"]
mic_operation = client.split("func setMicrophoneEnabled", 1)[1].split("func disconnect", 1)[0]
endpoint = pairing.split("private static func isPrivateIPv4", 1)[1]
tap = audio.split("private func convertMicrophoneBuffer", 1)[1]
completion = audio.split("player.scheduleBuffer(buffer)", 1)[1].split("\n    func stop()", 1)[0]
counter_locked = False
counter_native_calls = False
for line in audio.splitlines():
    if "playbackCounterLock.lock()" in line:
        counter_locked = True
    elif "playbackCounterLock.unlock()" in line:
        counter_locked = False
    elif counter_locked and any(native in line for native in ("engine.", "player.")):
        counter_native_calls = True
checks = {
    "four_swift_files_utf8_and_balanced_delimiters": all(
        not text.startswith("\ufeff") and balanced_delimiters(text) for text in sources.values()
    ),
    "endpoint_exact_ascii_octets_without_compactMap": (
        "omittingEmptySubsequences: false" in endpoint
        and "components.count == 4" in endpoint
        and "compactMap" not in endpoint
        and "component.utf8.allSatisfy" in endpoint
        and 'component == "0" || component.first != "0"' in endpoint
    ),
    "persisted_endpoint_revalidated": "guard Self.isPrivateIPv4(host)" in pairing,
    "client_and_model_main_actor": all(
        "@MainActor\nfinal class " in sources[name] for name in ("BridgeClient", "BridgeModel")
    ),
    "single_bounded_pcm_sender_no_dispatch_per_frame": (
        "maximumPendingFrames = 6" in client
        and "maximumAge: TimeInterval = 0.5" in client
        and "inFlight == nil" in client
        and "sendQueue" not in client
        and ".async {" not in client
        and client.count("socket?.send(") == 1
    ),
    "ack_before_capture_and_permission_identity_check": (
        mic_operation.index("try await requestMicrophoneState(enabled)")
        < mic_operation.index("try audio.startMicrophone")
        and "let permitted = await PhoneAudioEngine.requestMicrophonePermission()\n            try requireConnected()" in mic_operation
        and "4_000_000_000" in client
    ),
    "mute_discards_pcm_and_stops_capture_before_await": (
        mic_operation.index("writer.setCaptureEnabled(false)")
        < mic_operation.index("try audio.stopMicrophoneAndReturnToPlayback()")
        < mic_operation.index("try await requestMicrophoneState(enabled)")
    ),
    "terminal_invalidates_and_clears_transport": all(
        value in client for value in (
            "guard !terminal else { return }", "terminal = true", "writer?.close()",
            "socket?.cancel()", "session?.invalidateAndCancel()", "socket = nil", "session = nil",
            "pending?.continuation.resume(throwing: CancellationError())",
        )
    ),
    "model_generation_identity_and_task_cancellation": all(
        value in model for value in (
            "generation &+= 1", "client === candidate", "connectTask?.cancel()",
            "microphoneTask?.cancel()", "!Task.isCancelled, self.isCurrent(client, currentGeneration)",
        )
    ),
    "audio_permission_separate_and_start_unwinds": (
        "static func requestMicrophonePermission() async -> Bool" in audio
        and "func startMicrophone(onFrame: @escaping (Data) -> Void) throws" in audio
        and "let startupError = error" in audio
        and "try returnToPlaybackLocked()" in audio
        and "throw PhoneAudioError.playbackRecoveryFailed" in audio
        and "guard !stopped, captureInstalled, let converter" in audio
    ),
    "tap_never_waits_for_native_configuration_lock": (
        "guard lock.try() else { return }" in tap and "lock.lock()" not in tap
    ),
    "playback_completion_has_separate_counter_lock": (
        "self.playbackCounterLock.lock()" in completion
        and "self.lock.lock()" not in completion
        and not counter_native_calls
    ),
    "stale_pcm_dropped_before_dispatch_and_checked_again_at_send": (
        "discardStaleFramesLocked(now: ProcessInfo.processInfo.systemUptime)" in client
        and "while let first = frames.first, now - first.queuedAt > Self.maximumAge" in client
        and "inFlight?.id == current.id" in client
        and "!captureEnabled || ProcessInfo.processInfo.systemUptime - current.queuedAt > Self.maximumAge" in client
    ),
}
probe = ROOT / "ios" / "tests" / "PairingInfoProbe.swift"
probe_text = probe.read_text(encoding="utf-8")
checks["standalone_probe_delimiters_and_29_cases"] = (
    balanced_delimiters(probe_text)
    and len(re.findall(r'\("(?:\\.|[^"\\])*",\s*(?:true|false)\)', probe_text)) == 29
)
result = {
    "observed_at_utc": datetime.now(timezone.utc).isoformat(),
    "proof_level": "source-inspection-only",
    "swift": shutil.which("swift"),
    "swiftc": shutil.which("swiftc"),
    "swift_compilation": "not_run",
    "swift_probe": "not_run",
    "ios_runtime_tests": "not_run",
    "checks": checks,
    "sources": {
        str((SOURCE / f"{name}.swift").relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
            (SOURCE / f"{name}.swift").read_bytes()
        ).hexdigest()
        for name in NAMES
    },
}
destination = Path(__file__).with_name("source-check-results.json")
destination.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2, ensure_ascii=False))
sys.exit(0 if all(checks.values()) else 1)
