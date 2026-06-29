# Examples

## Basic

```swift
import BackgroundFetch

// Configure the minimum fetch interval and subscribe to app-refresh events.
let status = await BackgroundFetch.shared.configure(minimumFetchInterval: 15 * 60)  // seconds

BackgroundFetch.shared.onFetch(identifier: "MyApp") { event in
    if event.timeout {
        // The OS is about to suspend the app — finish immediately.
        print("[BackgroundFetch] TIMEOUT: \(event.taskId)")
        BackgroundFetch.shared.finish(taskId: event.taskId)
        return
    }
    print("[BackgroundFetch] Event received: \(event.taskId)")
    // Perform your work here...
    BackgroundFetch.shared.finish(taskId: event.taskId)
}
```

## Custom task

```swift
// The identifier must be listed in Info.plist under BGTaskSchedulerPermittedIdentifiers.
try BackgroundFetch.shared.scheduleTask(
    identifier: "com.transistorsoft.customtask",
    delay: 900,        // seconds
    periodic: true
) { event in
    print("[BackgroundFetch] Event received: \(event.taskId)")
    // Perform your work here...
    BackgroundFetch.shared.finish(taskId: event.taskId)
}
```

A complete runnable example app lives in [`example/ios/`](https://github.com/transistorsoft/transistor-background-fetch/tree/master/example/ios) of the repository.
