import Foundation
import SwiftUI

@MainActor
final class BridgeModel: ObservableObject {
    @Published private(set) var pairingInfo: PairingInfo?
    @Published private(set) var status = "Не подключено"
    @Published private(set) var isConnected = false
    @Published private(set) var isConnecting = false
    @Published private(set) var microphoneEnabled = false
    @Published private(set) var isChangingMicrophone = false
    @Published var errorMessage: String?

    private var client: BridgeClient?
    private var generation: UInt64 = 0
    private var connectTask: Task<Void, Never>?
    private var microphoneTask: Task<Void, Never>?
    private let pairingDefaultsKey = "remontevoice.pairing.v1"

    init() {
        if let data = UserDefaults.standard.data(forKey: pairingDefaultsKey) {
            let saved = try? JSONDecoder().decode(PairingInfo.self, from: data)
            // Persisted data must obey the same endpoint boundary as a new QR.
            if saved?.webSocketURL != nil { pairingInfo = saved }
        }
    }

    func acceptPairingLink(_ url: URL) {
        do {
            let pairing = try PairingInfo.parse(pairingLink: url)
            disconnect()
            pairingInfo = pairing
            persistPairing(pairing)
            errorMessage = nil
            status = "Сопряжение сохранено. Подключитесь к ноутбуку."
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func connect() {
        guard !isConnecting, !isConnected, let pairingInfo else { return }
        generation &+= 1
        let currentGeneration = generation
        isConnecting = true
        errorMessage = nil
        status = "Подключение…"

        let client = BridgeClient(pairing: pairingInfo)
        self.client = client
        client.onConnected = { [weak self, weak client] _ in
            guard let self, let client, self.isCurrent(client, currentGeneration) else { return }
            var saved = pairingInfo
            saved.pairingPIN = nil
            self.pairingInfo = saved
            self.persistPairing(saved)
            self.isConnecting = false
            self.isConnected = true
            self.microphoneEnabled = false
            self.status = "Ноутбук подключён · звук идёт на iPhone"
            self.errorMessage = nil
        }
        client.onMicrophoneState = { [weak self, weak client] enabled in
            guard let self, let client, self.isCurrent(client, currentGeneration) else { return }
            self.microphoneEnabled = enabled
            // Only the matching operation completion clears the switching state.
        }
        client.onDisconnected = { [weak self, weak client] message in
            guard let self, let client, self.isCurrent(client, currentGeneration) else { return }
            let wasConnecting = self.isConnecting
            self.generation &+= 1
            self.client = nil
            self.connectTask?.cancel()
            self.connectTask = nil
            self.microphoneTask?.cancel()
            self.microphoneTask = nil
            self.isConnecting = false
            self.isConnected = false
            self.microphoneEnabled = false
            self.isChangingMicrophone = false
            self.status = wasConnecting
                ? "Не удалось подключиться"
                : "Соединение закрыто · микрофон выключен"
            self.errorMessage = message
        }

        connectTask = Task { [weak self, weak client] in
            guard let self, let client, self.isCurrent(client, currentGeneration) else { return }
            do {
                try await client.connect()
                guard !Task.isCancelled, self.isCurrent(client, currentGeneration) else { return }
                self.connectTask = nil
            } catch {
                guard self.isCurrent(client, currentGeneration) else { return }
                client.disconnect()
                self.client = nil
                self.connectTask = nil
                self.isConnecting = false
                self.isConnected = false
                self.status = "Не удалось подключиться"
                self.errorMessage = error.localizedDescription
            }
        }
    }

    func disconnect() {
        // Invalidate callbacks before cancelling suspended permission/network work.
        generation &+= 1
        connectTask?.cancel()
        connectTask = nil
        microphoneTask?.cancel()
        microphoneTask = nil
        let previous = client
        client = nil
        // Synchronous local cleanup finishes before a new client can own AVAudioSession.
        previous?.disconnect()
        isConnected = false
        isConnecting = false
        microphoneEnabled = false
        isChangingMicrophone = false
        status = "Отключено · микрофон выключен"
    }

    func setMicrophoneEnabled(_ enabled: Bool) {
        guard isConnected, let client, !isChangingMicrophone else { return }
        let currentGeneration = generation
        isChangingMicrophone = true
        errorMessage = nil

        microphoneTask = Task { [weak self, weak client] in
            guard let self, let client, self.isCurrent(client, currentGeneration) else { return }
            do {
                try await client.setMicrophoneEnabled(enabled)
                guard !Task.isCancelled, self.isCurrent(client, currentGeneration) else { return }
                self.microphoneEnabled = enabled
                self.status = enabled
                    ? "Микрофон включён · голос идёт на ноутбук"
                    : "Микрофон выключен · звук ноутбука идёт на iPhone"
                self.isChangingMicrophone = false
                self.microphoneTask = nil
            } catch {
                guard self.isCurrent(client, currentGeneration) else { return }
                self.microphoneEnabled = false
                self.isChangingMicrophone = false
                self.microphoneTask = nil
                self.errorMessage = error.localizedDescription
            }
        }
    }

    private func isCurrent(_ candidate: BridgeClient, _ candidateGeneration: UInt64) -> Bool {
        generation == candidateGeneration && client === candidate
    }

    private func persistPairing(_ pairing: PairingInfo) {
        guard let data = try? JSONEncoder().encode(pairing) else { return }
        UserDefaults.standard.set(data, forKey: pairingDefaultsKey)
    }
}