import UIKit
import TSBackgroundFetch

/// iOS host of the React Native fetch lab (WO-002 phase 2): React Native 0.88.0's template AppDelegate, changed in
/// three places (and, for ios-registration, the SDK's launch call made where the launch says).  React Native itself
/// starts in SceneDelegate.swift, after this has returned (a UIScene app).
@main
class AppDelegate: UIResponder, UIApplicationDelegate {
  func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
  ) -> Bool {
    // (1) The launch, with which scenario keys argv carried (LabModule.m reads their values).  NSLog: os_log is
    // the channel the harness taps (`log stream`).  The format is a literal so an argv value is never one.
    let keys = ProcessInfo.processInfo.arguments.filter { $0.hasPrefix("--") }
      .map { String($0.dropFirst(2).prefix { $0 != "=" }) }.sorted()
    let call = LabLaunchCall.value
    NSLog("%@", "LAB point=didFinishLaunching argv=\(keys) state=\(LabState.name(application.applicationState)) " +
          "sdkLaunchCall=\(call)")
    guard LabLaunchCall.values.contains(call) else {
      fatalError("LAB sdkLaunchCall=\(call) is not one of \(LabLaunchCall.values)")
    }

    // (2) [REQUIRED] by the plugin's install docs (docs/INSTALL-AUTO-IOS.md, "AppDelegate").  The SDK's +load
    // also registers on UIApplicationDidFinishLaunchingNotification; its `launched` flag makes the two one call.
    // (ios-registration) Unless the launch moves the call (LabLaunchCall).
    if call == "delegate" {
      NSLog("%@", "LAB point=sdkLaunchCall at=delegate")
      TSBackgroundFetch.sharedInstance().didFinishLaunching()
    }
    NSLog("%@", "LAB point=didFinishLaunchingReturn")
    return true
  }

  /// (3) (WO-002 D3) The legacy background fetch, as UIKit delivers it on the Simulator: there BGTaskScheduler is
  /// unavailable, the SDK falls back to setMinimumBackgroundFetchInterval (TSBackgroundFetch.m:117-122), and
  /// `xcrun simctl push <udid> <bundle>` with {"aps":{"content-available":1}} has SpringBoard send this app a
  /// background fetch in any state (MEASURED 2026-11-10, WO-002 Log).  Handed to the SDK as the plugin's
  /// RNBackgroundFetch+AppDelegate.m did; its podspec and Package.swift no longer compile that file, so in a
  /// React Native 0.88 app nothing else forwards it.  The completion is logged with UIKit's result.
  func application(
    _ application: UIApplication,
    performFetchWithCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void
  ) {
    let state = LabState.name(application.applicationState)
    NSLog("%@", "LAB point=legacyFetch state=\(state) remaining=\(LabState.remaining(application))")
    TSBackgroundFetch.sharedInstance().perform(completionHandler: { result in
      NSLog("%@", "LAB point=legacyFetchComplete result=\(LabState.name(result)) state=\(state)")
      completionHandler(result)
    }, applicationState: application.applicationState)
  }
}

/// (ios-registration) Where this launch makes the SDK's didFinishLaunching() call, from the launch's
/// `--sdkLaunchCall=<value>` (a step's `extras: {sdkLaunchCall: …}`):
///   * `delegate` (the default): in application(_:didFinishLaunchingWithOptions:), as the install docs say;
///   * `scene`: in SceneDelegate's scene(_:willConnectTo:options:), after the launch has finished, as a plugin that
///     makes the call from its own start-up (Cordova's pluginInitialize, Capacitor's load()) would where that
///     start-up comes with the scene (NOT CHECKED in those platforms' hosts);
///   * `none`: never, so the SDK's +load path (TSBackgroundFetch.m:28-32, :59) alone registers.
/// A launch the OS makes (a background fetch into a terminated app) has no argv, so it reads `delegate`.
enum LabLaunchCall {
  static let values = ["delegate", "scene", "none"]
  static let value: String = ProcessInfo.processInfo.arguments.first { $0.hasPrefix("--sdkLaunchCall=") }
    .map { String($0.dropFirst("--sdkLaunchCall=".count)) } ?? "delegate"
}

/// The LAB lines' words, UIKit's.  SpringBoard's `SBBackgroundFetchTask … with result:` line words the same
/// results its own way (RetrievedNewData, NoNewDataAvailable, Error).
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
