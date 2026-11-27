import SwiftUI
import UIKit
import VisionKit

struct ContentView: View {
    @ObservedObject var model: BridgeModel
    @State private var showingScanner = false

    var body: some View {
        ScrollView {
            VStack(spacing: 18) {
                header
                pairingCard
                microphoneCard
                if let error = model.errorMessage {
                    Text(error)
                        .font(.footnote)
                        .foregroundStyle(.red)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 4)
                }
                footer
            }
            .padding(20)
        }
        .background(Color(uiColor: .systemGroupedBackground))
        .sheet(isPresented: $showingScanner) {
            scannerSheet
        }
    }

    // MARK: - Header

    private var header: some View {
        VStack(spacing: 10) {
            Logomark()
            Text("RemonteVoice")
                .font(.system(size: 32, weight: .bold, design: .rounded))
            HStack(spacing: 8) {
                Circle()
                    .fill(model.isConnected ? Color.green : Color.secondary.opacity(0.45))
                    .frame(width: 9, height: 9)
                Text(model.status)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 12)
    }

    // MARK: - Pairing

    private var pairingCard: some View {
        card(title: "РќРѕСѓС‚Р±СѓРє", systemImage: "laptopcomputer") {
            if let pairing = model.pairingInfo {
                pairedContent(pairing)
            } else {
                unpairedContent
            }
        }
    }

    private func pairedContent(_ pairing: PairingInfo) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 4) {
                Text(verbatim: "\(pairing.host):\(pairing.port)")
                    .font(.system(.title3, design: .monospaced))
                Text(model.isConnected ? "РџРѕРґРєР»СЋС‡РµРЅРѕ" : "РЎРѕРїСЂСЏР¶РµРЅРёРµ СЃРѕС…СЂР°РЅРµРЅРѕ")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            primaryButton(
                model.isConnected ? "РћС‚РєР»СЋС‡РёС‚СЊ" : "РџРѕРґРєР»СЋС‡РёС‚СЊ",
                systemImage: model.isConnected ? "stop.circle" : "play.circle",
                tint: model.isConnected ? .red : .green
            ) {
                if model.isConnected {
                    model.disconnect()
                } else {
                    model.connect()
                }
            }
            .disabled(model.isConnecting || model.pairingInfo == nil)

            VStack(spacing: 10) {
                secondaryButton("РЎРєР°РЅРёСЂРѕРІР°С‚СЊ QR-РєРѕРґ", systemImage: "qrcode.viewfinder") {
                    showingScanner = true
                }
                secondaryButton("РЎРјРµРЅРёС‚СЊ РЅРѕСѓС‚Р±СѓРє", systemImage: "arrow.triangle.2.circlepath") {
                    model.forgetPairing()
                    showingScanner = true
                }
            }
        }
    }
    private var unpairedContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("РћС‚РєСЂРѕР№С‚Рµ RemonteVoice РЅР° РЅРѕСѓС‚Р±СѓРєРµ, РЅР°Р¶РјРёС‚Рµ В«РќР°С‡Р°С‚СЊ РїРѕРґРєР»СЋС‡РµРЅРёРµВ» Рё РѕС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ QR-РєРѕРґ РїСЂСЏРјРѕ Р·РґРµСЃСЊ.")
                .font(.callout)
                .foregroundStyle(.secondary)

            primaryButton("РђРєС‚РёРІРёСЂРѕРІР°С‚СЊ", systemImage: "qrcode.viewfinder", tint: .green) {
                showingScanner = true
            }

            secondaryButton("Р’СЃС‚Р°РІРёС‚СЊ СЃСЃС‹Р»РєСѓ РёР· Р±СѓС„РµСЂР°", systemImage: "doc.on.clipboard") {
                pastePairingLink()
            }
        }
    }

    // MARK: - Microphone

    private var microphoneCard: some View {
        card(title: "РњРёРєСЂРѕС„РѕРЅ iPhone в†’ РЅРѕСѓС‚Р±СѓРє", systemImage: "mic") {
            VStack(spacing: 12) {
                primaryButton(
                    model.microphoneEnabled ? "Р’С‹РєР»СЋС‡РёС‚СЊ РјРёРєСЂРѕС„РѕРЅ" : "Р’РєР»СЋС‡РёС‚СЊ РјРёРєСЂРѕС„РѕРЅ",
                    systemImage: model.microphoneEnabled ? "mic.slash.fill" : "mic.fill",
                    tint: model.microphoneEnabled ? .red : .green
                ) {
                    model.setMicrophoneEnabled(!model.microphoneEnabled)
                }
                .disabled(!model.isConnected || model.isChangingMicrophone)

                Text(model.microphoneEnabled
                     ? "Р“РѕР»РѕСЃ РїРѕСЃС‚СѓРїР°РµС‚ РІ РІРёСЂС‚СѓР°Р»СЊРЅС‹Р№ РјРёРєСЂРѕС„РѕРЅ Windows."
                     : "РњРёРєСЂРѕС„РѕРЅ РІС‹РєР»СЋС‡РµРЅ. Р—РІСѓРє РЅРѕСѓС‚Р±СѓРєР° РїСЂРѕРґРѕР»Р¶Р°РµС‚ РёРґС‚Рё РЅР° iPhone.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
            }
        }
    }

    // MARK: - Footer and scanner sheet

    private var footer: some View {
        Text("РћР±Р° СѓСЃС‚СЂРѕР№СЃС‚РІР° РґРѕР»Р¶РЅС‹ Р±С‹С‚СЊ РІ РѕРґРЅРѕР№ Wi-Fi СЃРµС‚Рё. РџРѕРґРєР»СЋС‡РёС‚Рµ РЅР°СѓС€РЅРёРєРё Рє iPhone РґР»СЏ РїСЂРёРІР°С‚РЅРѕРіРѕ РїСЂРѕСЃР»СѓС€РёРІР°РЅРёСЏ.")
            .font(.footnote)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .padding(.top, 4)
    }

    @ViewBuilder
    private var scannerSheet: some View {
        NavigationStack {
            Group {
                if DataScannerViewController.isSupported {
                    LinkScanner { payload in
                        showingScanner = false
                        model.applyPayload(payload)
                    }
                    .ignoresSafeArea()
                } else {
                    ContentUnavailableView(
                        "РЎРєР°РЅРµСЂ РЅРµРґРѕСЃС‚СѓРїРµРЅ",
                        systemImage: "camera",
                        description: Text("РћС‚СЃРєР°РЅРёСЂСѓР№С‚Рµ QR-РєРѕРґ РєР°РјРµСЂРѕР№ iPhone РёР»Рё РІСЃС‚Р°РІСЊС‚Рµ СЃСЃС‹Р»РєСѓ РёР· Р±СѓС„РµСЂР° РѕР±РјРµРЅР°.")
                    )
                }
            }
            .navigationTitle("РЎРєР°РЅРёСЂРѕРІР°РЅРёРµ QR")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("РћС‚РјРµРЅР°") { showingScanner = false }
                }
            }
        }
    }

    // MARK: - Building blocks

    private func card<Content: View>(
        title: String,
        systemImage: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(title, systemImage: systemImage)
                .font(.headline)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(18)
        .background(Color(uiColor: .secondarySystemGroupedBackground), in: RoundedRectangle(cornerRadius: 18, style: .continuous))
    }

    private func primaryButton(
        _ title: String,
        systemImage: String,
        tint: Color,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(.headline)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 4)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
        .tint(tint)
    }

    private func secondaryButton(
        _ title: String,
        systemImage: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
                .minimumScaleFactor(0.85)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 2)
        }
        .buttonStyle(.bordered)
        .controlSize(.large)
    }

    private func pastePairingLink() {
        guard let text = UIPasteboard.general.string else {
            model.errorMessage = "Р‘СѓС„РµСЂ РѕР±РјРµРЅР° РїСѓСЃС‚."
            return
        }
        model.applyPayload(text)
    }
}

/// Brand mark drawn in SwiftUI: a ring with a waveform. Drawing it in code
/// keeps the mark crisp and avoids the clipping the image asset showed.
private struct Logomark: View {
    private let brand = Color(red: 0.494, green: 0.906, blue: 0.529)

    var body: some View {
        ZStack {
            Circle()
                .stroke(brand, lineWidth: 6)
            HStack(alignment: .center, spacing: 6) {
                ForEach(Array([0.42, 0.72, 1.0, 0.72, 0.42].enumerated()), id: \.offset) { _, factor in
                    Capsule()
                        .fill(brand)
                        .frame(width: 7, height: 44 * factor)
                }
            }
        }
        .frame(width: 92, height: 92)
        .accessibilityHidden(true)
    }
}
