```swift hl_lines="2 11 12"
import UIKit
import TSBackgroundFetch

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    func application(_ application: UIApplication,
                     didFinishLaunchingWithOptions launchOptions:
                       [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // ...
        // [REQUIRED] Register BackgroundFetch
        TSBackgroundFetch.sharedInstance().didFinishLaunching()

        return true
    }
```
