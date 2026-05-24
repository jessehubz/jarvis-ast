import Foundation
import AVFAudio

// MARK: - TTS Speaker

final class JarvisSpeaker: NSObject, AVSpeechSynthesizerDelegate {
    private let synth = AVSpeechSynthesizer()
    private var voice: AVSpeechSynthesisVoice?

    // How many utterances are queued or currently speaking.
    // AVSpeechSynthesizer queues speak() calls automatically; we track
    // the count so we know precisely when all speech has drained.
    private var pendingCount = 0

    // isSpeaking is true while any utterance is in-flight or queued.
    // Computed from pendingCount so it can never get out of sync.
    var isSpeaking: Bool { pendingCount > 0 }

    // Called on main thread once pendingCount reaches 0.
    var onQueueEmpty: (() -> Void)?

    override init() {
        super.init()
        synth.delegate = self
        pickVoice()
    }

    // MARK: Voice selection

    private func pickVoice() {
        let all = AVSpeechSynthesisVoice.speechVoices()

        let nameRanking = ["Daniel", "Arthur", "Malcolm",
                           "Zac", "Evan", "Aaron", "Alex", "Reed", "Liam"]

        func score(_ v: AVSpeechSynthesisVoice) -> Int {
            let nameScore    = nameRanking.firstIndex(where: { v.name.contains($0) })
                                    .map { nameRanking.count - $0 } ?? 0
            let qualScore    = v.quality == .premium ? 3 : v.quality == .enhanced ? 2 : 1
            let britishBonus = v.language.hasPrefix("en-GB") ? 4 : 0
            return nameScore * 10 + qualScore * 2 + britishBonus
        }

        voice = all
            .filter { $0.language.hasPrefix("en-US") || $0.language.hasPrefix("en-GB") }
            .sorted { score($0) > score($1) }
            .first ?? AVSpeechSynthesisVoice(language: "en-GB")
              ?? AVSpeechSynthesisVoice(language: "en-US")

        print("[TTS] selected voice: \(voice?.name ?? "system") (\(voice?.language ?? "?"))")
    }

    // MARK: Public API

    // AVSpeechSynthesizer needs ~400ms to warm up the audio hardware on first use.
    // Without a leading delay the first utterance starts playing before the output
    // device is ready and the opening syllables are clipped / silent.
    private var isFirstUtterance = true

    func speak(_ text: String) {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty else { return }

        let u = AVSpeechUtterance(string: t)
        u.voice           = voice
        u.rate            = 0.46
        u.pitchMultiplier = 0.80
        u.volume          = 1.0
        if isFirstUtterance {
            u.preUtteranceDelay = 0.45   // give audio hardware time to init
            isFirstUtterance = false
        }

        // Increment before calling speak so isSpeaking is true immediately,
        // preventing a premature tts_done check in the "done" handler.
        pendingCount += 1
        synth.speak(u)
        print("[TTS] queued (\(pendingCount) pending): \(t.prefix(60))")
    }

    func stop() {
        // Reset count immediately — don't rely on didCancel callbacks which
        // only fire for the current utterance, not the full internal queue.
        pendingCount = 0
        synth.stopSpeaking(at: .word)
        print("[TTS] stopped")
    }

    // MARK: Delegate

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish _: AVSpeechUtterance) {
        pendingCount = max(0, pendingCount - 1)
        print("[TTS] didFinish — \(pendingCount) remaining")
        if pendingCount == 0 { onQueueEmpty?() }
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel _: AVSpeechUtterance) {
        // stop() already zeroed pendingCount; just ensure clean state.
        pendingCount = 0
        print("[TTS] didCancel — queue cleared")
    }
}

// MARK: - View Model

final class VoiceViewModel: NSObject, ObservableObject, URLSessionWebSocketDelegate {

    static let shared = VoiceViewModel()

    @Published var messages: [ChatMessage] = [
        ChatMessage(role: .jarvis,
                    text: "Hello. Say \"hey jarvis\" or type a message below.")
    ]
    @Published var statusText = "Connecting to voice server…"
    @Published var mode: Mode  = .offline

    // Task automation state
    @Published var taskActive      = false
    @Published var taskId          = ""
    @Published var taskName        = ""
    @Published var taskSteps: [TaskStep]   = []
    @Published var taskPreviewImage: String? = nil
    @Published var recentTasks: [RecentTask] = []

    private var wsTask:  URLSessionWebSocketTask?
    private var session: URLSession!
    let speaker = JarvisSpeaker()

    // TTS accumulation buffers
    private var ttsBuf    = ""
    private var streamBuf = ""

    // Guards the tts_done handshake:
    // Only send "tts_done" to Python when BOTH generation is confirmed finished
    // AND the speech queue has fully drained.  Without this guard, a temporarily
    // empty queue between streaming sentences fires tts_done prematurely,
    // re-opening the mic while Jarvis is still speaking.
    private var generationComplete = false

    private var reconnecting  = false
    private var reconnectDelay: TimeInterval = 2.0

    override init() {
        super.init()
        session = URLSession(configuration: .default,
                             delegate: self,
                             delegateQueue: .main)

        // Fire tts_done only when generation is also complete
        speaker.onQueueEmpty = { [weak self] in
            guard let self = self else { return }
            if self.generationComplete {
                self.generationComplete = false
                self.send(["type": "tts_done"])
            }
            // If generationComplete is still false, the "done" event hasn't
            // arrived yet (race: last sentence played before streaming ended).
            // We'll send tts_done from the "done" handler instead.
        }

        connect()
    }

    // MARK: - WebSocket

    func connect() {
        wsTask?.cancel(with: .goingAway, reason: nil)
        guard let url = URL(string: "ws://localhost:8765") else { return }
        wsTask = session.webSocketTask(with: url)
        wsTask?.resume()
        receive()
    }

    private func receive() {
        wsTask?.receive { [weak self] result in
            DispatchQueue.main.async {
                guard let self = self else { return }
                switch result {
                case .success(let msg):
                    if case .string(let txt) = msg { self.handle(txt) }
                    self.receive()
                case .failure:
                    self.mode        = .offline
                    self.statusText  = "Voice server offline — run voice_server.py"
                    self.scheduleReconnect()
                }
            }
        }
    }

    private func scheduleReconnect() {
        guard !reconnecting else { return }
        reconnecting = true
        DispatchQueue.main.asyncAfter(deadline: .now() + reconnectDelay) { [weak self] in
            guard let self = self else { return }
            self.reconnecting    = false
            self.reconnectDelay  = min(self.reconnectDelay * 1.5, 30.0) // exponential backoff
            self.connect()
        }
    }

    func send(_ obj: [String: String]) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj),
              let str  = String(data: data, encoding: .utf8) else { return }
        wsTask?.send(.string(str)) { _ in }
    }

    func sendText(_ text: String) {
        send(["type": "text", "text": text])
    }

    func cancelTask() {
        send(["type": "cancel_task"])
    }

    // MARK: - URLSessionWebSocketDelegate

    func urlSession(_ session: URLSession, webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        DispatchQueue.main.async { [weak self] in
            self?.mode          = .idle
            self?.statusText    = "Connected — loading models…"
            self?.reconnectDelay = 2.0   // reset backoff on successful connect
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask,
                    didCompleteWithError error: Error?) {
        DispatchQueue.main.async { [weak self] in
            self?.mode       = .offline
            self?.statusText = "Voice server offline — run voice_server.py"
            self?.scheduleReconnect()
        }
    }

    // MARK: - Event handler

    private func handle(_ raw: String) {
        guard let data = raw.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = json["type"] as? String else { return }

        switch type {

        case "_connected":
            mode       = .idle
            statusText = "Connected — loading models…"
            reconnectDelay = 2.0

        case "_disconnected":
            mode       = .offline
            statusText = "Voice server offline — run voice_server.py"

        case "ready":
            mode       = .listen
            statusText = "Listening…"

        case "stop_tts":
            // Wake word interrupted; discard pending speech and reset state
            speaker.stop()
            generationComplete = false
            ttsBuf    = ""
            streamBuf = ""

        case "status":
            guard let text = json["text"] as? String else { break }
            statusText = text
            if      text.contains("Record")                               { mode = .record }
            else if text.contains("Think") || text.contains("Transcrib") { mode = .think  }
            else if text.contains("Speaking") || text.contains("Speak")  { mode = .speak  }
            else if text.contains("Listen")                               { mode = .listen }
            else                                                           { mode = .idle   }

        case "woke":
            mode       = .record
            statusText = "Recording…"

        case "heard":
            guard let text = json["text"] as? String else { break }
            // New utterance starts; reset everything for the new response
            generationComplete = false
            ttsBuf    = ""
            streamBuf = ""
            messages.append(ChatMessage(role: .user,   text: text))
            messages.append(ChatMessage(role: .jarvis, text: "", isStreaming: true))

        case "token":
            guard let text = json["text"] as? String else { break }
            streamBuf += text
            ttsBuf    += text
            if let idx = messages.indices.last, messages[idx].isStreaming {
                messages[idx].text = streamBuf
            }
            flushTTSSentences()

        case "done":
            if let idx = messages.indices.last, messages[idx].isStreaming {
                messages[idx].isStreaming = false
            }
            // Speak any remaining incomplete sentence
            let leftover = ttsBuf.trimmingCharacters(in: .whitespacesAndNewlines)
            if !leftover.isEmpty { speaker.speak(leftover) }
            ttsBuf = ""

            // Mark generation as complete.
            // If the speech queue is already empty (short or silent response),
            // send tts_done immediately.  Otherwise the onQueueEmpty callback
            // will fire it once the last utterance finishes.
            generationComplete = true
            if !speaker.isSpeaking {
                generationComplete = false
                send(["type": "tts_done"])
            }

        case "warn":
            if let text = json["text"] as? String {
                messages.append(ChatMessage(role: .jarvis, text: "⚠ \(text)", isWarning: true))
            }

        case "error":
            if let text = json["text"] as? String {
                statusText = "⚠ \(text)"
                if let idx = messages.indices.last, messages[idx].isStreaming {
                    messages[idx].text        = text
                    messages[idx].isStreaming = false
                    messages[idx].isError     = true
                } else {
                    messages.append(ChatMessage(role: .jarvis, text: text, isError: true))
                }
                // On error, always ensure mic re-opens
                generationComplete = false
                send(["type": "tts_done"])
            }

        case "task_start":
            guard let tid   = json["task_id"]   as? String,
                  let tname = json["task_name"]  as? String,
                  let raw   = json["steps"]      as? [[String: Any]] else { break }
            taskId    = tid
            taskName  = tname
            taskSteps = raw.map { s in
                TaskStep(id:   s["id"]   as? String ?? UUID().uuidString,
                         name: s["name"] as? String ?? "Step")
            }
            taskPreviewImage = nil
            taskActive = true

        case "task_step_start":
            guard let stepId = json["step_id"] as? String else { break }
            if let i = taskSteps.firstIndex(where: { $0.id == stepId }) {
                taskSteps[i].status = .active
            }

        case "task_step_done":
            guard let stepId = json["step_id"] as? String else { break }
            if let i = taskSteps.firstIndex(where: { $0.id == stepId }) {
                taskSteps[i].status  = .done
                taskSteps[i].message = json["message"] as? String ?? ""
            }

        case "task_step_error":
            guard let stepId = json["step_id"] as? String else { break }
            if let i = taskSteps.firstIndex(where: { $0.id == stepId }) {
                taskSteps[i].status  = .failed
                taskSteps[i].message = json["message"] as? String ?? ""
            }

        case "task_preview":
            if let img = json["image"] as? String { taskPreviewImage = img }

        case "task_complete":
            recentTasks.insert(RecentTask(name: taskName, completedAt: Date(), success: true), at: 0)
            if recentTasks.count > 10 { recentTasks.removeLast() }
            taskActive = false

        case "task_error":
            recentTasks.insert(RecentTask(name: taskName, completedAt: Date(), success: false), at: 0)
            if recentTasks.count > 10 { recentTasks.removeLast() }
            taskActive = false

        case "task_cancelled":
            taskActive = false

        default: break
        }
    }

    // Speak complete sentences as they arrive so audio starts before LLM finishes
    private func flushTTSSentences() {
        let terminators = [". ", "! ", "? ", ".\n", "!\n", "?\n"]
        var progressed  = true
        while progressed {
            progressed = false
            for term in terminators {
                if let range = ttsBuf.range(of: term) {
                    let sentence = String(ttsBuf[..<range.upperBound])
                                        .trimmingCharacters(in: .whitespaces)
                    if sentence.count > 3 { speaker.speak(sentence) }
                    ttsBuf    = String(ttsBuf[range.upperBound...])
                    progressed = true
                    break
                }
            }
        }
    }
}
