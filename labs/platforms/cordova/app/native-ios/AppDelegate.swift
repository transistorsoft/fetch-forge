import UIKit

/// iOS host of the Cordova fetch lab (WO-002): cordova-ios 8.1.1's template AppDelegate.swift — an `@objc
/// @implementation` of App/AppDelegate.h's `AppDelegate : CDVAppDelegate`, a UIScene app whose SceneDelegate
/// (CDVSceneDelegate) and storyboard build the CDVViewController when the scene connects — plus the launch lines the
/// harness reads and a wrapper around the legacy background fetch.  build.sh copies it over the generated
/// platforms/ios/App/AppDelegate.swift (app/build/ios-spm: gitignored, under a directory run.py's source stamp skips);
/// this file is tracked and hashed.  The lab plugin's iOS half (app/lab-plugin/src/apple/LabPlugin.m) is the driver's
/// channel; this file only frames the launch.
///
/// The SDK is reached by name (the Objective-C runtime), never imported: it comes into the app through the plugin's
/// Swift package, not through this target's sources.
@main
#if compiler(>=6.1)
@objc @implementation
#else
@_objcImplementation
#endif
extension AppDelegate {
    open override func application(_ application: UIApplication,
                                   didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // (1) The launch, with which scenario keys argv carried (LabPlugin reads their values).  NSLog, not print:
        // os_log is the channel the harness taps (`log stream`).  The format is a literal so an argv value is never one.
        // fetch_pack reads this line first in each process.
        let keys = ProcessInfo.processInfo.arguments.filter { $0.hasPrefix("--") }
            .map { String($0.dropFirst(2).prefix { $0 != "=" }) }.sorted()
        let call = LabLaunchCall.value
        NSLog("%@", "LAB point=didFinishLaunching argv=\(keys) state=\(LabState.name(application.applicationState)) "
              + "sdkLaunchCall=\(call)")
        guard LabLaunchCall.values.contains(call) else {
            fatalError("LAB sdkLaunchCall=\(call) is not one of \(LabLaunchCall.values)")
        }

        // (2) The lab's default launch call, here as React Native's install docs put it; the Cordova plugin's docs ask
        // for none.  The SDK's +load registers on UIApplicationDidFinishLaunchingNotification anyway, and its
        // `launched` flag makes every call one registration.  (ios-registration) Unless the launch moves the call.
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

    /// The template's.
    open override func application(_ application: UIApplication, configurationForConnecting connectingSceneSession: UISceneSession,
                                   options: UIScene.ConnectionOptions) -> UISceneConfiguration {
        return UISceneConfiguration(name: "Default Configuration", sessionRole: connectingSceneSession.role)
    }

    /// (4) The legacy background fetch, as UIKit delivers it on the Simulator: BGTaskScheduler is unavailable there, the
    /// SDK falls back to setMinimumBackgroundFetchInterval, and `xcrun simctl push` with {"aps":{"content-available":1}}
    /// has SpringBoard send the app a background fetch in any state (WO-002 D3).
    ///
    /// The host does NOT hand the fetch to the SDK: the PLUGIN does, through its category on CDVAppDelegate
    /// (CDVBackgroundFetch.m, `CDVAppDelegate(CDVBackgroundFetch)`), the path a customer's app takes.  This override only
    /// wraps it, and `super` IS that category's method: AppDelegate's superclass is CDVAppDelegate.  Without the plugin
    /// the call would raise (unrecognized selector), so build.sh proves the category is in the app.
    open override func application(_ application: UIApplication,
                                   performFetchWithCompletionHandler completionHandler: @escaping (UIBackgroundFetchResult) -> Void) {
        let state = LabState.name(application.applicationState)
        NSLog("%@", "LAB point=legacyFetch state=\(state) remaining=\(LabState.remaining(application))")
        super.application(application, performFetchWithCompletionHandler: { result in
            NSLog("%@", "LAB point=legacyFetchComplete result=\(LabState.name(result)) state=\(state)")
            completionHandler(result)
        })
        NSLog("%@", "LAB point=legacyFetchForward category=CDVAppDelegate(CDVBackgroundFetch)")
    }
}

/// (ios-registration) Where this launch makes the SDK's didFinishLaunching() call, from `--sdkLaunchCall=<value>`:
/// `delegate` (the lab's default, React Native's documented call; the Cordova plugin's docs ask for none), `scene` (when
/// the scene connects) or `none`.  A launch the OS makes (a background fetch into a terminated app) has no argv, so it
/// reads `delegate`.
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

/// The SDK by name: [[TSBackgroundFetch sharedInstance] didFinishLaunching], never building anything the SDK would not.
enum LabSDK {
    static func didFinishLaunching() {
        guard let cls = NSClassFromString("TSBackgroundFetch") as AnyObject?,
              cls.responds(to: Selector(("sharedInstance"))),
              let f = cls.perform(Selector(("sharedInstance")))?.takeUnretainedValue() as? NSObject,
              f.responds(to: Selector(("didFinishLaunching"))) else {
            NSLog("%@", "LAB point=sdkLaunchCall outcome=absent")
            return
        }
        f.perform(Selector(("didFinishLaunching")))
    }
}
