import Flutter
import UIKit

/// iOS host of the Flutter fetch lab (WO-002): Flutter 3.47.5's template AppDelegate (a UIScene app whose implicit
/// engine starts when the scene connects, after didFinishLaunching has returned), plus the lab's native side.  Ported
/// from bg-forge's labs/platforms/flutter/app/native-ios/AppDelegate.swift (addc50501) with the fetch Capacitor host's
/// LabPlugin (labs/platforms/capacitor/app/native-ios/AppDelegate.swift).  build.sh copies it over each generated
/// host's ios/Runner/AppDelegate.swift (app/build/ios-pods, app/build/ios-spm: gitignored, under a directory run.py's
/// source stamp skips); this file is the one tracked iOS source, and the stamp hashes it.
///
/// The SDK is reached by name (the Objective-C runtime, dlsym), never imported: the overlay compiles the same on both
/// channels, and nothing here links the SDK but the plugin.  The template's SceneDelegate (FlutterSceneDelegate) is
/// used as generated.
@main
@objc class AppDelegate: FlutterAppDelegate, FlutterImplicitEngineDelegate {

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    // (1) The launch, with which scenario keys argv carried (LabProbe reads their values).  NSLog, not print: os_log is
    // the channel the harness taps (`log stream`), and the Flutter engine routes Dart's print there too.  The format is
    // a literal so an argv value is never one.  fetch_pack reads this line first in each process.
    let keys = ProcessInfo.processInfo.arguments.filter { $0.hasPrefix("--") }
      .map { String($0.dropFirst(2).prefix { $0 != "=" }) }.sorted()
    let call = LabLaunchCall.value
    NSLog("%@", "LAB point=didFinishLaunching argv=\(keys) state=\(LabState.name(application.applicationState)) "
          + "sdkLaunchCall=\(call)")
    guard LabLaunchCall.values.contains(call) else {
      fatalError("LAB sdkLaunchCall=\(call) is not one of \(LabLaunchCall.values)")
    }

    // (2) The lab's default launch call, here as React Native's install docs put it.  Flutter's ask for none
    // (help/INSTALL-IOS.md; CHANGELOG 0.5.1 has customers remove theirs): its documented setup is `--sdkLaunchCall=none`.
    // The SDK's +load also registers on UIApplicationDidFinishLaunchingNotification, and the plugin's own
    // application:didFinishLaunchingWithOptions: (BackgroundFetchPlugin.m:38-42) makes the call again when the engine
    // replays the launch to its plugins at the scene; the SDK's `launched` flag makes them one call.
    if call == "delegate" {
      NSLog("%@", "LAB point=sdkLaunchCall at=delegate")
      LabSDK.didFinishLaunching()
    }

    // (3) The scene's connection, logged without touching the template's SceneDelegate.
    NotificationCenter.default.addObserver(forName: UIScene.willConnectNotification, object: nil, queue: nil) { _ in
      NSLog("%@", "LAB point=sceneWillConnect scenes=\(UIApplication.shared.connectedScenes.count) "
            + "state=\(LabState.name(UIApplication.shared.applicationState))")
      if LabLaunchCall.value == "scene" {
        NSLog("%@", "LAB point=sdkLaunchCall at=scene")
        LabSDK.didFinishLaunching()
      }
    }
    let ok = super.application(application, didFinishLaunchingWithOptions: launchOptions)
    NSLog("%@", "LAB point=didFinishLaunchingReturn")
    return ok
  }

  /// (4) The engine, created when the scene connects (the template's storyboard FlutterViewController): the plugins
  /// register here, background_fetch among them, and so does the lab's channel, on the application registrar's
  /// messenger (Flutter's UIScene migration guide for an app-level channel; bg-forge WO-187).  `n` counts engines in
  /// this process: it must stay 1, as Capacitor's `pluginLoad n=` must.
  func didInitializeImplicitFlutterEngine(_ engineBridge: FlutterImplicitEngineBridge) {
    GeneratedPluginRegistrant.register(with: engineBridge.pluginRegistry)
    LabProbe.engines += 1
    LabProbe.register(engineBridge.applicationRegistrar.messenger())
    NSLog("%@", "LAB point=pluginLoad n=\(LabProbe.engines) scenes=\(UIApplication.shared.connectedScenes.count) "
          + "state=\(LabState.name(UIApplication.shared.applicationState))")
    LabProbe.observeFires()
  }

  /// (5) The legacy background fetch, as UIKit delivers it on the Simulator: BGTaskScheduler is unavailable there, the
  /// SDK falls back to setMinimumBackgroundFetchInterval, and `xcrun simctl push` with {"aps":{"content-available":1}}
  /// has SpringBoard send the app a background fetch in any state (WO-002 D3).
  ///
  /// Unlike the React Native and Capacitor hosts, this host does NOT hand the fetch to the SDK: on Flutter the PLUGIN
  /// does (BackgroundFetchPlugin.m:29-34, an application delegate the registrar added), through FlutterAppDelegate's
  /// lifecycle forwarding, and that is the path a customer's app takes.  This override only wraps it: UIKit sends the
  /// selector here because FlutterAppDelegate answers respondsToSelector: for it when a registered plugin implements it
  /// (FlutterAppDelegate.mm:284-297); `super.forwardingTarget(for:)` is the lifecycle delegate it would have forwarded
  /// to (:299-305), which offers the fetch to each plugin in turn and says whether one took it.  None did: the lab
  /// leaves UIKit's handler uncalled, as a customer's app does (FlutterPluginAppLifeCycleDelegate.mm:646-658 returns NO
  /// and completes nothing), so SpringBoard records the failure — never a noData the host made up, which would pass
  /// ios-stop-start `stopped` for a plugin that dropped the fetch (the review of 2026-11-14).  An `override` to Swift
  /// (FlutterAppDelegate adopts UIApplicationDelegate), though FlutterAppDelegate implements no such method: it adds the
  /// selector dynamically, so nothing here calls super's.
  override func application(_ application: UIApplication,
                            performFetchWithCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void) {
    let state = LabState.name(application.applicationState)
    NSLog("%@", "LAB point=legacyFetch state=\(state) remaining=\(LabState.remaining(application))")
    let wrapped: (UIBackgroundFetchResult) -> Void = { result in
      NSLog("%@", "LAB point=legacyFetchComplete result=\(LabState.name(result)) state=\(state)")
      completionHandler(result)
    }
    let sel = #selector(UIApplicationDelegate.application(_:performFetchWithCompletionHandler:))
    let lifecycle = super.forwardingTarget(for: sel) as? FlutterPluginAppLifeCycleDelegate
    let taken = lifecycle?.application(application, performFetchWithCompletionHandler: wrapped) ?? false
    NSLog("%@", "LAB point=legacyFetchForward plugins=\(taken) lifecycle=\(lifecycle != nil)")
  }
}

/// (ios-registration) Where this launch makes the SDK's didFinishLaunching() call, from `--sdkLaunchCall=<value>`:
/// `delegate` (the lab's default, React Native's documented call; Flutter's docs ask for none), `scene` (when the scene
/// connects) or `none`.  On this host the plugin's own application:didFinishLaunchingWithOptions: makes the call when
/// the engine replays the launch at the scene, on EVERY launch, so `none` no longer isolates the SDK's +load path.  A
/// launch the OS makes (a background fetch into a terminated app) has no argv, so it reads `delegate`.
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

/// The SDK by name: a class method of TSBackgroundFetch and friends, nil when absent.  Never builds anything the SDK
/// would not (the React Native host's LabModule.m, the Capacitor host's LabPlugin.sdk).
enum LabSDK {
  static func call(_ className: String, _ selector: String) -> AnyObject? {
    guard let cls = NSClassFromString(className) as AnyObject?, cls.responds(to: Selector(selector)) else { return nil }
    return cls.perform(Selector(selector))?.takeUnretainedValue()
  }

  /// [[TSBackgroundFetch sharedInstance] didFinishLaunching].
  static func didFinishLaunching() {
    guard let f = call("TSBackgroundFetch", "sharedInstance") as? NSObject,
          f.responds(to: Selector(("didFinishLaunching"))) else {
      NSLog("%@", "LAB point=sdkLaunchCall outcome=absent")
      return
    }
    f.perform(Selector(("didFinishLaunching")))
  }
}

/// iOS half of the lab's `lab/probe` channel.  The Android half is the lab_probe plugin (LabProbePlugin.kt), which
/// reads the launch Intent's extras; here the scenario args arrive as process argv (`xcrun simctl launch <dev> <bundle>
/// --key=value …`).  Same channel, same keys, same TYPES — the driver tests `== true` and `as num` — so app/lib/main.dart
/// is one file for both OSes.  A channel on the engine, not a plugin: iOS has no headless engine to reach.
enum LabProbe {
  /// MainActivity's DEFAULT_FINISH_DELAY_MS on Android: how long after a fetch the driver calls finish(taskId).
  static let defaultFinishDelayMs = 1000

  /// Engines that registered this channel in the process (`pluginLoad n=`), and Dart boots (`args` calls).
  static var engines = 0
  private static var boots = 0

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

  static func register(_ messenger: FlutterBinaryMessenger) {
    let channel = FlutterMethodChannel(name: "lab/probe", binaryMessenger: messenger)
    channel.setMethodCallHandler { call, result in
      switch call.method {
      case "args":
        // Only what the driver honours, as on Android (the boot line echoes this map).  restartOnFinish is Android's
        // (there is no finish trigger on the Simulator), read so the key set is the same.
        boots += 1
        var out = knobValues
        out.merge([
          "stopOnTerminate": flag("stopOnTerminate", true),
          "noConfigure": flag("noConfigure", false),
          "restartOnFinish": flag("restartOnFinish", false),
          "scenario": argv["scenario"] ?? "",
          "nativeLabel": argv["nativeLabel"] ?? "",
          "config": decode("configB64", "{}"),
          "hooks": decode("hooksB64", "[]"),
          "events": decode("eventsB64", "[]"),
          "view": argv["view"] ?? "",
          "boot": boots,
        ]) { _, new in new }
        result(out)
      case "knobs":
        result(knobValues)
      case "probe":
        result(probe())
      case "verdict":
        // The host's verdict for the device view: run.py writes it into this app's data container
        // (backends.Simctl.push_verdict), or nothing yet.  Read here, never judged.
        result((try? String(contentsOfFile: NSHomeDirectory() + "/Library/lab-verdict.json", encoding: .utf8)) ?? "")
      default:
        result(FlutterMethodNotImplemented)
      }
    }
  }

  /// The SDK's build and state, read through the Objective-C runtime, never by building anything (the React Native
  /// host's LabModule.m probe, field for field).  `subscribers` are the fetch listeners (TSBGAppRefreshSubscriber),
  /// `tasks` the scheduled TSBGTasks, `pendingFetch` whether a legacy fetch's completion handler is held, `bgTask`
  /// whether its UIBackgroundTask (the legacy timeout) is open.
  static func probe() -> String {
    let f = LabSDK.call("TSBackgroundFetch", "sharedInstance")
    let subs = ((LabSDK.call("TSBGAppRefreshSubscriber", "subscribers") as? NSDictionary)?.allKeys as? [String] ?? [])
      .sorted()
    let tasks = ((LabSDK.call("TSBGTask", "tasks") as? NSArray) ?? [])
      .map { (kvc($0 as AnyObject, "identifier") as? String) ?? "?" }.sorted()
    let invalid = UInt(UIBackgroundTaskIdentifier.invalid.rawValue)
    let bg = (kvc(f, "backgroundTask") as? NSNumber)?.uintValue ?? invalid
    return "headless=- sdkBuild=\(sdkBuild) configured=\(bool(f, "configured")) "
      + "active=\(bool(f, "active")) stopOnTerminate=\(bool(f, "stopOnTerminate")) "
      + "subscribers=\(subs.isEmpty ? "-" : subs.joined(separator: ",")) "
      + "tasks=\(tasks.isEmpty ? "-" : tasks.joined(separator: ",")) "
      + "pendingFetch=\(kvc(f, "completionHandler") != nil) "
      + "bgTask=\(bg != invalid)"
  }

  /// The SDK's apple-generic version string (`PROJECT:TSBackgroundFetch-<build>`), which no SDK code references:
  /// build.sh links it into the app with `-Wl,-u,_TSBackgroundFetchVersionString` (a Swift file cannot name a C
  /// array), so the harness's native_resolved and build.sh's proof find it; read here by name.
  static let sdkBuild: String = {
    guard let sym = dlsym(UnsafeMutableRawPointer(bitPattern: -2), "TSBackgroundFetchVersionString") else { return "?" }
    let v = String(cString: sym.assumingMemoryBound(to: CChar.self))
    guard let r = v.range(of: "PROJECT:TSBackgroundFetch-") else { return "?" }
    return String(v[r.upperBound...].prefix { $0.isNumber })
  }()

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

  // --- (ios-task-sim) the custom-task hook -------------------------------------------------------------------------

  /// BGTaskScheduler is unavailable on the Simulator, so nothing launches a TSBGTask there; but scheduleTask has put the
  /// task, with the plugin's callback, in TSBGTask's list before its submit fails.  The host's `fire: task` /
  /// `fire: task-timeout` posts a Darwin notification, `<bundle id>/<kind>/<taskId>` (`simctl spawn <udid> notifyutil
  /// -p`), and this calls that task's `execute` or `onTimeout`, as BGTaskScheduler's launch handler and the task's
  /// expiration handler do on a device (the React Native host's LabModule.m LabFire, the Capacitor host's LabPlugin).
  private static var fireArmed = false

  static func observeFires() {
    guard !fireArmed else { return }
    fireArmed = true
    let bundle = Bundle.main.bundleIdentifier ?? ""
    let ids = (Bundle.main.object(forInfoDictionaryKey: "BGTaskSchedulerPermittedIdentifiers") as? [String] ?? [])
      .filter { $0 != "com.transistorsoft.fetch" }   // the fetch: `fire: fetch`, simctl push
    for taskId in ids {
      for kind in ["task", "task-timeout"] {
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), nil, { _, _, name, _, _ in
          guard let name = name?.rawValue as String? else { return }
          LabProbe.fire(name)
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
