# Examples

## Basic

```kotlin
val config = BackgroundFetchConfig.Builder()
    .setMinimumFetchInterval(15)   // minutes
    .build()

BackgroundFetch.getInstance(context).configure(config, object : BackgroundFetch.Callback {
    override fun onFetch(taskId: String) {
        Log.d(TAG, "[BackgroundFetch] Event received: $taskId")
        // Perform your work here...
        BackgroundFetch.getInstance(context).finish(taskId)
    }
    override fun onTimeout(taskId: String) {
        Log.d(TAG, "[BackgroundFetch] TIMEOUT: $taskId")
        BackgroundFetch.getInstance(context).finish(taskId)
    }
})
```

## Custom task

```kotlin
val task = BackgroundFetchConfig.Builder()
    .setTaskId("com.transistorsoft.customtask")
    .setDelay(900_000L)   // milliseconds
    .setPeriodic(true)
    .build()

BackgroundFetch.getInstance(context).scheduleTask(task)
```

A complete runnable example app lives in [`example/android/`](https://github.com/transistorsoft/transistor-background-fetch/tree/master/example/android) of the repository.
