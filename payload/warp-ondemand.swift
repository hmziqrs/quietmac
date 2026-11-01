// warp-ondemand: run Cloudflare WARP's root daemon only while the WARP app is
// open. Driven by key-value observation of NSWorkspace.runningApplications, so
// it sleeps in the run loop between app launches/quits and costs no CPU. (The
// didLaunch/didTerminate notifications are not delivered for WARP: it is an
// LSUIElement menu-bar app.)
//
//   you open the app          -> enable + load the daemon
//   the app quits             -> disconnect, unload + disable the daemon, and
//                                delete the login item the app re-adds on launch
//   app auto-starts at login  -> quit it (login item / reopen-at-login)
//
// Privileged launchctl calls go through `sudo -n`, which
// /etc/sudoers.d/quietmac-warp allows without a password for exactly the
// four commands used here. Installed and built by `quietmac apply warp-on-demand`.
//
// Build: swiftc -O -swift-version 5 -o warp-ondemand warp-ondemand.swift

import AppKit

let bundleID = "com.cloudflare.1dot1dot1dot1.macos"
let appName = "Cloudflare WARP"
let label = "com.cloudflare.1dot1dot1dot1.macos.warp.daemon"
let plist = "/Library/LaunchDaemons/\(label).plist"
let warpCLI = "/usr/local/bin/warp-cli"
let logPath = ProcessInfo.processInfo.environment["WARP_ONDEMAND_LOG"]
    ?? NSString(string: "~/Library/Logs/quietmac/warp-ondemand.log").expandingTildeInPath
// WARP launches this soon after login come from its login item or from macOS
// reopening apps, not from you.
let loginGrace: TimeInterval = 90

let stamp: DateFormatter = {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd HH:mm:ss"
    return f
}()

func log(_ message: String) {
    try? FileManager.default.createDirectory(atPath: (logPath as NSString).deletingLastPathComponent,
                                             withIntermediateDirectories: true)
    let data = "\(stamp.string(from: Date())) \(message)\n".data(using: .utf8)!
    if let h = FileHandle(forWritingAtPath: logPath) {
        h.seekToEndOfFile()
        h.write(data)
        h.closeFile()
    } else {
        FileManager.default.createFile(atPath: logPath, contents: data)
    }
}

@discardableResult
func run(_ path: String, _ args: [String]) -> Int32 {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: path)
    p.arguments = args
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    do {
        try p.run()
    } catch {
        log("failed to run \(path): \(error)")
        return -1
    }
    p.waitUntilExit()
    return p.terminationStatus
}

@discardableResult
func sudoLaunchctl(_ args: String...) -> Int32 {
    run("/usr/bin/sudo", ["-n", "/bin/launchctl"] + args)
}

func daemonLoaded() -> Bool {
    run("/bin/launchctl", ["print", "system/\(label)"]) == 0
}

func daemonUp() {
    if daemonLoaded() { return }
    let enable = sudoLaunchctl("enable", "system/\(label)")
    let boot = sudoLaunchctl("bootstrap", "system", plist)
    log("daemon up (enable=\(enable) bootstrap=\(boot))")
}

func daemonDown() {
    if daemonLoaded() {
        // Disconnect first so the daemon restores DNS and routes before it goes.
        run(warpCLI, ["--accept-tos", "disconnect"])
        let out = sudoLaunchctl("bootout", "system/\(label)")
        log("daemon down (bootout=\(out))")
    }
    sudoLaunchctl("disable", "system/\(label)")
}

func removeLoginItem() {
    run("/usr/bin/osascript", ["-e",
        "tell application \"System Events\" to if exists login item \"\(appName)\" then delete login item \"\(appName)\""])
}

func runningWARP() -> NSRunningApplication? {
    NSWorkspace.shared.runningApplications.first { $0.bundleIdentifier == bundleID && !$0.isTerminated }
}

func quitAutoStarted(_ app: NSRunningApplication) {
    log("WARP auto-started at login (pid \(app.processIdentifier)), quitting it")
    app.terminate()
    DispatchQueue.main.asyncAfter(deadline: .now() + 5) {
        if !app.isTerminated { app.forceTerminate() }
    }
    // The quit notification then unloads the daemon and drops the login item.
}

let start = Date()
// launchd starts this agent at login; a later start is a crash restart or a
// manual reload, which must not kill a WARP session you opened.
let sessionAge = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.loginwindow")
    .first?.launchDate.map { start.timeIntervalSince($0) } ?? ProcessInfo.processInfo.systemUptime
let atLogin = sessionAge < 120

func inLoginGrace() -> Bool {
    atLogin && Date().timeIntervalSince(start) < loginGrace
}

var trackedPID: pid_t? = nil

// Fires on every app launch or quit; acts only when WARP's presence changes.
func sync() {
    let app = runningWARP()
    let pid = app?.processIdentifier
    guard pid != trackedPID else { return }
    trackedPID = pid
    if let app = app {
        if inLoginGrace() {
            quitAutoStarted(app)
            return
        }
        log("WARP opened (pid \(app.processIdentifier))")
        daemonUp()
    } else {
        log("WARP quit")
        daemonDown()
        removeLoginItem()
    }
}

log("watcher started pid=\(getpid()) atLogin=\(atLogin) sessionAge=\(Int(sessionAge))s")
if let app = runningWARP() {
    trackedPID = app.processIdentifier
    if atLogin { quitAutoStarted(app) } else { daemonUp() }
} else {
    daemonDown()
    removeLoginItem()
}

let observation = NSWorkspace.shared.observe(\.runningApplications, options: [.new]) { _, _ in
    DispatchQueue.main.async { sync() }
}
// A real (UI-less) NSApplication keeps AppKit's LaunchServices updates flowing
// into runningApplications; a bare RunLoop in a command-line tool may not.
let nsApp = NSApplication.shared
nsApp.setActivationPolicy(.prohibited)
withExtendedLifetime(observation) { nsApp.run() }
