import SwiftUI
import AppKit

// MARK: - Status dot (used by IslandPanel)

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
            .shadow(color: color.opacity(mode == .idle || mode == .offline ? 0 : 0.75), radius: 5)
    }
}

// MARK: - Custom blur view

struct CustomBlurView: NSViewRepresentable {
    var material:     NSVisualEffectView.Material
    var blendingMode: NSVisualEffectView.BlendingMode
    var state:        NSVisualEffectView.State = .active

    func makeNSView(context: Context) -> NSVisualEffectView {
        let v = NSVisualEffectView()
        v.material     = material
        v.blendingMode = blendingMode
        v.state        = state
        v.wantsLayer   = true
        return v
    }

    func updateNSView(_ v: NSVisualEffectView, context: Context) {
        v.material     = material
        v.blendingMode = blendingMode
        v.state        = state
    }
}

// MARK: - Drag area

struct GlassDragArea: NSViewRepresentable {
    func makeNSView(context: Context) -> DraggableView { DraggableView() }
    func updateNSView(_ v: DraggableView, context: Context) {}
}

final class DraggableView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
    override func draw(_ r: NSRect) {}
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
