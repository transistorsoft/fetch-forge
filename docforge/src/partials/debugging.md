# Debugging

Background Fetch events are notoriously difficult to test because the OS controls when they fire.  Use these simulation techniques during development.

## iOS

### Simulating Fetch Events

iOS 13+ uses the `BGTaskScheduler` API.  To simulate a fetch event:

1. Run your app in Xcode.
2. Click the **Pause** button (<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: text-bottom;"><rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/></svg>) to initiate a breakpoint.
3. In the `(lldb)` console, paste:

```objc
e -l objc -- (void)[[BGTaskScheduler sharedScheduler] _simulateLaunchForTaskWithIdentifier:@"com.transistorsoft.fetch"]
```

4. Click the **Continue** button (<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align: text-bottom;"><polygon points="6 3 20 12 6 21 6 3"/></svg>).  The callback provided to `configure` will receive the event.

![](https://dl.dropboxusercontent.com/s/zr7w3g8ivf71u32/ios-simulate-bgtask-pause.png?dl=1)

![](https://dl.dropboxusercontent.com/s/87c9uctr1ka3s1e/ios-simulate-bgtask-paste.png?dl=1)

![](https://dl.dropboxusercontent.com/s/bsv0avap5c2h7ed/ios-simulate-bgtask-play.png?dl=1)

### Simulating Custom `scheduleTask` Events

If you've registered a custom task (e.g. `com.transistorsoft.customtask`), simulate it with:

```objc
e -l objc -- (void)[[BGTaskScheduler sharedScheduler] _simulateLaunchForTaskWithIdentifier:@"com.transistorsoft.customtask"]
```

### Simulating Task Timeout Events

To simulate a task timeout, **do not** call `finish(taskId)` in your event callback, then run:

```objc
e -l objc -- (void)[[BGTaskScheduler sharedScheduler] _simulateExpirationForTaskWithIdentifier:@"com.transistorsoft.fetch"]
```

Your timeout callback will fire, allowing you to verify your timeout handling logic.

### iOS Tips

!!! warning
    iOS can take **hours or even days** before Apple's machine-learning algorithm begins regularly firing fetch events.  Do not sit staring at your logs waiting for an event.  If your *simulated events* work, everything is correctly configured.

- If the user doesn't open your app for long periods of time, iOS will **stop firing events**.
- `scheduleTask` on iOS seems only to run when the device is plugged into power.
- There is **no** `stopOnTerminate: false` for iOS — when the app is terminated, iOS stops firing events.

---

## Android

### Observing Logs

Use `adb logcat` to observe plugin logs:

```bash
adb logcat "*:S" TSBackgroundFetch:V
```

### Simulating Fetch Events

Simulate a background-fetch event (replace `<your.application.id>` with your app's package name):

```bash
adb shell cmd jobscheduler run -f <your.application.id> 999
```

### Simulating Custom `scheduleTask` Events

1. Observe `adb logcat` for the `registerTask` log entry and copy the `jobId`:

```
TSBackgroundFetch: - registerTask: com.your.package.name (jobId: -359368280)
```

2. Paste the `jobId` into the `adb shell` command:

```bash
adb shell cmd jobscheduler run -f <your.application.id> -359368280
```

### Simulating Headless Events

After terminating your app, simulate a fetch event to test your headless task:

```bash
adb shell cmd jobscheduler run -f <your.application.id> 999
```

Verify the headless task executes by observing `adb logcat` output.
