# Examples

## Basic Usage

```dart
import 'package:background_fetch/background_fetch.dart';

class _MyAppState extends State<MyApp> {
  @override
  void initState() {
    super.initState();
    initBackgroundFetch();
  }

  Future<void> initBackgroundFetch() async {
    int status = await BackgroundFetch.configure(
      BackgroundFetchConfig(
        minimumFetchInterval: 15, // minutes
        stopOnTerminate: false,
        startOnBoot: true,
        enableHeadless: true,
      ),
      (String taskId) async {
        // <-- Event callback
        print("[BackgroundFetch] Event received: $taskId");
        // Perform your work here...
        BackgroundFetch.finish(taskId);
      },
      (String taskId) async {
        // <-- Timeout callback
        print("[BackgroundFetch] TIMEOUT: $taskId");
        BackgroundFetch.finish(taskId);
      },
    );
    print("[BackgroundFetch] configure status: $status");
  }
}
```

## Scheduling a Custom Task

```dart
BackgroundFetch.scheduleTask(TaskConfig(
  taskId: "com.example.my-custom-task",
  delay: 60000, // milliseconds
  periodic: false,
  forceAlarmManager: true,
  requiredNetworkType: NetworkType.ANY,
));
```

All scheduled tasks fire into the same callback provided to `configure`.  Use a `switch` statement to route by `taskId`:

```dart
int status = await BackgroundFetch.configure(
  BackgroundFetchConfig(minimumFetchInterval: 15),
  (String taskId) async {
    switch (taskId) {
      case "com.example.my-custom-task":
        print("Custom task fired");
        break;
      default:
        print("Default fetch event");
    }
    BackgroundFetch.finish(taskId);
  },
  (String taskId) async {
    BackgroundFetch.finish(taskId);
  },
);
```

## Headless Task (Android)

When the app is terminated, Android can continue executing background-fetch events via a headless Dart isolate.

Register your headless task in **`lib/main.dart`**.  The callback **must** be a top-level or static function annotated with `@pragma('vm:entry-point')`:

```dart
import 'package:flutter/material.dart';
import 'package:background_fetch/background_fetch.dart';

@pragma('vm:entry-point')
void backgroundFetchHeadlessTask(HeadlessEvent task) async {
  String taskId = task.taskId;
  bool isTimeout = task.timeout;

  if (isTimeout) {
    print("[BackgroundFetch] Headless TIMEOUT: $taskId");
    BackgroundFetch.finish(taskId);
    return;
  }

  print("[BackgroundFetch] Headless event: $taskId");
  // Perform your work here...
  BackgroundFetch.finish(taskId);
}

void main() {
  runApp(MyApp());

  // Register to receive BackgroundFetch events after app is terminated.
  BackgroundFetch.registerHeadlessTask(backgroundFetchHeadlessTask);
}
```

!!! note
    See [enableHeadless](BackgroundFetchConfig.md#enableheadless) for full setup details.
    Requires [stopOnTerminate](BackgroundFetchConfig.md#stoponterminate) `false` and [enableHeadless](BackgroundFetchConfig.md#enableheadless) `true`.

## Checking Status

```dart
int status = await BackgroundFetch.status;

switch (status) {
  case BackgroundFetch.STATUS_AVAILABLE:
    print("Background fetch is available");
    break;
  case BackgroundFetch.STATUS_DENIED:
    print("Background fetch is denied by the user");
    break;
  case BackgroundFetch.STATUS_RESTRICTED:
    print("Background fetch is restricted (e.g. parental controls)");
    break;
}
```

## Stopping Tasks

```dart
// Stop a specific scheduled task
await BackgroundFetch.stop("com.example.my-custom-task");

// Stop ALL tasks (including the default fetch event)
await BackgroundFetch.stop();
```
