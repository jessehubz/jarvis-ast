import SwiftUI
import AppKit

// MARK: - Keyable floating panel
// NSPanel.canBecomeKey returns false by default, which prevents any text field
// inside it from ever becoming first responder (keyboard events go nowhere).
// Overriding canBecomeKey fixes this without removing .nonactivatingPanel.

private final class FloatingPanel: NSPanel {
    override var canBecomeKey:  Bool { true  }
    override var canBecomeMain: Bool { false }
}

// MARK: - Island Window Controller

final class IslandController {
    static let shared = IslandController()
    private var panel: FloatingPanel?

    func show() {
        guard panel == nil else { return }

        // Panel is tall enough for the fully expanded island; unused bottom is transparent
        let panelW: CGFloat = 260
        let panelH: CGFloat = 390

        let panel = FloatingPanel(
            contentRect: NSRect(x: 0, y: 0, width: panelW, height: panelH),
            styleMask:   [.borderless, .nonactivatingPanel],
            backing:     .buffered,
            defer:       false
        )
        panel.level                    = .floating
        panel.backgroundColor          = .clear
        panel.isOpaque                 = false
        panel.hasShadow                = false
        panel.acceptsMouseMovedEvents  = true
        panel.isMovableByWindowBackground = false
        panel.collectionBehavior       = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        let host = NSHostingView(rootView: IslandView())
        host.frame = NSRect(x: 0, y: 0, width: panelW, height: panelH)
        host.autoresizingMask = [.width, .height]
        panel.contentView = host

        // Position: top-centre, 6 px below the menu bar
        if let screen = NSScreen.main {
            let x = (screen.frame.width - panelW) / 2
            let y = screen.visibleFrame.maxY - 6 - panelH
            panel.setFrameOrigin(NSPoint(x: x, y: y))
        }

        panel.orderFront(nil)
        self.panel = panel
    }
}

// MARK: - Island SwiftUI View

struct IslandView: View {
    @ObservedObject private var vm = VoiceViewModel.shared
    @State private var expanded  = false
    @State private var inputText = ""

    var body: some View {
        VStack(spacing: 0) {
            // Island shape anchored to top; Spacer below passes mouse events through
            islandShape
                .padding(.top, 6)
            Spacer(minLength: 0).allowsHitTesting(false)
        }
    }

    // MARK: Island shell

    private var islandShape: some View {
        VStack(spacing: 0) {
            pillRow
            if expanded {
                miniChatContent
                    .transition(.opacity.combined(with: .move(edge: .top)))
            }
        }
        .frame(width: expanded ? 260 : 210)
        .background(islandBG)
        .clipShape(RoundedRectangle(cornerRadius: expanded ? 18 : 22, style: .continuous))
        .shadow(color: .black.opacity(0.55), radius: 22, x: 0, y: 10)
        .animation(.spring(response: 0.32, dampingFraction: 0.76), value: expanded)
        .onHover { hovering in
            expanded = hovering
            // Bring the main window to front when the user starts interacting
            // with the island, so it's immediately accessible for chat.
            if hovering { activateMainWindow() }
        }
        .frame(maxWidth: 260, alignment: .center)
    }

    // MARK: Pill / status row

    private var pillRow: some View {
        HStack(spacing: 7) {
            StatusDot(mode: vm.mode)
            Text(islandLabel)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.white.opacity(0.82))
                .lineLimit(1)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 13)
        .frame(height: 34)
    }

    // MARK: Expanded mini-chat

    private var miniChatContent: some View {
        VStack(spacing: 0) {
            divider

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 6) {
                        ForEach(vm.messages.suffix(6)) { msg in
                            MiniMsgRow(msg: msg).id(msg.id)
                        }
                        Color.clear.frame(height: 1).id("_ib")
                    }
                    .padding(.horizontal, 10).padding(.vertical, 8)
                }
                .frame(height: 240)
                .onChange(of: vm.messages.count)      { _ in jump(proxy) }
                .onChange(of: vm.messages.last?.text) { _ in jump(proxy) }
            }

            divider

            // Input row
            HStack(spacing: 6) {
                TextField("Message…", text: $inputText)
                    .textFieldStyle(.plain)
                    .font(.system(size: 12))
                    .foregroundStyle(.white)
                    .onSubmit { sendIsland() }

                Button(action: sendIsland) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(Color(hex: "aaaaaa"))
                        .frame(width: 22, height: 22)
                        .background(Circle().fill(Color(hex: "2a2a2a")))
                }
                .buttonStyle(.plain)
            }
            .padding(.horizontal, 12).padding(.vertical, 9)
        }
    }

    // MARK: Helpers

    private var divider: some View {
        Rectangle().fill(Color.white.opacity(0.07)).frame(height: 1)
    }

    private var islandBG: some View {
        RoundedRectangle(cornerRadius: expanded ? 18 : 22, style: .continuous)
            .fill(Color.black.opacity(0.90))
            .overlay(
                RoundedRectangle(cornerRadius: expanded ? 18 : 22, style: .continuous)
                    .stroke(Color.white.opacity(0.09), lineWidth: 1)
            )
    }

    private var islandLabel: String {
        switch vm.mode {
        case .offline: return "Offline"
        case .idle:    return "Idle"
        case .listen:  return "Listening"
        case .record:  return "Recording"
        case .think:   return "Thinking"
        case .speak:   return "Speaking"
        }
    }

    private func jump(_ proxy: ScrollViewProxy) {
        withAnimation { proxy.scrollTo("_ib") }
    }

    private func sendIsland() {
        let t = inputText.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return }
        inputText = ""
        vm.sendText(t)
    }

    // Activates the Jarvis app and brings the main chat window to front.
    // Called when user clicks the island's input area so the main window
    // becomes accessible without a separate Dock click or cmd+tab.
    private func activateMainWindow() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.windows.first(where: { !($0 is NSPanel) })?.makeKeyAndOrderFront(nil)
    }
}

// MARK: - Compact message bubble

struct MiniMsgRow: View {
    let msg: ChatMessage

    var body: some View {
        Group {
            if msg.role == .user {
                HStack {
                    Spacer()
                    bubble(msg.text, bg: Color.white, fg: Color.black)
                }
            } else {
                HStack {
                    bubble(msg.text, bg: Color(hex: "1e1e1e"), fg: Color(hex: "cccccc"))
                    Spacer()
                }
            }
        }
    }

    private func bubble(_ text: String, bg: Color, fg: Color) -> some View {
        Text(text.isEmpty ? "…" : text)
            .font(.system(size: 11))
            .foregroundStyle(fg)
            .lineLimit(3)
            .multilineTextAlignment(msg.role == .user ? .trailing : .leading)
            .padding(.horizontal, 8).padding(.vertical, 5)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(bg))
    }
}
