// Whether an emulator's window is actually drawn (Tier C, C7.7): lab_devices.py's window step.
// On 2026-09-23 three emulators booted — adb saw sys.boot_completed = 1 — and never drew a window,
// so readiness is read from the window server, not from the device.
//
//     swift labs/harness/avd/windows.swift <pid>...
//
// Prints `<pid> <width>x<height> <on-screen 1|0>` for each window those processes own in the normal
// window layer (0) that is not empty.  Only an on-screen one counts as drawn; the others are printed
// so a failure can say what there is: a minimized window, or one on another Space, is not on screen.
import CoreGraphics

let pids = Set(CommandLine.arguments.dropFirst().compactMap { Int($0) })
let windows = (CGWindowListCopyWindowInfo([.optionAll], kCGNullWindowID) as? [[String: Any]]) ?? []
for w in windows {
    guard let pid = w[kCGWindowOwnerPID as String] as? Int, pids.contains(pid),
          w[kCGWindowLayer as String] as? Int == 0,
          let bounds = w[kCGWindowBounds as String] as? [String: Any],
          let width = bounds["Width"] as? Double, let height = bounds["Height"] as? Double,
          width > 0, height > 0 else { continue }
    let onscreen = w[kCGWindowIsOnscreen as String] as? Bool ?? false
    print("\(pid) \(Int(width))x\(Int(height)) \(onscreen ? 1 : 0)")
}
