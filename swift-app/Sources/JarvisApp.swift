import SwiftUI
import AppKit

@main
struct JarvisApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    // Retained for the lifetime of the app
    private var clickMonitor: Any?

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.main.async { [self] in
            guard let win = NSApp.windows.first(where: { !($0 is NSPanel) }) else { return }
            win.titlebarAppearsTransparent  = true
            win.titleVisibility             = .hidden
            win.setContentSize(NSSize(width: 860, height: 620))
            win.minSize = NSSize(width: 720, height: 500)
            win.center()
            // participatesInCycle ensures cmd+tab brings this window into focus
            win.collectionBehavior = [.managed, .participatesInCycle]

            IslandController.shared.show()

            // Force-activate the app so the main window is always in front on launch,
            // not hidden behind whatever was active before.
            NSApp.activate(ignoringOtherApps: true)
            win.makeKeyAndOrderFront(nil)

            // When the island panel is clicked it becomes key WITHOUT activating the app
            // (.nonactivatingPanel). This monitor fires before event dispatch so that any
            // click anywhere in the main window makes it key first — one click to type.
            clickMonitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { event in
                if let w = event.window, !(w is NSPanel), !w.isKeyWindow {
                    w.makeKeyAndOrderFront(nil)
                }
                return event
            }
        }
    }

    // Called when user cmd+tabs back to Jarvis or clicks its Dock icon.
    // The island's .nonactivatingPanel means using the island never activates the app,
    // so the main window can end up behind other apps. Re-asserting it here ensures
    // the chat window always comes to front when the user switches back.
    func applicationDidBecomeActive(_ notification: Notification) {
        NSApp.windows.first(where: { !($0 is NSPanel) })?.makeKeyAndOrderFront(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let m = clickMonitor { NSEvent.removeMonitor(m) }
    }
}
