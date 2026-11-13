import UIKit
import Capacitor
import TSBackgroundFetch

/// iOS host of the Capacitor fetch lab (WO-002): the AppDelegate of @capacitor/cli 8.5.3's iOS template, plus the two
/// lab plugins below.  Ported from bg-forge's labs/platforms/capacitor/app/native-ios/AppDelegate.swift (addc50501),
/// with the fetch React Native host's AppDelegate.swift and LabModule.m (labs/platforms/react-native/app/ios).  All
/// three live in this one file because it is already in the App target: build.sh copies it over the generated
/// ios/App/App/AppDelegate.swift, so neither the project file nor a pod needs a new source, and the generated tree is
/// gitignored.
///
/// The template is a UIScene app: its SceneDelegate.swift, used as generated, creates the window and the
/// CAPBridgeViewController in scene(_:willConnectTo:options:), after didFinishLaunching has returned, so the bridge and
/// every plugin load from the scene — and the plugin's own load() makes the SDK's didFinishLaunching() call there
/// (BackgroundFetchPlugin.swift load()).  A background launch for a fetch connects a scene too, in the background
/// state (MEASURED on the React Native host, ios-fetch-sim `cold`).
@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // (1) The launch, with which scenario keys argv carried (LabPlugin reads their values).  NSLog, not print:
        // os_log is the channel the harness taps (`log stream`).  The format is a literal so an argv value is never one.
        let keys = ProcessInfo.processInfo.arguments.filter { $0.hasPrefix("--") }
            .map { String($0.dropFirst(2).prefix { $0 != "=" }) }.sorted()
        let call = LabLaunchCall.value
        NSLog("%@", "LAB point=didFinishLaunching argv=\(keys) state=\(LabState.name(application.applicationState)) "
              + "sdkLaunchCall=\(call)")
        guard LabLaunchCall.values.contains(call) else {
            fatalError("LAB sdkLaunchCall=\(call) is not one of \(LabLaunchCall.values)")
        }

        // (2) The plugin's install docs (help/INSTALL-IOS.md) put the SDK's launch call here.  Its +load also registers
        // on UIApplicationDidFinishLaunchingNotification, and the plugin's load() calls it again from the scene; the
        // SDK's `launched` flag makes them one call.  (ios-registration) Unless the launch moves the call.
        if call == "delegate" {
            NSLog("%@", "LAB point=sdkLaunchCall at=delegate")
            TSBackgroundFetch.sharedInstance().didFinishLaunching()
        }

        // (3) The scene's connection, logged without touching the template's SceneDelegate.  UIKit posts this AFTER the
        // scene's delegate has run scene(_:willConnectTo:options:), which builds the bridge: `point=pluginLoad` comes
        // 15-21 ms before `point=sceneWillConnect` in every capture (MEASURED 2026-11-13, 32 launches).  So here, unlike
        // on the React Native host, `--sdkLaunchCall=scene` is the third call, after +load's and the plugin's load().
        NotificationCenter.default.addObserver(forName: UIScene.willConnectNotification, object: nil, queue: nil) { _ in
            NSLog("%@", "LAB point=sceneWillConnect scenes=\(UIApplication.shared.connectedScenes.count) "
                  + "state=\(LabState.name(UIApplication.shared.applicationState))")
            if LabLaunchCall.value == "scene" {
                NSLog("%@", "LAB point=sdkLaunchCall at=scene")
                TSBackgroundFetch.sharedInstance().didFinishLaunching()
            }
        }
        NSLog("%@", "LAB point=didFinishLaunchingReturn")
        return true
    }

    /// (4) The legacy background fetch, as UIKit delivers it on the Simulator: BGTaskScheduler is unavailable there, the
    /// SDK falls back to setMinimumBackgroundFetchInterval, and `xcrun simctl push <udid> <bundle>` with
    /// {"aps":{"content-available":1}} has SpringBoard send this app a background fetch in any state (WO-002 D3).
    /// Forwarded to the SDK as the plugin's install docs say (help/INSTALL-IOS.md): nothing in Capacitor or the plugin
    /// forwards it.  The completion is logged with UIKit's result.
    func application(_ application: UIApplication,
                     performFetchWithCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void) {
        let state = LabState.name(application.applicationState)
        NSLog("%@", "LAB point=legacyFetch state=\(state) remaining=\(LabState.remaining(application))")
        TSBackgroundFetch.sharedInstance().perform(completionHandler: { result in
            NSLog("%@", "LAB point=legacyFetchComplete result=\(LabState.name(result)) state=\(state)")
            completionHandler(result)
        }, applicationState: application.applicationState)
    }

    func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        return ApplicationDelegateProxy.shared.application(app, open: url, options: options)
    }

    func application(_ application: UIApplication, continue userActivity: NSUserActivity,
                     restorationHandler: @escaping ([UIUserActivityRestoring]?) -> Void) -> Bool {
        return ApplicationDelegateProxy.shared.application(application, continue: userActivity,
                                                           restorationHandler: restorationHandler)
    }

    /// The template's, kept: the scene gets the template's SceneDelegate and no storyboard, so the one bridge is the
    /// one SceneDelegate builds.  Without it UIKit reads Info.plist's scene manifest (UISceneStoryboardFile Main), whose
    /// initial controller is a second CAPBridgeViewController (bg-forge WO-187; LabPlugin's `n` must stay 1).
    func application(_ application: UIApplication,
                     configurationForConnecting connectingSceneSession: UISceneSession,
                     options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        let config = UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
        config.delegateClass = SceneDelegate.self
        return config
    }
}

/// (ios-registration) Where this launch makes the SDK's didFinishLaunching() call, from `--sdkLaunchCall=<value>`:
/// `delegate` (the default, as the install docs say), `scene` (when the scene connects) or `none`.  On this host the
/// plugin's load() makes the call from the scene on EVERY launch, so `none` no longer isolates the SDK's +load path.
/// A launch the OS makes (a background fetch into a terminated app) has no argv, so it reads `delegate`.
enum LabLaunchCall {
    static let values = ["delegate", "scene", "none"]
    static let value: String = ProcessInfo.processInfo.arguments.first { $0.hasPrefix("--sdkLaunchCall=") }
        .map { String($0.dropFirst("--sdkLaunchCall=".count)) } ?? "delegate"
}

/// The LAB lines' words, UIKit's (the React Native host's LabState).
enum LabState {
    static func name(_ s: UIApplication.State) -> String {
        switch s {
        case .active: return "active"
        case .inactive: return "inactive"
        case .background: return "background"
        @unknown default: return "unknown"
        }
    }

    static func name(_ r: UIBackgroundFetchResult) -> String {
        switch r {
        case .newData: return "newData"
        case .noData: return "noData"
        case .failed: return "failed"
        @unknown default: return "unknown"
        }
    }

    /// backgroundTimeRemaining is DBL_MAX in the foreground: "-" then, else whole seconds.
    static func remaining(_ app: UIApplication) -> String {
        let r = app.backgroundTimeRemaining
        return r > 1e6 ? "-" : String(format: "%.1f", r)
    }
}

/// iOS half of the lab's `Lab` plugin.  The Android half is LabPlugin.java, which reads the launch Intent's extras;
/// here the scenario args arrive as process argv (`xcrun simctl launch <dev> <bundle> --key=value …`).  Same plugin
/// name, same keys and types, so www/index.html is one file for both OSes.  Registered through the packageClassList
/// build.sh writes into App/capacitor.config.json, since `cap sync` is never run.
@objc(LabPlugin)
public class LabPlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "LabPlugin"
    public let jsName = "Lab"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "args", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "knobs", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "probe", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "verdict", returnType: CAPPluginReturnPromise),
    ]

    /// MainActivity.DEFAULT_FINISH_DELAY_MS on Android: how long after a fetch the driver calls finish(taskId).
    static let defaultFinishDelayMs = 1000

    /// The Android counter lives on MainActivity; the WebView's JavaScript is what boots here.
    private static var jsBoots = 0

    /// Bridges in this process: every plugin loads once per CAPBridgeViewController, and the template names two —
    /// Main.storyboard's initial controller and the one its SceneDelegate creates (bg-forge WO-187).  `n` must stay 1.
    private static var loads = 0

    public override func load() {
        LabPlugin.loads += 1
        NSLog("%@", "LAB point=pluginLoad n=\(LabPlugin.loads) scenes=\(UIApplication.shared.connectedScenes.count) "
              + "state=\(LabState.name(UIApplication.shared.applicationState))")
        LabPlugin.observeFires()
    }

    /// `--key=value` pairs from argv; a bare `--key` reads "true".
    private static let argv: [String: String] = {
        var out: [String: String] = [:]
        for a in ProcessInfo.processInfo.arguments where a.hasPrefix("--") {
            let body = String(a.dropFirst(2))
            guard let eq = body.firstIndex(of: "=") else { out[body] = "true"; continue }
            out[String(body[body.startIndex..<eq])] = String(body[body.index(after: eq)...])
        }
        return out
    }()

    private static func flag(_ key: String, _ fallback: Bool) -> Bool {
        guard let v = argv[key] else { return fallback }
        return v == "true" || v == "1"
    }

    private static func int(_ key: String, _ fallback: Int) -> Int {
        guard let v = argv[key], let n = Int(v) else { return fallback }
        return n
    }

    private static func decode(_ key: String, _ fallback: String) -> String {
        guard let v = argv[key], let d = Data(base64Encoded: v),
              let s = String(data: d, encoding: .utf8) else { return fallback }
        return s
    }

    /// The finish knobs.  Android persists them for its headless task; iOS has no headless mode, so they are the
    /// launch's own (an OS launch has no argv: the defaults).
    private static var knobValues: [String: Any] {
        return ["finishDelayMs": int("finishDelayMs", defaultFinishDelayMs),
                "withholdFinish": flag("withholdFinish", false),
                "finishOnTimeout": flag("finishOnTimeout", true)]
    }

    /// The launch's args: only what the driver honours, as on Android (the boot line echoes this map).
    @objc func args(_ call: CAPPluginCall) {
        LabPlugin.jsBoots += 1
        var out = LabPlugin.knobValues
        out.merge([
            "stopOnTerminate": LabPlugin.flag("stopOnTerminate", true),
            "noConfigure": LabPlugin.flag("noConfigure", false),
            "scenario": LabPlugin.argv["scenario"] ?? "",
            "nativeLabel": LabPlugin.argv["nativeLabel"] ?? "",
            "config": LabPlugin.decode("configB64", "{}"),
            "hooks": LabPlugin.decode("hooksB64", "[]"),
            "events": LabPlugin.decode("eventsB64", "[]"),
            "view": LabPlugin.argv["view"] ?? "",
            "boot": LabPlugin.jsBoots,
        ]) { _, new in new }
        call.resolve(out)
    }

    @objc func knobs(_ call: CAPPluginCall) {
        call.resolve(LabPlugin.knobValues)
    }

    /// The SDK's build and state, read through the Objective-C runtime and its public API, never by building anything
    /// (the React Native host's LabModule.m probe, field for field).  `subscribers` are the fetch listeners
    /// (TSBGAppRefreshSubscriber), `tasks` the scheduled TSBGTasks, `pendingFetch` whether a legacy fetch's completion
    /// handler is held, `bgTask` whether its UIBackgroundTask (the legacy timeout) is open.
    @objc func probe(_ call: CAPPluginCall) {
        let f = LabPlugin.sdk("TSBackgroundFetch", "sharedInstance")
        let subs = ((LabPlugin.sdk("TSBGAppRefreshSubscriber", "subscribers") as? NSDictionary)?.allKeys as? [String] ?? [])
            .sorted()
        let tasks = ((LabPlugin.sdk("TSBGTask", "tasks") as? NSArray) ?? [])
            .map { (LabPlugin.kvc($0 as AnyObject, "identifier") as? String) ?? "?" }.sorted()
        let bg = (LabPlugin.kvc(f, "backgroundTask") as? NSNumber)?.uintValue ?? UInt(UIBackgroundTaskIdentifier.invalid.rawValue)
        let line = "headless=- sdkBuild=\(LabPlugin.sdkBuild) configured=\(LabPlugin.bool(f, "configured")) "
            + "active=\(LabPlugin.bool(f, "active")) stopOnTerminate=\(LabPlugin.bool(f, "stopOnTerminate")) "
            + "subscribers=\(subs.isEmpty ? "-" : subs.joined(separator: ",")) "
            + "tasks=\(tasks.isEmpty ? "-" : tasks.joined(separator: ",")) "
            + "pendingFetch=\(LabPlugin.kvc(f, "completionHandler") != nil) "
            + "bgTask=\(bg != UInt(UIBackgroundTaskIdentifier.invalid.rawValue))"
        call.resolve(["probe": line])
    }

    /// The host's verdict for the device view: run.py writes it into this app's data container
    /// (backends.Simctl.push_verdict), or nothing yet.  Read here, never judged.
    @objc func verdict(_ call: CAPPluginCall) {
        let path = NSHomeDirectory() + "/Library/lab-verdict.json"
        call.resolve(["value": (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""])
    }

    // --- the SDK, by name ------------------------------------------------------------------------------------------

    /// The SDK's apple-generic version string (`PROJECT:TSBackgroundFetch-<build>`), which no SDK code references:
    /// build.sh links it into the app with `-Wl,-u,_TSBackgroundFetchVersionString` (a Swift file cannot name a C
    /// array), so the harness's native_resolved and build.sh's proof find it; read here by name.
    static let sdkBuild: String = {
        guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "TSBackgroundFetchVersionString") else { return "?" }
        let v = String(cString: sym.assumingMemoryBound(to: CChar.self))
        guard let r = v.range(of: "PROJECT:TSBackgroundFetch-") else { return "?" }
        return String(v[r.upperBound...].prefix { $0.isNumber })
    }()

    /// A class method of the SDK by name, e.g. +[TSBackgroundFetch sharedInstance]: nil when it is absent.
    private static func sdk(_ className: String, _ selector: String) -> AnyObject? {
        guard let cls = NSClassFromString(className) as AnyObject?, cls.responds(to: Selector(selector)) else { return nil }
        return cls.perform(Selector(selector))?.takeUnretainedValue()
    }

    /// Key-value read that never raises (Swift cannot catch NSUnknownKeyException): only a key the object answers, as
    /// a getter or as KVC's `_key` / `key` instance variable.
    private static func kvc(_ obj: AnyObject?, _ key: String) -> Any? {
        guard let o = obj as? NSObject else { return nil }
        let cls: AnyClass = type(of: o)
        guard o.responds(to: Selector(key)) || class_getInstanceVariable(cls, "_" + key) != nil
                || class_getInstanceVariable(cls, key) != nil else { return nil }
        return o.value(forKey: key)
    }

    private static func bool(_ obj: AnyObject?, _ key: String) -> String {
        guard let n = kvc(obj, key) as? NSNumber else { return "?" }
        return n.boolValue ? "true" : "false"
    }

    // --- (ios-task-sim) the custom-task hook -----------------------------------------------------------------------

    /// BGTaskScheduler is unavailable on the Simulator, so nothing launches a TSBGTask there; but scheduleTask has put
    /// the task, with the plugin's callback, in TSBGTask's list before its submit fails.  The host's `fire: task` /
    /// `fire: task-timeout` posts a Darwin notification, `<bundle id>/<kind>/<taskId>` (`simctl spawn <udid> notifyutil
    /// -p`), and this calls that task's `execute` or `onTimeout`, as BGTaskScheduler's launch handler and the task's
    /// expiration handler do on a device (the React Native host's LabModule.m LabFire).
    private static var fireArmed = false

    private static func observeFires() {
        guard !fireArmed else { return }
        fireArmed = true
        let bundle = Bundle.main.bundleIdentifier ?? ""
        let ids = (Bundle.main.object(forInfoDictionaryKey: "BGTaskSchedulerPermittedIdentifiers") as? [String] ?? [])
            .filter { $0 != "com.transistorsoft.fetch" }   // the fetch: `fire: fetch`, simctl push
        for taskId in ids {
            for kind in ["task", "task-timeout"] {
                CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, name, _, _ in
                    guard let name = name?.rawValue as String? else { return }
                    LabPlugin.fire(name)
                }, "\(bundle)/\(kind)/\(taskId)" as CFString, nil, .deliverImmediately)
            }
        }
        NSLog("%@", "LAB point=fireArmed taskIds=\(ids.isEmpty ? "-" : ids.joined(separator: ","))")
    }

    private static func fire(_ name: String) {
        let rest = name.dropFirst((Bundle.main.bundleIdentifier ?? "").count + 1)
        guard let slash = rest.firstIndex(of: "/") else { return }
        let kind = String(rest[rest.startIndex..<slash]), taskId = String(rest[rest.index(after: slash)...])
        var task: AnyObject? = nil
        if let cls = NSClassFromString("TSBGTask") as AnyObject?, cls.responds(to: Selector(("get:"))) {
            task = cls.perform(Selector(("get:")), with: taskId)?.takeUnretainedValue()
        }
        let sel = Selector(kind == "task-timeout" ? "onTimeout" : "execute")
        var ran = false
        if let t = task as? NSObject, t.responds(to: sel), let imp = t.method(for: sel) {
            // NO from either: the task has no callback (loaded from the defaults, never scheduled in this process).
            typealias Run = @convention(c) (AnyObject, Selector) -> Bool
            ran = unsafeBitCast(imp, to: Run.self)(t, sel)
        }
        NSLog("%@", "LAB point=fire kind=\(kind) taskId=\(taskId) found=\(task != nil) ran=\(ran)")
    }
}

/// The JavaScript console's sink (bg-forge's LabConsolePlugin).  On iOS Capacitor's native bridge sends every console
/// call to its `Console` plugin (native-bridge.js: `cap.toNative('Console', 'log', {level, message})`), which prints it
/// with Swift `print` — stdout, which reaches neither the harness's `log stream` tap nor `simctl launch --stdout`, so
/// the driver's `LAB app=…` lines would be lost.  This plugin takes the `Console` name after the core one
/// (packageClassList entries register after Capacitor's own plugins, keyed by jsName, the last one wins) and NSLogs the
/// message verbatim.  It returns nothing (CAPPluginReturnNone), so it never resolves a call back into the WebView: a
/// log line costs the page no extra native-to-JavaScript round trip.
@objc(LabConsolePlugin)
public class LabConsolePlugin: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "LabConsolePlugin"
    public let jsName = "Console"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "log", returnType: CAPPluginReturnNone),
    ]

    @objc func log(_ call: CAPPluginCall) {
        NSLog("%@", call.getString("message") ?? "")
    }
}
