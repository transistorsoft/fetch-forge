# Setup

## Installation

The `tsbackgroundfetch` library is published to **Maven Central** — no custom repository is required.

```kotlin
// app/build.gradle.kts
dependencies {
    implementation("com.transistorsoft:tsbackgroundfetch:4.1.1")
}
```

!!! success "No manifest configuration required"
    The library AAR contributes its own `FetchJobService`, `BootReceiver`, `FetchAlarmReceiver`
    and the `RECEIVE_BOOT_COMPLETED` / `WAKE_LOCK` / `SCHEDULE_EXACT_ALARM` permissions via
    manifest merge — your app declares none of them.

## Headless tasks

To keep receiving events after the app is terminated (`stopOnTerminate: false` /
`startOnBoot: true`), register a headless `jobService` class:

```kotlin
BackgroundFetchConfig.Builder()
    .setTaskId("com.transistorsoft.fetch")
    .setStopOnTerminate(false)
    .setStartOnBoot(true)
    .setJobService(MyHeadlessTask::class.java.name)
    .build()
```

The class named by `setJobService` is instantiated by reflection via a
`HeadlessTask(Context, BGTask)` constructor; call
`BackgroundFetch.getInstance(context).finish(taskId)` when its work is done.
