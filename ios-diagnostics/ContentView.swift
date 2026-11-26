import SwiftUI
import Foundation

struct LogEntry: Codable {
    let device_id: String
    let device_name: String
    let app_version: String
    let priority: String
    let tag: String
    let message: String
}

struct UploadPayload: Codable {
    let consent: Bool
    let entries: [LogEntry]
}

@MainActor
final class DiagnosticsModel: ObservableObject {
    @Published var server = "https://onlinemoney-sauce.onrender.com"
    @Published var apiKey = ""
    @Published var consent = false
    @Published var status = "No data is uploaded until you explicitly consent and tap Upload."
    @Published var events: [String] = ["App started"]
    
    func record(_ message: String) {
        let safe = message.replacingOccurrences(of: #"(?i)(token|password|authorization|api[_-]?key)\s*[:=]\s*[^\s,;]+"#, with: "$1=[redacted]", options: .regularExpression)
        events.append(String(safe.prefix(1000)))
        if events.count > 100 { events.removeFirst(events.count - 100) }
    }
    
    func upload() async {
        guard consent, let base = URL(string: server), base.scheme == "https", !apiKey.isEmpty else {
            status = "Enable consent and provide an HTTPS URL and API key."; return
        }
        let entries = events.suffix(100).map {
            LogEntry(device_id: "ios-app", device_name: "iPhone", app_version: "ios-client-1.0",
                     priority: "I", tag: "APP_DIAGNOSTIC", message: $0)
        }
        guard !entries.isEmpty else { status = "No events to upload."; return }
        do {
            var request = URLRequest(url: base.appendingPathComponent("api/android-logcat"))
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.setValue(apiKey, forHTTPHeaderField: "X-Logcat-Key")
            request.httpBody = try JSONEncoder().encode(UploadPayload(consent: true, entries: entries))
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
                status = "Server rejected the upload. Check URL, key, and deployment."; return
            }
            status = "Uploaded \(entries.count) app diagnostic events."
        } catch { status = "Upload failed: \(error.localizedDescription)" }
    }
}

struct ContentView: View {
    @StateObject private var model = DiagnosticsModel()
    var body: some View {
        NavigationView {
            Form {
                Section("Server connection") {
                    TextField("HTTPS server URL", text: $model.server).keyboardType(.URL).textInputAutocapitalization(.never)
                    SecureField("API key", text: $model.apiKey)
                }
                Section("App-only diagnostics") {
                    Text("This app cannot read iOS system logs or logs from other apps.")
                    ForEach(Array(model.events.enumerated()), id: \.offset) { _, event in Text(event).font(.caption) }
                    Button("Add test event") { model.record("User added a diagnostic test event") }
                }
                Section("Consent and upload") {
                    Toggle("I own this device and consent to upload these app events.", isOn: $model.consent)
                    Button("Upload selected app events") { Task { await model.upload() } }
                        .disabled(!model.consent)
                    Text(model.status).font(.footnote)
                }
            }.navigationTitle("Daved Diagnostics")
        }
        .onAppear { model.record("Diagnostics screen opened") }
    }
}
