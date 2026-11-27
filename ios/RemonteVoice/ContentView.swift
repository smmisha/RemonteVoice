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
            Image("Logo")
                .resizable()
                .scaledToFit()
                .frame(width: 96, height: 96)
                .accessibilityHidden(true)
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
        card(title: "Ноутбук", systemImage: "laptopcomputer") {
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
                Text(model.isConnected ? "Подключено" : "Сопряжение сохранено")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }

            primaryButton(
                model.isConnected ? "Отключить" : "Подключить",
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

            HStack(spacing: 10) {
                secondaryButton("Сканировать QR", systemImage: "qrcode.viewfinder") {
                    showingScanner = true
                }
                secondaryButton("Другой ноутбук", systemImage: "arrow.triangle.2.circlepath") {
                    model.forgetPairing()
                    showingScanner = true
                }
            }
        }
    }

    private var unpairedContent: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Откройте RemonteVoice на ноутбуке, нажмите «Начать подключение» и отсканируйте QR-код прямо здесь.")
                .font(.callout)
                .foregroundStyle(.secondary)

            primaryButton("Активировать", systemImage: "qrcode.viewfinder", tint: .green) {
                showingScanner = true
            }

            secondaryButton("Вставить ссылку из буфера", systemImage: "doc.on.clipboard") {
                pastePairingLink()
            }
        }
    }

    // MARK: - Microphone

    private var microphoneCard: some View {
        card(title: "Микрофон iPhone → ноутбук", systemImage: "mic") {
            VStack(spacing: 12) {
                primaryButton(
                    model.microphoneEnabled ? "Выключить микрофон" : "Включить микрофон",
                    systemImage: model.microphoneEnabled ? "mic.slash.fill" : "mic.fill",
                    tint: model.microphoneEnabled ? .red : .green
                ) {
                    model.setMicrophoneEnabled(!model.microphoneEnabled)
                }
                .disabled(!model.isConnected || model.isChangingMicrophone)

                Text(model.microphoneEnabled
                     ? "Голос поступает в виртуальный микрофон Windows."
                     : "Микрофон выключен. Звук ноутбука продолжает идти на iPhone.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: .infinity)
            }
        }
    }

    // MARK: - Footer and scanner sheet

    private var footer: some View {
        Text("Оба устройства должны быть в одной Wi-Fi сети. Подключите наушники к iPhone для приватного прослушивания.")
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
                        "Сканер недоступен",
                        systemImage: "camera",
                        description: Text("Отсканируйте QR-код камерой iPhone или вставьте ссылку из буфера обмена.")
                    )
                }
            }
            .navigationTitle("Сканирование QR")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Отмена") { showingScanner = false }
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
                .frame(maxWidth: .infinity)
                .padding(.vertical, 2)
        }
        .buttonStyle(.bordered)
        .controlSize(.large)
    }

    private func pastePairingLink() {
        guard let text = UIPasteboard.general.string else {
            model.errorMessage = "Буфер обмена пуст."
            return
        }
        model.applyPayload(text)
    }
}
