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

// MARK: - Task

enum TaskStepStatus {
    case pending, active, done, failed
}

struct TaskStep: Identifiable {
    var id: String
    var name: String
    var status: TaskStepStatus = .pending
    var message: String = ""
}

struct RecentTask: Identifiable {
    var id: UUID = UUID()
    var name: String
    var completedAt: Date
    var success: Bool
}
