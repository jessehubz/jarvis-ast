import SwiftUI
import AppKit

// MARK: - Main view

struct ContentView: View {
    @ObservedObject private var vm = VoiceViewModel.shared
    @State private var input    = ""

    var body: some View {
        ZStack {
            // ── Glassmorphism background ──────────────────────────────────────
            VisualEffectView(material: .underWindowBackground, blendingMode: .behindWindow)
                .ignoresSafeArea()
            Color.black.opacity(0.78).ignoresSafeArea()

            HStack(spacing: 0) {
                sidebar
                Divider().background(Color(hex: "1c1c1c")).frame(width: 1)
                mainColumn
            }
        }
        .preferredColorScheme(.dark)
    }

    // MARK: Sidebar

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            // macOS traffic lights live here; just add drag + logo
            GlassDragArea()
                .frame(height: 52)
                .overlay(alignment: .bottomLeading) {
                    Text("JARVIS")
                        .font(.system(size: 11, weight: .black))
                        .tracking(5)
                        .foregroundStyle(.white)
                        .padding(.leading, 18).padding(.bottom, 14)
                }

            // Nav
            VStack(alignment: .leading, spacing: 3) {
                NavBtn(label: "Chat",     active: true)
                NavBtn(label: "History",  active: false)
                NavBtn(label: "Settings", active: false)
            }
            .padding(.horizontal, 10)

            Spacer()

            // Status
            HStack(alignment: .top, spacing: 8) {
                StatusDot(mode: vm.mode).padding(.top, 3)
                Text(vm.statusText)
                    .font(.system(size: 11))
                    .foregroundStyle(Color(hex: "555555"))
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 14).padding(.bottom, 20)
        }
        .frame(width: 188)
        .background(Color(hex: "0d0d0d").opacity(0.55))
    }

    // MARK: Main column

    private var mainColumn: some View {
        VStack(spacing: 0) {
            // Title bar
            GlassDragArea()
                .frame(height: 48)
                .overlay {
                    Text("Chat")
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(Color(hex: "444444"))
                }
                .overlay(alignment: .bottom) {
                    Color(hex: "161616").frame(height: 1)
                }

            // Messages
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 12) {
                        ForEach(vm.messages) { msg in
                            MessageRow(msg: msg).id(msg.id)
                        }
                        Color.clear.frame(height: 1).id("_bottom")
                    }
                    .padding(.horizontal, 20).padding(.vertical, 16)
                }
                .onChange(of: vm.messages.count)      { _ in scroll(proxy) }
                .onChange(of: vm.messages.last?.text)  { _ in scroll(proxy) }
            }

            // Input bar
            HStack(spacing: 8) {
                // Inner HStack gives FocusableTextField the full 40pt pill height.
                // Without this, the NSTextField frame is only ~20pt tall and clicks
                // on the top/bottom padding area of the visual pill miss it entirely.
                HStack(spacing: 0) {
                    FocusableTextField(
                        text: $input,
                        placeholder: "Type a message or say \"hey jarvis\"…",
                        onSubmit: send
                    )
                    .padding(.horizontal, 16)
                }
                .frame(height: 40)
                .background(glassField)

                Button(action: send) {
                    Image(systemName: "arrow.up")
                        .font(.system(size: 14, weight: .bold))
                        .foregroundStyle(Color(hex: "aaaaaa"))
                }
                .buttonStyle(.plain)
                .frame(width: 36, height: 36)
                .background(glassCircle)
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            .overlay(alignment: .top) { Color(hex: "161616").frame(height: 1) }
        }
    }

    // MARK: Helpers

    private func send() {
        let t = input.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty else { return }
        input = ""
        vm.sendText(t)
    }

    private func scroll(_ proxy: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo("_bottom") }
    }

    private var glassField: some View {
        RoundedRectangle(cornerRadius: 20)
            .fill(Color(hex: "111111"))
            .overlay(RoundedRectangle(cornerRadius: 20).stroke(Color(hex: "242424"), lineWidth: 1))
    }
    private var glassCircle: some View {
        Circle()
            .fill(Color(hex: "1a1a1a"))
            .overlay(Circle().stroke(Color(hex: "2a2a2a"), lineWidth: 1))
    }
}

// MARK: - Message row

struct MessageRow: View {
    let msg: ChatMessage

    var body: some View {
        HStack(alignment: .bottom, spacing: 8) {
            if msg.role == .user {
                Spacer(minLength: 60)
                bubble
            } else {
                avatar
                bubble
                Spacer(minLength: 60)
            }
        }
    }

    @ViewBuilder private var bubble: some View {
        let isUser = msg.role == .user
        Group {
            if msg.text.isEmpty && msg.isStreaming {
                cursorView.padding(.vertical, 12).padding(.horizontal, 14)
            } else {
                Text(msg.text)
                    .font(.system(size: 14)).lineSpacing(4)
                    .foregroundStyle(isUser ? Color.black : textColor)
                    .textSelection(.enabled)
                    .padding(.horizontal, 14).padding(.vertical, 10)
            }
        }
        .frame(maxWidth: 480, alignment: isUser ? .trailing : .leading)
        .background(bubbleBG(isUser: isUser))
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }

    private var cursorView: some View {
        RoundedRectangle(cornerRadius: 1.5)
            .fill(Color(hex: "555555"))
            .frame(width: 2, height: 14)
            .opacity(0.8)
    }

    private var textColor: Color {
        if msg.isError   { return Color(hex: "f87171") }
        if msg.isWarning { return Color(hex: "facc15") }
        return Color(hex: "d4d4d4")
    }

    private func bubbleBG(isUser: Bool) -> some View {
        Group {
            if isUser {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(Color.white)
            } else {
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .fill(Color(hex: "141414"))
                    .overlay(
                        RoundedRectangle(cornerRadius: 16, style: .continuous)
                            .stroke(Color(hex: "1e1e1e"), lineWidth: 1)
                    )
            }
        }
    }

    private var avatar: some View {
        ZStack {
            Circle().fill(Color(hex: "1a1a1a"))
            Circle().stroke(Color(hex: "2a2a2a"), lineWidth: 1)
            Text("J").font(.system(size: 11, weight: .semibold)).foregroundStyle(Color(hex: "888888"))
        }
        .frame(width: 26, height: 26)
    }
}

// MARK: - Status dot

struct StatusDot: View {
    let mode: Mode

    private var color: Color {
        switch mode {
        case .offline: return Color(hex: "333333")
        case .idle:    return Color(hex: "444444")
        case .listen:  return Color(hex: "4ade80")
        case .record:  return Color(hex: "f87171")
        case .think:   return Color(hex: "facc15")
        case .speak:   return Color(hex: "818cf8")
        }
    }

    var body: some View {
        Circle().fill(color).frame(width: 7, height: 7)
            .shadow(color: color.opacity(
                mode == .idle || mode == .offline ? 0 : 0.7), radius: 5)
    }
}

// MARK: - Nav button

struct NavBtn: View {
    let label: String
    let active: Bool

    var body: some View {
        Text(label)
            .font(.system(size: 13))
            .foregroundStyle(active ? Color(hex: "e8e8e8") : Color(hex: "555555"))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 10).padding(.vertical, 8)
            .background {
                if active {
                    RoundedRectangle(cornerRadius: 7)
                        .fill(Color(hex: "1a1a1a"))
                        .overlay(RoundedRectangle(cornerRadius: 7)
                            .stroke(Color(hex: "2a2a2a"), lineWidth: 1))
                }
            }
    }
}

// MARK: - Native text field
// SwiftUI's TextField returns false for acceptsFirstMouse, so clicking it in a
// non-key window activates the window but does NOT start editing — the user has
// to click a second time. Subclassing NSTextField and overriding acceptsFirstMouse
// makes the first click both activate the window and begin editing.

private struct FocusableTextField: NSViewRepresentable {
    @Binding var text: String
    let placeholder: String
    let onSubmit: () -> Void

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> FirstMouseTextField {
        let f = FirstMouseTextField()
        f.placeholderAttributedString = NSAttributedString(
            string: placeholder,
            attributes: [.foregroundColor: NSColor(white: 1, alpha: 0.28),
                         .font: NSFont.systemFont(ofSize: 14)]
        )
        f.isBordered      = false
        f.drawsBackground = false
        f.focusRingType   = .none
        f.font            = .systemFont(ofSize: 14)
        f.textColor       = .white
        f.delegate        = context.coordinator
        return f
    }

    func updateNSView(_ v: FirstMouseTextField, context: Context) {
        if v.stringValue != text { v.stringValue = text }
        context.coordinator.parent = self
    }

    final class FirstMouseTextField: NSTextField {
        override class var cellClass: AnyClass? {
            get { CenteredCell.self }
            set {}
        }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    }

    // Vertically centers the text within whatever height the container gives the field.
    private final class CenteredCell: NSTextFieldCell {
        private func centeredRect(for bounds: NSRect) -> NSRect {
            let h = cellSize(forBounds: bounds).height
            return bounds.insetBy(dx: 0, dy: max(0, (bounds.height - h) / 2))
        }
        override func drawingRect(forBounds r: NSRect) -> NSRect { centeredRect(for: r) }
        override func titleRect(forBounds r: NSRect) -> NSRect   { centeredRect(for: r) }
        override func edit(withFrame r: NSRect, in v: NSView, editor e: NSText, delegate d: Any?, event evt: NSEvent?) {
            super.edit(withFrame: centeredRect(for: r), in: v, editor: e, delegate: d, event: evt)
        }
        override func select(withFrame r: NSRect, in v: NSView, editor e: NSText, delegate d: Any?, start s: Int, length l: Int) {
            super.select(withFrame: centeredRect(for: r), in: v, editor: e, delegate: d, start: s, length: l)
        }
    }

    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: FocusableTextField
        init(_ p: FocusableTextField) { parent = p }

        func controlTextDidChange(_ n: Notification) {
            guard let f = n.object as? NSTextField else { return }
            parent.text = f.stringValue
        }

        func control(_: NSControl, textView _: NSTextView, doCommandBy cmd: Selector) -> Bool {
            guard cmd == #selector(NSResponder.insertNewline(_:)) else { return false }
            parent.onSubmit()
            return true
        }
    }
}

// MARK: - Drag area (NSView trick for window dragging)

struct GlassDragArea: NSViewRepresentable {
    func makeNSView(context: Context) -> DraggableView { DraggableView() }
    func updateNSView(_ v: DraggableView, context: Context) {}
}

final class DraggableView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func draw(_ r: NSRect) {} // transparent
}

// MARK: - NSVisualEffectView wrapper (true glassmorphism)

struct VisualEffectView: NSViewRepresentable {
    var material:     NSVisualEffectView.Material
    var blendingMode: NSVisualEffectView.BlendingMode

    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material     = material
        v.blendingMode = blendingMode
        v.state        = .active
        return v
    }
    func updateNSView(_ v: NSVisualEffectView, context: Context) {
        v.material     = material
        v.blendingMode = blendingMode
    }
}

// MARK: - Color helper

extension Color {
    init(hex: String) {
        let h = hex.trimmingCharacters(in: CharacterSet.alphanumerics.inverted)
        var val: UInt64 = 0
        Scanner(string: h).scanHexInt64(&val)
        let r = Double((val >> 16) & 0xFF) / 255
        let g = Double((val >>  8) & 0xFF) / 255
        let b = Double( val        & 0xFF) / 255
        self.init(red: r, green: g, blue: b)
    }
}
