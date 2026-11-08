import Foundation

struct PairingInfo: Codable, Equatable {
    let host: String
    let port: Int
    let fingerprint: String
    var pairingPIN: String?

    var webSocketURL: URL? {
        guard Self.isPrivateIPv4(host), (1...65535).contains(port),
              Self.isFingerprint(fingerprint) else { return nil }
        var components = URLComponents()
        components.scheme = "wss"
        components.host = host
        components.port = port
        components.path = "/"
        return components.url
    }

    static func parse(pairingLink url: URL) throws -> PairingInfo {
        guard url.scheme?.lowercased() == "remotevoice",
              url.host?.lowercased() == "pair",
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let items = components.queryItems else {
            throw PairingError.invalidLink
        }

        let values = Dictionary(items.map { ($0.name, $0.value ?? "") }, uniquingKeysWith: { first, _ in first })
        guard let host = values["host"], isPrivateIPv4(host),
              let portValue = values["port"], let port = Int(portValue), (1...65535).contains(port),
              let fingerprint = values["fingerprint"], isFingerprint(fingerprint),
              let pin = values["pin"], pin.utf8.count == 8,
              pin.utf8.allSatisfy({ (48...57).contains($0) }) else {
            throw PairingError.invalidLink
        }

        return PairingInfo(
            host: host,
            port: port,
            fingerprint: fingerprint.uppercased(),
            pairingPIN: pin
        )
    }

    private static func isFingerprint(_ value: String) -> Bool {
        value.utf8.count == 64 && value.utf8.allSatisfy {
            (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0)
        }
    }

    private static func isPrivateIPv4(_ value: String) -> Bool {
        let components = value.split(separator: ".", omittingEmptySubsequences: false)
        guard components.count == 4 else { return false }
        var parts: [UInt8] = []
        for component in components {
            // Canonical decimal octets also avoid URL parsers treating leading
            // zeroes as an alternate (for example octal) IPv4 representation.
            guard !component.isEmpty, component.utf8.count <= 3,
                  component.utf8.allSatisfy({ (48...57).contains($0) }),
                  component == "0" || component.first != "0",
                  let octet = UInt8(component) else { return false }
            parts.append(octet)
        }
        let first = parts[0]
        let second = parts[1]
        return first == 10 ||
            (first == 172 && (16...31).contains(second)) ||
            (first == 192 && second == 168)
    }
}

enum PairingError: LocalizedError {
    case invalidLink
    case microphonePermissionDenied
    case invalidAudioFrame
    case hostRejectedPairing

    var errorDescription: String? {
        switch self {
        case .invalidLink:
            return "QR-код RemoteVoice повреждён или содержит адрес вне локальной сети."
        case .microphonePermissionDenied:
            return "Разрешите RemoteVoice использовать микрофон в настройках iPhone."
        case .invalidAudioFrame:
            return "Получен аудиокадр неподдерживаемого формата."
        case .hostRejectedPairing:
            return "Host отклонил сопряжение. Запустите новое сопряжение на ноутбуке."
        }
    }
}
