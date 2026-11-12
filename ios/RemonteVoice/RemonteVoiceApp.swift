import SwiftUI

@main
struct RemonteVoiceApp: App {
    @StateObject private var model = BridgeModel()

    var body: some Scene {
        WindowGroup {
            ContentView(model: model)
                .onOpenURL { model.acceptPairingLink($0) }
        }
    }
}
