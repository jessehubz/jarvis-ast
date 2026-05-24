import SwiftUI
import AppKit

// MARK: - Floating panel

private final class FloatingPanel: NSPanel {
    override var canBecomeKey:  Bool { true  }
    override var canBecomeMain: Bool { false }

    // Become key on click so the text field can receive keyboard events
    // without fully activating the app (.nonactivatingPanel stays in effect).
    override func mouseDown(with event: NSEvent) {
        if !isKeyWindow { makeKey() }
        super.mouseDown(with: event)
    }
}

// MARK: - Island controller

final class IslandController {
    static let shared = IslandController()
    private var panel: FloatingPanel?

    func show() {
        guard panel == nil else { return }

        let w: CGFloat = 380
        let h: CGFloat = 580

        let panel = FloatingPanel(
            contentRect: NSRect(x: 0, y: 0, width: w, height: h),
            styleMask:   [.borderless, .nonactivatingPanel],
            backing:     .buffered,
            defer:       false
        )
        panel.level                       = .floating
        panel.backgroundColor             = .clear
        panel.isOpaque                    = false
        panel.hasShadow                   = false
        panel.acceptsMouseMovedEvents     = true
        panel.isMovableByWindowBackground = false
        panel.collectionBehavior          = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        let host = NSHostingView(rootView: IslandView())
        host.frame = NSRect(x: 0, y: 0, width: w, height: h)
        host.autoresizingMask = [.width, .height]
        panel.contentView = host

        if let screen = NSScreen.main {
            let x = (screen.frame.width - w) / 2
            let y = screen.visibleFrame.maxY - 6 - h
            panel.setFrameOrigin(NSPoint(x: x, y: y))
        }

        panel.orderFront(nil)
        self.panel = panel
    }
}

// MARK: - Island view

struct IslandView: View {
    @ObservedObject private var vm = VoiceViewModel.shared
    @State private var expanded    = false
    @State private var inputText   = ""
    @State private var collapseWork: DispatchWorkItem? = nil

    var body: some View {
        VStack(spacing: 0) {
            island.padding(.top, 6)
            Spacer(minLength: 0).allowsHitTesting(false)
        }
    }

    // MARK: Island shell

    private var island: some View {
        VStack(spacing: 0) {
            pillRow
            if expanded {
                expandedContent
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .frame(width: expanded ? 360 : 230)
        .background(islandBG)
        .clipShape(RoundedRectangle(cornerRadius: expanded ? 20 : 22, style: .continuous))
        .shadow(color: .black.opacity(0.6), radius: expanded ? 28 : 14, x: 0, y: expanded ? 12 : 6)
        // Liquid spring: slow response + slight underdamping = fluid feel
        .animation(.spring(response: 0.5, dampingFraction: 0.68), value: expanded)
        .onHover { hovering in
            collapseWork?.cancel()
            if hovering {
                withAnimation(.spring(response: 0.5, dampingFraction: 0.68)) { expanded = true }
            } else {
                let work = DispatchWorkItem {
                    withAnimation(.spring(response: 0.45, dampingFraction: 0.72)) { self.expanded = false }
                }
                collapseWork = work
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.25, execute: work)
            }
        }
        .frame(maxWidth: 380, alignment: .center)
    }

    // MARK: Pill row

    private var pillRow: some View {
        HStack(spacing: 8) {
            StatusDot(mode: vm.mode)

            if expanded {
                Text("JARVIS")
                    .font(.system(size: 11, weight: .black))
                    .tracking(4)
                    .foregroundStyle(.white)
                    .transition(.opacity)
            } else {
                Text(modeLabel)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.white.opacity(0.82))
                    .transition(.opacity)
            }

            Spacer(minLength: 0)

            if expanded {
                Button(action: {}) {
                    Image(systemName: "gear")
                        .font(.system(size: 12))
                        .foregroundStyle(.white.opacity(0.45))
                        .frame(width: 26, height: 26)
                        .background(Circle().fill(Color.white.opacity(0.07)))
                }
                .buttonStyle(.plain)
                .transition(.opacity)
            } else if vm.mode == .think {
                thinkingDots.transition(.opacity)
            }
        }
        .padding(.horizontal, 14)
        .frame(height: 36)
        .animation(.easeInOut(duration: 0.18), value: expanded)
    }

    // MARK: Expanded content

    private var expandedContent: some View {
        VStack(spacing: 0) {
            // Status chip
            statusChip

            if !vm.messages.isEmpty {
                divider
                messagesSection
            }

            if vm.taskActive || !vm.taskSteps.isEmpty {
                divider
                taskSection
            }

            divider
            inputRow
        }
    }

    // MARK: Status chip

    private var statusChip: some View {
        HStack(spacing: 6) {
            Text(vm.statusText)
                .font(.system(size: 10))
                .foregroundStyle(Color(hex: "555555"))
                .lineLimit(1)
            Spacer()
            Text(modeLabel.uppercased())
                .font(.system(size: 9, weight: .semibold))
                .tracking(1.5)
                .foregroundStyle(modeAccentColor.opacity(0.7))
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
    }

    // MARK: Messages (last 4 = 2 exchanges)

    private var messagesSection: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(Array(vm.messages.suffix(4))) { msg in
                CompactMsgRow(msg: msg)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
    }

    // MARK: Task checklist

    private var taskSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "cpu")
                    .font(.system(size: 9))
                    .foregroundStyle(Color(hex: "444444"))
                Text(vm.taskName.isEmpty ? "Running task…" : vm.taskName)
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(Color(hex: "777777"))
                    .lineLimit(1)
                Spacer(minLength: 0)
                if vm.taskActive {
                    Button("Cancel") { vm.cancelTask() }
                        .font(.system(size: 9))
                        .foregroundStyle(Color(hex: "f87171"))
                        .buttonStyle(.plain)
                }
            }

            ForEach(Array(vm.taskSteps.prefix(7))) { step in
                InlineStepRow(step: step)
            }
            if vm.taskSteps.count > 7 {
                Text("+ \(vm.taskSteps.count - 7) more")
                    .font(.system(size: 9))
                    .foregroundStyle(Color(hex: "3a3a3a"))
                    .padding(.leading, 16)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
    }

    // MARK: Input

    private var inputRow: some View {
        HStack(spacing: 8) {
            TextField("Ask Jarvis anything…", text: $inputText)
                .textFieldStyle(.plain)
                .font(.system(size: 12))
                .foregroundStyle(.white)
                .onSubmit { sendMessage() }

            Button(action: sendMessage) {
                Image(systemName: "arrow.up")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color(hex: "aaaaaa"))
                    .frame(width: 22, height: 22)
                    .background(Circle().fill(Color(hex: "252525")))
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    // MARK: Helpers

    private var divider: some View {
        Rectangle().fill(Color.white.opacity(0.055)).frame(height: 1)
    }

    private var islandBG: some View {
        RoundedRectangle(cornerRadius: expanded ? 20 : 22, style: .continuous)
            .fill(Color.black.opacity(0.93))
            .overlay(
                RoundedRectangle(cornerRadius: expanded ? 20 : 22, style: .continuous)
                    .stroke(Color.white.opacity(0.09), lineWidth: 1)
            )
    }

    private var modeLabel: String {
        switch vm.mode {
        case .offline: return "Offline"
        case .idle:    return "Jarvis"
        case .listen:  return "Listening"
        case .record:  return "Recording"
        case .think:   return "Thinking"
        case .speak:   return "Speaking"
        }
    }

    private var modeAccentColor: Color {
        switch vm.mode {
        case .listen, .idle: return Color(hex: "4ade80")
        case .record:        return Color(hex: "f87171")
        case .think:         return Color(hex: "facc15")
        case .speak:         return Color(hex: "818cf8")
        case .offline:       return Color(hex: "555555")
        }
    }

    private var thinkingDots: some View {
        HStack(spacing: 3) {
            ForEach(0..<3, id: \.self) { _ in
                Circle().fill(Color(hex: "facc15").opacity(0.5)).frame(width: 3, height: 3)
            }
        }
    }

    private func sendMessage() {
        let t = inputText.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return }
        inputText = ""
        vm.sendText(t)
    }
}

// MARK: - Compact message bubble

struct CompactMsgRow: View {
    let msg: ChatMessage

    var body: some View {
        HStack(alignment: .top, spacing: 7) {
            if msg.role == .jarvis {
                ZStack {
                    Circle().fill(Color(hex: "1e1e1e"))
                    Text("J").font(.system(size: 7, weight: .bold)).foregroundStyle(Color(hex: "777777"))
                }
                .frame(width: 14, height: 14)
                .padding(.top, 1)
            }

            Text(msg.text.isEmpty ? "…" : msg.text)
                .font(.system(size: 11))
                .foregroundStyle(msg.role == .user ? .white.opacity(0.88) : Color(hex: "bebebe"))
                .lineLimit(2)
                .frame(maxWidth: .infinity, alignment: msg.role == .user ? .trailing : .leading)
                .multilineTextAlignment(msg.role == .user ? .trailing : .leading)

            if msg.role == .user {
                Circle().fill(Color(hex: "303030")).frame(width: 14, height: 14).padding(.top, 1)
            }
        }
    }
}

// MARK: - Inline step row

struct InlineStepRow: View {
    let step: TaskStep

    var body: some View {
        HStack(spacing: 8) {
            stepIcon.frame(width: 10, height: 10)
            Text(step.name)
                .font(.system(size: 10))
                .foregroundStyle(labelColor)
                .lineLimit(1)
        }
    }

    @ViewBuilder private var stepIcon: some View {
        switch step.status {
        case .pending:
            Circle().stroke(Color(hex: "333333"), lineWidth: 1)
        case .active:
            Circle().fill(Color(hex: "facc15"))
                .shadow(color: Color(hex: "facc15").opacity(0.7), radius: 3)
        case .done:
            Image(systemName: "checkmark").font(.system(size: 7, weight: .bold)).foregroundStyle(Color(hex: "4ade80"))
        case .failed:
            Image(systemName: "xmark").font(.system(size: 7, weight: .bold)).foregroundStyle(Color(hex: "f87171"))
        }
    }

    private var labelColor: Color {
        switch step.status {
        case .pending: return Color(hex: "3e3e3e")
        case .active:  return .white
        case .done:    return Color(hex: "4ade80")
        case .failed:  return Color(hex: "f87171")
        }
    }
}
