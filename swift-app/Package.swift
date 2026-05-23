// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "Jarvis",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "Jarvis",
            path: "Sources"
        )
    ]
)
