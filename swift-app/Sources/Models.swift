import Foundation

// MARK: - Mode

enum Mode {
    case offline, idle, listen, record, think, speak
}

// MARK: - Message

struct ChatMessage: Identifiable {
    var id: UUID = UUID()
    var role: Role
    var text: String
    var isStreaming: Bool = false
    var isWarning: Bool = false
    var isError: Bool = false

    enum Role { case user, jarvis }
}
