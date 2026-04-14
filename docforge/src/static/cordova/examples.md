# Examples

## Basic Usage

```javascript
import BackgroundFetch from "cordova-plugin-background-fetch";

const status = await BackgroundFetch.configure(
  {
    minimumFetchInterval: 15, // minutes
  },
  function (taskId) {
    // <-- Event callback
    console.log("[BackgroundFetch] Event received:", taskId);
    // Perform your work here...
    BackgroundFetch.finish(taskId);
  },
  function (taskId) {
    // <-- Timeout callback
    console.log("[BackgroundFetch] TIMEOUT:", taskId);
    BackgroundFetch.finish(taskId);
  }
);
console.log("[BackgroundFetch] configure status:", status);
```

## Scheduling a Custom Task

```javascript
BackgroundFetch.scheduleTask({
  taskId: "com.example.my-custom-task",
  delay: 60000, // milliseconds
  periodic: false,
  forceAlarmManager: true,
  requiredNetworkType: BackgroundFetch.NETWORK_TYPE_ANY,
});
```

All scheduled tasks fire into the same callback provided to `configure`.  Use a `switch` statement to route by `taskId`:

```javascript
BackgroundFetch.configure(
  { minimumFetchInterval: 15 },
  function (taskId) {
    switch (taskId) {
      case "com.example.my-custom-task":
        console.log("Custom task fired");
        break;
      default:
        console.log("Default fetch event");
    }
    BackgroundFetch.finish(taskId);
  },
  function (taskId) {
    BackgroundFetch.finish(taskId);
  }
);
```

## Headless Task (Android)

When the app is terminated, Android can continue executing background-fetch events via a custom Java class.

!!! warning
    You must be prepared to **write Java code**.

**Step 1:** Create **`BackgroundFetchHeadlessTask.java`** (e.g. in `src/android/`):

```java
package com.transistorsoft.cordova.backgroundfetch;

import android.content.Context;
import android.util.Log;

import com.transistorsoft.tsbackgroundfetch.BackgroundFetch;
import com.transistorsoft.tsbackgroundfetch.BGTask;

public class BackgroundFetchHeadlessTask implements HeadlessTask {
    @Override
    public void onFetch(Context context, BGTask task) {
        String taskId = task.getTaskId();
        boolean isTimeout = task.getTimedOut();
        if (isTimeout) {
            Log.d(BackgroundFetch.TAG, "TIMEOUT: " + taskId);
            BackgroundFetch.getInstance(context).finish(taskId);
            return;
        }
        Log.d(BackgroundFetch.TAG, "onFetch: " + taskId);
        // Perform your work here...

        BackgroundFetch.getInstance(context).finish(taskId);
    }
}
```

**Step 2:** In your **`config.xml`**, add a **`<resource-file>`** within `<platform name="android">`:

```xml
<platform name="android">
    <resource-file
        src="src/android/BackgroundFetchHeadlessTask.java"
        target="app/src/main/java/com/transistorsoft/cordova/backgroundfetch/BackgroundFetchHeadlessTask.java" />
</platform>
```

!!! note
    See [enableHeadless](BackgroundFetchConfig.md#enableheadless) for full setup details.
    Requires [stopOnTerminate](BackgroundFetchConfig.md#stoponterminate) `false` and [enableHeadless](BackgroundFetchConfig.md#enableheadless) `true`.

## Checking Status

```javascript
var status = await BackgroundFetch.status;

switch (status) {
  case BackgroundFetch.STATUS_AVAILABLE:
    console.log("Background fetch is available");
    break;
  case BackgroundFetch.STATUS_DENIED:
    console.log("Background fetch is denied by the user");
    break;
  case BackgroundFetch.STATUS_RESTRICTED:
    console.log("Background fetch is restricted (e.g. parental controls)");
    break;
}
```

## Stopping Tasks

```javascript
// Stop a specific scheduled task
BackgroundFetch.stop("com.example.my-custom-task");

// Stop ALL tasks (including the default fetch event)
BackgroundFetch.stop();
```
