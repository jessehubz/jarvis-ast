import SwiftUI
import AppKit

// MARK: - Progress overlay controller

final class ProgressOverlayController {
    static let shared = ProgressOverlayController()
    private var panel: NSPanel?

    func show() {
        guard panel == nil else { return }

        let w: CGFloat = 360
        let h: CGFloat = 520

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: w, height: h),
            styleMask:   [.borderless, .nonactivatingPanel],
            backing:     .buffered,
            defer:       false
        )
        panel.level              = .floating
        panel.backgroundColor    = .clear
        panel.isOpaque           = false
        panel.hasShadow          = false
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        let host = NSHostingView(rootView: ProgressOverlayView())
        host.frame = NSRect(x: 0, y: 0, width: w, height: h)
        host.autoresizingMask = [.width, .height]
        panel.contentView = host

        if let screen = NSScreen.main {
            let x = screen.visibleFrame.maxX - w - 20
            let y = screen.visibleFrame.minY + 20
            panel.setFrameOrigin(NSPoint(x: x, y: y))
        }

        panel.orderFront(nil)
        self.panel = panel
    }
}

// MARK: - Progress overlay view

struct ProgressOverlayView: View {
    @ObservedObject private var vm = VoiceViewModel.shared
    @State private var elapsed: Int = 0
    @State private var minimized = false

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0).allowsHitTesting(false)

            if vm.taskActive {
                card
                    .transition(.asymmetric(
                        insertion: .move(edge: .bottom).combined(with: .opacity),
                        removal:   .move(edge: .bottom).combined(with: .opacity)
                    ))
                    .padding(.bottom, 14)
                    .padding(.horizontal, 4)
            }
        }
        .animation(.spring(response: 0.42, dampingFraction: 0.78), value: vm.taskActive)
        .onReceive(Timer.publish(every: 1, on: .main, in: .common).autoconnect()) { _ in
            if vm.taskActive { elapsed += 1 }
        }
        .onChange(of: vm.taskActive) { active in
            if active { elapsed = 0; minimized = false }
        }
    }

    // MARK: Card

    private var card: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.bottom, 14)

            if !minimized {
                taskNameView
                    .padding(.bottom, 16)
                stepList
                    .padding(.bottom, 16)
                progressSection
            }
        }
        .padding(.horizontal, 18)
        .padding(.vertical, 16)
        .background(cardBG)
        .shadow(color: .black.opacity(0.65), radius: 28, x: 0, y: 12)
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 8) {
            Circle()
                .fill(Color(hex: "4ade80"))
                .frame(width: 8, height: 8)
                .shadow(color: Color(hex: "4ade80").opacity(0.75), radius: 5)
            Text("Working on task")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.white.opacity(0.45))
            Spacer()
            Text(timeString)
                .font(.system(size: 11).monospacedDigit())
                .foregroundStyle(.white.opacity(0.3))
            Button(action: {
                withAnimation(.spring(response: 0.3, dampingFraction: 0.8)) {
                    minimized.toggle()
                }
            }) {
                Image(systemName: minimized
                      ? "arrow.up.left.and.arrow.down.right"
                      : "arrow.down.right.and.arrow.up.left")
                    .font(.system(size: 9, weight: .medium))
                    .foregroundStyle(.white.opacity(0.28))
                    .frame(width: 22, height: 22)
                    .background(Circle().fill(Color.white.opacity(0.06)))
            }
            .buttonStyle(.plain)
        }
    }

    // MARK: Task name

    private var taskNameView: some View {
        Text(vm.taskName.isEmpty ? "Task running…" : vm.taskName)
            .font(.system(size: 20, weight: .bold))
            .foregroundStyle(.white)
            .lineLimit(2)
            .fixedSize(horizontal: false, vertical: true)
    }

    // MARK: Step list

    private var stepList: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(vm.taskSteps) { step in
                OverlayStepRow(step: step)
            }
        }
    }

    // MARK: Progress bar + footer

    private var progressSection: some View {
        let done  = vm.taskSteps.filter { $0.status == .done }.count
        let total = max(1, vm.taskSteps.count)

        return VStack(alignment: .leading, spacing: 8) {
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 3)
                        .fill(Color.white.opacity(0.07))
                    RoundedRectangle(cornerRadius: 3)
                        .fill(
                            LinearGradient(
                                colors: [Color(hex: "4ade80"), Color(hex: "22d3ee")],
                                startPoint: .leading, endPoint: .trailing
                            )
                        )
                        .frame(width: geo.size.width * CGFloat(done) / CGFloat(total))
                        .animation(.easeInOut(duration: 0.35), value: done)
                }
            }
            .frame(height: 6)

            HStack {
                Text("\(done) of \(vm.taskSteps.count) complete")
                    .font(.system(size: 11))
                    .foregroundStyle(.white.opacity(0.32))
                Spacer()
                Button("Cancel") { vm.cancelTask() }
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(Color(hex: "f87171"))
                    .buttonStyle(.plain)
            }
        }
    }

    private var cardBG: some View {
        RoundedRectangle(cornerRadius: 18, style: .continuous)
            .fill(Color(hex: "0e0e18").opacity(0.97))
            .overlay(
                RoundedRectangle(cornerRadius: 18, style: .continuous)
                    .stroke(Color.white.opacity(0.07), lineWidth: 1)
            )
    }

    private var timeString: String {
        String(format: "%d:%02d", elapsed / 60, elapsed % 60)
    }
}

// MARK: - Overlay step row

struct OverlayStepRow: View {
    let step: TaskStep

    var body: some View {
        HStack(spacing: 12) {
            stepIcon.frame(width: 22, height: 22)
            Text(step.name)
                .font(.system(size: 13))
                .foregroundStyle(labelColor)
                .strikethrough(step.status == .done, color: labelColor)
                .lineLimit(1)
        }
    }

    @ViewBuilder private var stepIcon: some View {
        switch step.status {
        case .pending:
            Circle()
                .stroke(
                    Color.white.opacity(0.18),
                    style: StrokeStyle(lineWidth: 1.5, dash: [3, 2])
                )
        case .active:
            ZStack {
                Circle().fill(Color(hex: "facc15").opacity(0.12))
                Circle().stroke(Color(hex: "facc15"), lineWidth: 1.5)
            }
            .shadow(color: Color(hex: "facc15").opacity(0.55), radius: 5)
        case .done:
            ZStack {
                Circle().fill(Color(hex: "4ade80").opacity(0.12))
                Image(systemName: "checkmark")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color(hex: "4ade80"))
            }
        case .failed:
            ZStack {
                Circle().fill(Color(hex: "f87171").opacity(0.12))
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Color(hex: "f87171"))
            }
        }
    }

    private var labelColor: Color {
        switch step.status {
        case .pending: return .white.opacity(0.28)
        case .active:  return .white
        case .done:    return .white.opacity(0.38)
        case .failed:  return Color(hex: "f87171")
        }
    }
}
