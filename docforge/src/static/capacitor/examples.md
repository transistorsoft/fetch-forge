# Examples

## Basic Usage

```typescript
import {BackgroundFetch} from '@transistorsoft/capacitor-background-fetch';

const status = await BackgroundFetch.configure(
  {
    minimumFetchInterval: 15, // minutes
    stopOnTerminate: false,
    startOnBoot: true,
    enableHeadless: true,
  },
  async (taskId: string) => {
    // <-- Event callback
    console.log("[BackgroundFetch] Event received:", taskId);
    // Perform your work here...
    BackgroundFetch.finish(taskId);
  },
  async (taskId: string) => {
    // <-- Timeout callback
    console.log("[BackgroundFetch] TIMEOUT:", taskId);
    BackgroundFetch.finish(taskId);
  }
);
console.log("[BackgroundFetch] configure status:", status);
```

## Scheduling a Custom Task

```typescript
BackgroundFetch.scheduleTask({
  taskId: "com.example.my-custom-task",
  delay: 60000, // milliseconds
  periodic: false,
  forceAlarmManager: true,
  requiredNetworkType: BackgroundFetch.NETWORK_TYPE_ANY,
});
```

All scheduled tasks fire into the same callback provided to `configure`.  Use a `switch` statement to route by `taskId`:

```typescript
const status = await BackgroundFetch.configure(
  { minimumFetchInterval: 15 },
  async (taskId: string) => {
    switch (taskId) {
      case "com.example.my-custom-task":
        console.log("Custom task fired");
        break;
      default:
        console.log("Default fetch event");
    }
    BackgroundFetch.finish(taskId);
  },
  async (taskId: string) => {
    BackgroundFetch.finish(taskId);
  }
);
```

## Headless Task (Android)

When the app is terminated, Android can continue executing background-fetch events via a custom Java class.

With your app open in **Android Studio**, browse the **`app`** folder to find the **`MainActivity`** class.  Right-click the *containing folder* and click **New > Java Class**.

You **MUST** name the file **`BackgroundFetchHeadlessTask`**:

```java
package com.your.package.name;  // <-- DO NOT REPLACE THIS LINE

import android.content.Context;
import android.util.Log;

import com.transistorsoft.tsbackgroundfetch.BackgroundFetch;
import com.transistorsoft.tsbackgroundfetch.BGTask;

public class BackgroundFetchHeadlessTask {
    public void onFetch(Context context, BGTask task) {
        BackgroundFetch backgroundFetch = BackgroundFetch.getInstance(context);
        String taskId = task.getTaskId();
        boolean isTimeout = task.getTimedOut();
        if (isTimeout) {
            backgroundFetch.finish(taskId);
            return;
        }
        Log.d("MyHeadlessTask", "onFetch: " + taskId);
        // Do your work here...

        backgroundFetch.finish(taskId);
    }
}
```

!!! note
    See [enableHeadless](BackgroundFetchConfig.md#enableheadless) for full setup details.
    Requires [stopOnTerminate](BackgroundFetchConfig.md#stoponterminate) `false` and [enableHeadless](BackgroundFetchConfig.md#enableheadless) `true`.

## Checking Status

```typescript
const status = await BackgroundFetch.status;

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

```typescript
// Stop a specific scheduled task
await BackgroundFetch.stop("com.example.my-custom-task");

// Stop ALL tasks (including the default fetch event)
await BackgroundFetch.stop();
```
