// Standalone Foundation probe. Not part of the app target.
// swiftc ios/RemoteVoice/PairingInfo.swift ios/tests/PairingInfoProbe.swift -o <temporary executable>
import Foundation

@main
struct PairingInfoProbe {
    static func main() throws {
        let fingerprint = String(repeating: "A", count: 64)
        let cases: [(String, Bool)] = [
            ("10.0.0.1", true), ("10.255.255.255", true),
            ("172.16.0.1", true), ("172.31.255.255", true), ("192.168.1.2", true),
            ("10.0.0.1.attacker.com", false), ("192.168.1.2.attacker.com", false),
            ("10..0.1", false), ("10.0.1", false), ("10.0.0.1.2", false),
            (".10.0.0.1", false), ("10.0.0.1.", false), ("10.0.0.256", false),
            ("010.0.0.1", false), ("10.00.0.1", false), ("+10.0.0.1", false),
            ("10.0.0.-1", false), ("１０.0.0.1", false), (" 10.0.0.1", false),
            ("10.0.0.1\n", false), ("172.15.0.1", false), ("172.32.0.1", false),
            ("192.169.0.1", false), ("127.0.0.1", false), ("169.254.1.2", false),
            ("8.8.8.8", false), ("::1", false), ("localhost", false), ("", false),
        ]
        for (host, accepted) in cases {
            var components = URLComponents()
            components.scheme = "remotevoice"
            components.host = "pair"
            components.queryItems = [
                URLQueryItem(name: "host", value: host),
                URLQueryItem(name: "port", value: "8765"),
                URLQueryItem(name: "fingerprint", value: fingerprint),
                URLQueryItem(name: "pin", value: "12345678"),
            ]
            guard let link = components.url else { fatalError("Fixture URL could not be encoded") }
            let parsed = try? PairingInfo.parse(pairingLink: link)
            precondition((parsed != nil) == accepted, "Unexpected endpoint acceptance")
            let restored = PairingInfo(host: host, port: 8765, fingerprint: fingerprint, pairingPIN: nil)
            precondition((restored.webSocketURL != nil) == accepted, "Persisted endpoint bypass")
            if accepted { precondition(parsed?.webSocketURL?.host == host, "Endpoint changed") }
        }
        print("PairingInfo probe passed: \(cases.count) endpoint fixtures")
    }
}
