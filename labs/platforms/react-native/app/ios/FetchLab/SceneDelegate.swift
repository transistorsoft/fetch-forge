import React
import React_RCTAppDelegate
import ReactAppDependencyProvider
import TSBackgroundFetch
import UIKit

/// React Native 0.88.0's template SceneDelegate (WO-002 phase 2), changed in three places, as bg-forge's React
/// Native lab host is (WO-187 there): a `LAB point=sceneWillConnect` line, no dev-loading view, and the bundle URL;
/// and in a fourth for ios-registration: the SDK's launch call, where a launch moves it here (LabLaunchCall).
/// The JavaScript ALWAYS comes from the embedded main.jsbundle that build.sh proves is `--dev false`, the iOS
/// analogue of the Android lab's useDevSupport = false: the template's `#if DEBUG` branch asks
/// RCTBundleURLProvider, which prefers any packager answering on localhost:8081, so a Metro left running would
/// supply the JavaScript under test.
class SceneDelegate: RCTDefaultReactNativeFactoryDelegate, UIWindowSceneDelegate {
  var window: UIWindow?
  var reactNativeFactory: RCTReactNativeFactory?

  func scene(
    _ scene: UIScene,
    willConnectTo session: UISceneSession,
    options connectionOptions: UIScene.ConnectionOptions
  ) {
    guard let windowScene = scene as? UIWindowScene else {
      return
    }

    // (1) After didFinishLaunching, with this scene connected: React Native starts from the scene.  A background
    // launch for a fetch connects one too, in the background state.
    NSLog("%@", "LAB point=sceneWillConnect scenes=\(UIApplication.shared.connectedScenes.count) " +
          "state=\(LabState.name(UIApplication.shared.applicationState))")

    // (ios-registration) The SDK's launch call made late, as a plugin's own start-up would make it where that start-up
    // comes with the scene (AppDelegate.swift, LabLaunchCall).  The SDK's `launched` flag must make it a no-op: its +load path has already
    // registered every permitted identifier, and BGTaskScheduler raises on a second registration of one.
    if LabLaunchCall.value == "scene" {
      NSLog("%@", "LAB point=sdkLaunchCall at=scene")
      TSBackgroundFetch.sharedInstance().didFinishLaunching()
    }

    // (2) No "Connect to Metro" banner: this host always loads the embedded bundle.
    RCTDevLoadingViewSetEnabled(false)

    dependencyProvider = RCTAppDependencyProvider()
    reactNativeFactory = RCTReactNativeFactory(delegate: self)
    window = UIWindow(windowScene: windowScene)

    #if DEBUG
    let devMenuConfiguration = RCTDevMenuConfiguration(
      devMenuEnabled: true,
      shakeGestureEnabled: true,
      keyboardShortcutsEnabled: true
    )
    reactNativeFactory?.devMenuConfiguration = devMenuConfiguration
    #endif

    reactNativeFactory?.startReactNative(
      withModuleName: "FetchLab",
      in: window,
      connectionOptions: connectionOptions
    )


  }

  func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
    RCTLinkingManager.scene(scene, openURLContexts: URLContexts)
  }

  func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
    RCTLinkingManager.scene(scene, continue: userActivity)
  }

  // (3) The embedded bundle, in Debug too.
  override func bundleURL() -> URL? {
    Bundle.main.url(forResource: "main", withExtension: "jsbundle")
  }
}
