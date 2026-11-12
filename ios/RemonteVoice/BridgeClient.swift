import CryptoKit
import Foundation
import Security

@MainActor
final class BridgeClient {
    var onConnected: (@MainActor (String) -> Void)?
    var onMicrophoneState: (@MainActor (Bool) -> Void)?
    var onDisconnected: (@MainActor (String?) -> Void)?

    private let pairing: PairingInfo
    private let audio = PhoneAudioEngine()
    private var session: URLSession?
    private var socket: URLSessionWebSocketTask?
    private var writer: BoundedSocketWriter?
    private var receiveTask: Task<Void, Never>?
    private var connectTimeoutTask: Task<Void, Never>?
    private var microphoneTimeoutTask: Task<Void, Never>?
    private var pendingMicrophone: MicrophoneRequest?
    private var connectStarted = false
    private var terminal = false
    private var isConnected = false
    private var microphoneEnabled = false
    private var changingMicrophone = false

    private struct MicrophoneRequest {
        let id: UUID
        let enabled: Bool
        let continuation: CheckedContinuation<Void, Error>
    }

    init(pairing: PairingInfo) {
        self.pairing = pairing
    }

    func connect() async throws {
        guard !connectStarted, !terminal else { throw BridgeError.connectionClosed }
        guard let url = pairing.webSocketURL else { throw PairingError.invalidLink }
        guard let credential = pairing.pairingPIN ?? CredentialStore.load(for: pairing.fingerprint) else {
            throw PairingError.hostRejectedPairing
        }
        try Task.checkCancellation()
        connectStarted = true
        let configuration = URLSessionConfiguration.default
        configuration.timeoutIntervalForRequest = 8
        // The delegate owns only the immutable pin; it cannot retain this client.
        let delegate = PinnedSessionDelegate(fingerprint: pairing.fingerprint)
        let session = URLSession(configuration: configuration, delegate: delegate, delegateQueue: nil)
        self.session = session
        let socket = session.webSocketTask(with: url)
        socket.maximumMessageSize = 16 * 1024
        self.socket = socket
        socket.resume()
        connectTimeoutTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 8_000_000_000) } catch { return }
            guard let self, !self.terminal, !self.isConnected else { return }
            self.finishTerminal(message: BridgeError.connectionTimeout.localizedDescription)
        }

        do {
            // No writer or microphone exists during this single authentication send.
            try await socket.send(.string(jsonString(["type": "auth", "credential": credential])))
            try checkCurrent(socket)
            while true {
                let message = try await socket.receive()
                try checkCurrent(socket)
                guard case .string(let text) = message,
                      let control = decodeControl(text) else {
                    throw PairingError.hostRejectedPairing
                }
                switch control["type"] as? String {
                case "auth_ok":
                    guard let token = control["credential"] as? String else {
                        throw PairingError.hostRejectedPairing
                    }
                    try CredentialStore.save(token, for: pairing.fingerprint)
                    try checkCurrent(socket)
                    try audio.startPlayback()
                    try checkCurrent(socket)
                    writer = BoundedSocketWriter(socket: socket) { [weak self] message in
                        Task { @MainActor [weak self] in
                            self?.finishTerminal(message: message)
                        }
                    }
                    isConnected = true
                    connectTimeoutTask?.cancel()
                    connectTimeoutTask = nil
                    receiveTask = Task { [weak self] in
                        guard let self else { return }
                        await self.receiveLoop(socket)
                    }
                    onConnected?(token)
                    return
                case "auth_error":
                    throw PairingError.hostRejectedPairing
                default:
                    continue
                }
            }
        } catch {
            finishTerminal(message: error.localizedDescription)
            throw error
        }
    }

    func setMicrophoneEnabled(_ enabled: Bool) async throws {
        try requireConnected()
        guard !changingMicrophone else { throw BridgeError.microphoneSwitchInProgress }
        guard enabled != microphoneEnabled else { return }
        changingMicrophone = true
        defer { changingMicrophone = false }

        if enabled {
            // Permission may outlive disconnect/reconnect. It cannot mutate AVAudioSession.
            let permitted = await PhoneAudioEngine.requestMicrophonePermission()
            try requireConnected()
            guard permitted else { throw PairingError.microphonePermissionDenied }
        }

        do {
            guard let writer else { throw BridgeError.connectionClosed }
            if !enabled {
                // Stop capture and discard unsent voice before any network await.
                writer.setCaptureEnabled(false)
                try audio.stopMicrophoneAndReturnToPlayback()
                microphoneEnabled = false
            }
            try await requestMicrophoneState(enabled)
            try requireConnected()
            if enabled {
                // Capture starts only after ACK of this live session's only request.
                try audio.startMicrophone { [weak writer] payload in
                    writer?.enqueueMicrophoneFrame(payload)
                }
                writer.setCaptureEnabled(true)
            }
            microphoneEnabled = enabled
            onMicrophoneState?(enabled)
        } catch {
            // An unconfirmed mute or failed audio rollback must release Host routing.
            finishTerminal(message: error.localizedDescription)
            throw error
        }
    }

    func disconnect() {
        finishTerminal(message: nil, notify: false)
    }

    private func requestMicrophoneState(_ enabled: Bool) async throws {
        try requireConnected()
        let requestID = UUID()
        try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
                pendingMicrophone = MicrophoneRequest(id: requestID, enabled: enabled, continuation: continuation)
                microphoneTimeoutTask = Task { [weak self] in
                    do { try await Task.sleep(nanoseconds: 4_000_000_000) } catch { return }
                    guard let self, self.pendingMicrophone?.id == requestID else { return }
                    self.finishTerminal(message: BridgeError.microphoneStateTimeout.localizedDescription)
                }
                writer?.sendControl(jsonString(["type": "mic_enabled", "enabled": enabled]))
            }
        }, onCancel: { [weak self] in
            Task { @MainActor [weak self] in
                guard let self, self.pendingMicrophone?.id == requestID else { return }
                self.finishTerminal(message: nil)
            }
        })
    }

    private func receiveLoop(_ socket: URLSessionWebSocketTask) async {
        do {
            while isConnected, !terminal {
                let message = try await socket.receive()
                try checkCurrent(socket)
                switch message {
                case .data(let data):
                    guard data.count == 1 + 480 * 2 * MemoryLayout<Float>.size,
                          data.first == 0x02 else {
                        throw PairingError.invalidAudioFrame
                    }
                    try audio.playSystemFrame(data.dropFirst())
                case .string(let text):
                    guard let control = decodeControl(text) else { continue }
                    if control["type"] as? String == "mic_state",
                       let enabled = control["enabled"] as? Bool,
                       let pending = pendingMicrophone, pending.enabled == enabled {
                        pendingMicrophone = nil
                        microphoneTimeoutTask?.cancel()
                        microphoneTimeoutTask = nil
                        pending.continuation.resume()
                    }
                @unknown default:
                    continue
                }
            }
        } catch {
            finishTerminal(message: error.localizedDescription)
        }
    }

    private func checkCurrent(_ candidate: URLSessionWebSocketTask) throws {
        try Task.checkCancellation()
        guard !terminal, socket === candidate else { throw CancellationError() }
    }

    private func requireConnected() throws {
        try Task.checkCancellation()
        guard !terminal, isConnected, socket != nil else { throw BridgeError.connectionClosed }
    }

    private func finishTerminal(message: String?, notify: Bool = true) {
        guard !terminal else { return }
        terminal = true
        isConnected = false
        microphoneEnabled = false
        changingMicrophone = false
        connectTimeoutTask?.cancel()
        connectTimeoutTask = nil
        microphoneTimeoutTask?.cancel()
        microphoneTimeoutTask = nil
        receiveTask?.cancel()
        receiveTask = nil
        let pending = pendingMicrophone
        pendingMicrophone = nil
        writer?.close()
        writer = nil
        // Cancel transport immediately so Host restores its devices even if audio cleanup fails.
        socket?.cancel()
        session?.invalidateAndCancel()
        socket = nil
        session = nil
        audio.stop()
        pending?.continuation.resume(throwing: CancellationError())
        let microphoneCallback = onMicrophoneState
        let disconnectedCallback = onDisconnected
        onConnected = nil
        onMicrophoneState = nil
        onDisconnected = nil
        microphoneCallback?(false)
        if notify { disconnectedCallback?(message) }
    }

    private func decodeControl(_ text: String) -> [String: Any]? {
        guard let data = text.data(using: .utf8),
              let value = try? JSONSerialization.jsonObject(with: data),
              let control = value as? [String: Any] else { return nil }
        return control
    }

    private func jsonString(_ value: [String: Any]) -> String {
        guard let data = try? JSONSerialization.data(withJSONObject: value),
              let text = String(data: data, encoding: .utf8) else { return "{}" }
        return text
    }
}

/// One socket send in flight, at most six pending PCM frames and one control.
/// Audio callbacks never create DispatchQueue blocks or Tasks.
final class BoundedSocketWriter {
    private struct Pending {
        let id = UUID()
        let message: URLSessionWebSocketTask.Message
        let queuedAt: TimeInterval
    }

    private static let maximumPendingFrames = 6
    private static let maximumAge: TimeInterval = 0.5
    private let lock = NSLock()
    private var socket: URLSessionWebSocketTask?
    private var frames: [Pending] = []
    private var control: Pending?
    private var inFlight: Pending?
    private var captureEnabled = false
    private var closed = false
    private var timer: DispatchSourceTimer?
    private var onFailure: ((String) -> Void)?

    init(socket: URLSessionWebSocketTask, onFailure: @escaping (String) -> Void) {
        self.socket = socket
        self.onFailure = onFailure
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(
            label: "dev.remontevoice.writer-deadline", qos: .userInitiated
        ))
        timer.schedule(deadline: .now() + .milliseconds(50), repeating: .milliseconds(50))
        timer.setEventHandler { [weak self] in self?.checkDeadline() }
        self.timer = timer
        timer.resume()
    }

    deinit {
        timer?.cancel()
    }

    func setCaptureEnabled(_ enabled: Bool) {
        lock.lock()
        captureEnabled = enabled && !closed
        if !captureEnabled { frames.removeAll(keepingCapacity: false) }
        lock.unlock()
    }

    func enqueueMicrophoneFrame(_ payload: Data) {
        guard payload.count == 480 * MemoryLayout<Float>.size else { return }
        lock.lock()
        guard !closed, captureEnabled else { lock.unlock(); return }
        var packet = Data([0x01])
        packet.append(payload)
        if frames.count == Self.maximumPendingFrames { frames.removeFirst() }
        frames.append(Pending(message: .data(packet), queuedAt: ProcessInfo.processInfo.systemUptime))
        let next = takeNextLocked()
        lock.unlock()
        if let next { send(next) }
    }

    func sendControl(_ text: String) {
        guard text.utf8.count <= 1024 else { fail(); return }
        lock.lock()
        guard !closed else { lock.unlock(); return }
        guard control == nil else { lock.unlock(); fail(); return }
        frames.removeAll(keepingCapacity: false)
        control = Pending(message: .string(text), queuedAt: ProcessInfo.processInfo.systemUptime)
        let next = takeNextLocked()
        lock.unlock()
        if let next { send(next) }
    }

    private func takeNextLocked() -> Pending? {
        guard !closed, inFlight == nil else { return nil }
        discardStaleFramesLocked(now: ProcessInfo.processInfo.systemUptime)
        if let control {
            self.control = nil
            inFlight = control
        } else if !frames.isEmpty {
            inFlight = frames.removeFirst()
        }
        return inFlight
    }

    private func discardStaleFramesLocked(now: TimeInterval) {
        while let first = frames.first, now - first.queuedAt > Self.maximumAge {
            frames.removeFirst()
        }
    }

    private func send(_ pending: Pending) {
        var candidate: Pending? = pending
        while let current = candidate {
            lock.lock()
            guard !closed, inFlight?.id == current.id else { lock.unlock(); return }
            if case .data = current.message,
               !captureEnabled || ProcessInfo.processInfo.systemUptime - current.queuedAt > Self.maximumAge {
                // A selected frame may become stale/muted before the send starts.
                inFlight = nil
                candidate = takeNextLocked()
                lock.unlock()
                continue
            }
            if ProcessInfo.processInfo.systemUptime - current.queuedAt > Self.maximumAge {
                lock.unlock()
                fail() // An expired control must never be sent later as an unmute.
                return
            }
            let socket = self.socket
            lock.unlock()
            socket?.send(current.message) { [weak self] error in
                self?.completed(current.id, error: error)
            }
            return
        }
    }

    private func completed(_ id: UUID, error: Error?) {
        lock.lock()
        guard !closed, let active = inFlight, active.id == id else { lock.unlock(); return }
        if error != nil || ProcessInfo.processInfo.systemUptime - active.queuedAt > Self.maximumAge {
            lock.unlock()
            fail()
            return
        }
        inFlight = nil
        let next = takeNextLocked()
        lock.unlock()
        if let next { send(next) }
    }

    private func checkDeadline() {
        lock.lock()
        discardStaleFramesLocked(now: ProcessInfo.processInfo.systemUptime)
        let oldest = [inFlight?.queuedAt, control?.queuedAt, frames.first?.queuedAt].compactMap { $0 }.min()
        let expired = !closed && oldest.map {
            ProcessInfo.processInfo.systemUptime - $0 > Self.maximumAge
        } == true
        lock.unlock()
        if expired { fail() }
    }

    func close() {
        finish(failed: false)
    }

    private func fail() {
        finish(failed: true)
    }

    private func finish(failed: Bool) {
        lock.lock()
        guard !closed else { lock.unlock(); return }
        closed = true
        captureEnabled = false
        frames.removeAll(keepingCapacity: false)
        control = nil
        inFlight = nil
        let socket = self.socket
        self.socket = nil
        let timer = self.timer
        self.timer = nil
        let callback = onFailure
        onFailure = nil
        lock.unlock()
        timer?.cancel()
        if failed {
            // This does not wait for MainActor or UI callbacks to restore Host routing.
            socket?.cancel()
            callback?(BridgeError.writerTimeout.localizedDescription)
        }
    }
}

private final class PinnedSessionDelegate: NSObject, URLSessionDelegate {
    private let fingerprint: String

    init(fingerprint: String) {
        self.fingerprint = fingerprint
        super.init()
    }

    func urlSession(
        _ session: URLSession,
        didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust,
              let certificate = SecTrustGetCertificateAtIndex(trust, 0) else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        let certificateData = SecCertificateCopyData(certificate) as Data
        let received = SHA256.hash(data: certificateData)
            .map { String(format: "%02x", $0) }
            .joined()
            .uppercased()
        guard received == fingerprint else {
            completionHandler(.cancelAuthenticationChallenge, nil)
            return
        }
        completionHandler(.useCredential, URLCredential(trust: trust))
    }
}

enum BridgeError: LocalizedError {
    case connectionClosed
    case connectionTimeout
    case microphoneSwitchInProgress
    case microphoneStateTimeout
    case writerTimeout

    var errorDescription: String? {
        switch self {
        case .connectionClosed:
            return "Соединение уже закрыто."
        case .connectionTimeout:
            return "Ноутбук не подтвердил подключение. Соединение закрыто."
        case .microphoneSwitchInProgress:
            return "Дождитесь подтверждения переключения микрофона."
        case .microphoneStateTimeout:
            return "Ноутбук не подтвердил состояние микрофона. Звук возвращается на ноутбук."
        case .writerTimeout:
            return "Передача звука остановилась. Соединение закрыто для возврата звука ноутбуку."
        }
    }
}
