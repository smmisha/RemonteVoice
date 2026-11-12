import SwiftUI

struct ContentView: View {
    @ObservedObject var model: BridgeModel

    var body: some View {
        VStack(spacing: 24) {
            VStack(spacing: 8) {
                Image(systemName: model.isConnected ? "waveform.circle.fill" : "waveform.circle")
                    .font(.system(size: 62))
                    .foregroundStyle(model.isConnected ? .green : .secondary)
                Text("RemonteVoice")
                    .font(.largeTitle.bold())
                Text(model.status)
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }

            GroupBox("Подключение") {
                VStack(alignment: .leading, spacing: 12) {
                    if let pairing = model.pairingInfo {
                        Label("Ноутбук: \(pairing.host):\(pairing.port)", systemImage: "laptopcomputer")
                            .font(.callout)
                    } else {
                        Label("Сопряжение не настроено", systemImage: "qrcode")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                        Text("Запустите RemonteVoice Host на ноутбуке, отсканируйте его QR-код камерой iPhone и откройте ссылку.")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }

                    Button(model.isConnected ? "Отключить" : "Подключить") {
                        if model.isConnected {
                            model.disconnect()
                        } else {
                            model.connect()
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.isConnecting || model.pairingInfo == nil)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            GroupBox("Микрофон iPhone → ноутбук") {
                VStack(spacing: 12) {
                    Button {
                        model.setMicrophoneEnabled(!model.microphoneEnabled)
                    } label: {
                        Label(
                            model.microphoneEnabled ? "ВЫКЛЮЧИТЬ МИКРОФОН" : "ВКЛЮЧИТЬ МИКРОФОН",
                            systemImage: model.microphoneEnabled ? "mic.fill" : "mic.slash.fill"
                        )
                        .font(.headline)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 16)
                    }
                    .tint(model.microphoneEnabled ? .red : .green)
                    .buttonStyle(.borderedProminent)
                    .disabled(!model.isConnected || model.isChangingMicrophone)

                    Text(model.microphoneEnabled
                         ? "Голос поступает в виртуальный микрофон Windows."
                         : "Микрофон выключен. Звук ноутбука продолжает идти на iPhone.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
            }

            if let error = model.errorMessage {
                Text(error)
                    .font(.footnote)
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }

            Spacer(minLength: 0)
            Text("Оба устройства должны быть в одной Wi-Fi сети. Подключите наушники к iPhone для приватного прослушивания.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(22)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(uiColor: .systemGroupedBackground))
    }
}
