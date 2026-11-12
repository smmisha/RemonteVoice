import AVFAudio
import Foundation

final class PhoneAudioEngine {
    private let engine = AVAudioEngine()
    private let player = AVAudioPlayerNode()
    private let targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatFloat32,
        sampleRate: 48_000,
        channels: 2,
        interleaved: false
    )!
    private var playerAttached = false
    private var captureInstalled = false
    private var converter: AVAudioConverter?
    private var pendingSamples: [Float] = []
    private var scheduledFrames = 0
    private var playbackGeneration: UInt64 = 0
    private var sessionConfigured = false
    private var stopped = false
    private let lock = NSRecursiveLock()
    // Never held across an AVAudioEngine/player call. Completion callbacks do
    // not wait for the configuration lock that native stop may itself await.
    private let playbackCounterLock = NSLock()

    func startPlayback() throws {
        lock.lock()
        defer { lock.unlock() }
        guard !stopped else { throw CancellationError() }
        do {
            try configureSession(category: .playback, mode: .default)
            try startEngineIfNeeded()
        } catch {
            stopLocked()
            throw error
        }
    }

    // Permission does not mutate an engine or the shared audio session. The
    // session owner must recheck its identity/cancellation after this await.
    static func requestMicrophonePermission() async -> Bool {
        await AVAudioApplication.requestRecordPermission()
    }

    func startMicrophone(onFrame: @escaping (Data) -> Void) throws {
        lock.lock()
        defer { lock.unlock() }
        guard !stopped else { throw CancellationError() }
        guard !captureInstalled else { return }

        do {
            resetPlaybackLocked()
            try configureSession(category: .playAndRecord, mode: .voiceChat)
            let input = engine.inputNode
            try input.setVoiceProcessingEnabled(true)
            let sourceFormat = input.outputFormat(forBus: 0)
            guard sourceFormat.sampleRate > 0,
                  let converter = AVAudioConverter(from: sourceFormat, to: monoFormat) else {
                throw PairingError.invalidAudioFrame
            }
            self.converter = converter
            pendingSamples.removeAll(keepingCapacity: true)

            input.installTap(onBus: 0, bufferSize: 480, format: sourceFormat) { [weak self] buffer, _ in
                self?.convertMicrophoneBuffer(buffer, onFrame: onFrame)
            }
            captureInstalled = true
            try startEngineIfNeeded()
        } catch {
            let startupError = error
            do {
                try returnToPlaybackLocked()
            } catch {
                stopLocked()
                throw PhoneAudioError.playbackRecoveryFailed
            }
            throw startupError
        }
    }

    func stopMicrophoneAndReturnToPlayback() throws {
        lock.lock()
        defer { lock.unlock() }
        guard !stopped else { throw CancellationError() }
        guard captureInstalled else { return }
        do {
            try returnToPlaybackLocked()
        } catch {
            stopLocked()
            throw PhoneAudioError.playbackRecoveryFailed
        }
    }

    private func returnToPlaybackLocked() throws {
        resetPlaybackLocked()
        if captureInstalled {
            engine.inputNode.removeTap(onBus: 0)
            captureInstalled = false
        }
        if engine.inputNode.isVoiceProcessingEnabled {
            try engine.inputNode.setVoiceProcessingEnabled(false)
        }
        converter = nil
        pendingSamples.removeAll(keepingCapacity: true)
        try configureSession(category: .playback, mode: .default)
        try startEngineIfNeeded()
    }

    func playSystemFrame(_ payload: Data) throws {
        let bytesPerFrame = MemoryLayout<Float>.size * 2
        guard payload.count == 480 * bytesPerFrame else {
            throw PairingError.invalidAudioFrame
        }

        lock.lock()
        defer { lock.unlock() }
        guard !stopped else { throw CancellationError() }

        let frameCount = payload.count / bytesPerFrame
        guard let buffer = AVAudioPCMBuffer(
            pcmFormat: targetFormat,
            frameCapacity: AVAudioFrameCount(frameCount)
        ) else {
            throw PairingError.invalidAudioFrame
        }
        buffer.frameLength = AVAudioFrameCount(frameCount)
        guard let channels = buffer.floatChannelData else {
            throw PairingError.invalidAudioFrame
        }

        payload.withUnsafeBytes { rawBuffer in
            let samples = rawBuffer.bindMemory(to: Float.self)
            for frame in 0..<frameCount {
                channels[0][frame] = samples[frame * 2]
                channels[1][frame] = samples[frame * 2 + 1]
            }
        }

        if !engine.isRunning {
            try startEngineIfNeeded()
        }
        playbackCounterLock.lock()
        let discardPlaybackBacklog = scheduledFrames > 48_000 / 3
        if discardPlaybackBacklog {
            playbackGeneration &+= 1
            scheduledFrames = 0
        }
        playbackCounterLock.unlock()
        if discardPlaybackBacklog {
            player.stop()
            player.play()
        }
        playbackCounterLock.lock()
        scheduledFrames += frameCount
        let generation = playbackGeneration
        playbackCounterLock.unlock()
        player.scheduleBuffer(buffer) { [weak self] in
            guard let self else { return }
            self.playbackCounterLock.lock()
            if self.playbackGeneration == generation {
                self.scheduledFrames = max(0, self.scheduledFrames - frameCount)
            }
            self.playbackCounterLock.unlock()
        }
    }

    func stop() {
        lock.lock()
        defer { lock.unlock() }
        guard !stopped else { return }
        stopped = true
        stopLocked()
    }

    private func resetPlaybackLocked() {
        playbackCounterLock.lock()
        playbackGeneration &+= 1
        scheduledFrames = 0
        playbackCounterLock.unlock()
        engine.stop()
        player.stop()
    }

    private func stopLocked() {
        resetPlaybackLocked()
        if captureInstalled {
            engine.inputNode.removeTap(onBus: 0)
            captureInstalled = false
        }
        if engine.inputNode.isVoiceProcessingEnabled {
            try? engine.inputNode.setVoiceProcessingEnabled(false)
        }
        converter = nil
        pendingSamples.removeAll(keepingCapacity: false)
        if sessionConfigured {
            try? AVAudioSession.sharedInstance().setActive(
                false,
                options: .notifyOthersOnDeactivation
            )
            sessionConfigured = false
        }
    }

    private var monoFormat: AVAudioFormat {
        AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 48_000, channels: 1, interleaved: false)!
    }

    private func configureSession(category: AVAudioSession.Category, mode: AVAudioSession.Mode) throws {
        let session = AVAudioSession.sharedInstance()
        sessionConfigured = true
        try session.setPreferredSampleRate(48_000)
        let options: AVAudioSession.CategoryOptions = category == .playAndRecord
            ? [.defaultToSpeaker]
            : []
        try session.setCategory(category, mode: mode, options: options)
        try session.setActive(true)
    }

    private func startEngineIfNeeded() throws {
        if !playerAttached {
            engine.attach(player)
            engine.connect(player, to: engine.mainMixerNode, format: targetFormat)
            playerAttached = true
        }
        if !engine.isRunning {
            engine.prepare()
            try engine.start()
        }
        if !player.isPlaying {
            player.play()
        }
    }

    private func convertMicrophoneBuffer(
        _ inputBuffer: AVAudioPCMBuffer,
        onFrame: @escaping (Data) -> Void
    ) {
        // Native stop/removeTap may wait for a render callback to finish. The
        // tap must never wait for their configuration lock: drop this buffer.
        guard lock.try() else { return }
        defer { lock.unlock() }
        guard !stopped, captureInstalled, let converter,
              let output = AVAudioPCMBuffer(
                pcmFormat: monoFormat,
                frameCapacity: AVAudioFrameCount(
                    ceil(Double(inputBuffer.frameLength) * 48_000 / inputBuffer.format.sampleRate) + 16
                )
              ) else { return }

        var conversionError: NSError?
        var suppliedInput = false
        let status = converter.convert(to: output, error: &conversionError) { _, inputStatus in
            guard !suppliedInput else {
                inputStatus.pointee = .noDataNow
                return nil
            }
            suppliedInput = true
            inputStatus.pointee = .haveData
            return inputBuffer
        }
        guard status != .error,
              let monoSamples = output.floatChannelData?[0],
              output.frameLength > 0 else { return }

        pendingSamples.append(contentsOf: UnsafeBufferPointer(
            start: monoSamples,
            count: Int(output.frameLength)
        ))
        while pendingSamples.count >= 480 {
            let packetSamples = Array(pendingSamples.prefix(480))
            pendingSamples.removeFirst(480)
            let packet = packetSamples.withUnsafeBytes { Data($0) }
            onFrame(packet)
        }
    }
}

enum PhoneAudioError: LocalizedError {
    case playbackRecoveryFailed

    var errorDescription: String? {
        "Не удалось восстановить звук iPhone. Соединение закрыто для возврата звука ноутбуку."
    }
}
