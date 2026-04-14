# Capacitor — Setup

[![](https://img.shields.io/npm/v/@transistorsoft/capacitor-background-fetch?style=flat-square)](https://www.npmjs.com/package/@transistorsoft/capacitor-background-fetch)

## Installation

=== "npm"

    ```bash
    npm install @transistorsoft/capacitor-background-fetch
    npx cap sync
    ```

=== "yarn"

    ```bash
    yarn add @transistorsoft/capacitor-background-fetch
    npx cap sync
    ```


## iOS Setup

### Background Modes

{{> ios-background-modes.md}}

### Info.plist

{{> ios-info-plist.md}}

### AppDelegate.swift

```swift hl_lines="3 13-16 21-27"
import UIKit
import Capacitor
import TSBackgroundFetch

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions:
                       [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // [REQUIRED] Register BackgroundFetch
        let fetchManager = TSBackgroundFetch.sharedInstance()
        fetchManager?.didFinishLaunching()

        return true
    }

    // [REQUIRED] Background fetch delegate
    func application(_ application: UIApplication,
                     performFetchWithCompletionHandler completionHandler:
                       @escaping (UIBackgroundFetchResult) -> Void) {
        let fetchManager = TSBackgroundFetch.sharedInstance()
        fetchManager?.perform(completionHandler: completionHandler,
                              applicationState: application.applicationState)
    }
}
```


## Android Setup

No additional Android setup is required.

