package com.transistorsoft.cordova.backgroundfetch;

import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;

import com.transistorsoft.fetchlab.cordova.MainActivity;
import com.transistorsoft.tsbackgroundfetch.BGTask;
import com.transistorsoft.tsbackgroundfetch.BackgroundFetch;

/**
 * (WO-002) The headless task: a process JobScheduler started with no Activity.  Cordova's headless mode is Java-only
 * (no JavaScript runs headless).  CDVBackgroundFetch hands the SDK this FIXED class name, in the plugin's own package
 * (CDVBackgroundFetch.java HEADLESS_JOB_SERVICE_CLASS), whatever the app's package is; the plugin's plugin.xml does not
 * install one, so a customer adds theirs (a config.xml resource-file) and build.sh copies this one.  The SDK reflects
 * it as `new BackgroundFetchHeadlessTask().onFetch(context, task)` (BGTask.fireHeadlessEvent's second form).
 *
 * It has no launch intent, so it reads the knobs the LAST launch persisted (MainActivity.saveKnobs), and logs what the
 * Capacitor host's task logs: `point=headless`, `point=task-probe`, `point=task-api api=finish`.  Nothing here logs
 * `point=headless-…`: a headless counter reads every `point=headless` line as a delivery.
 */
public class BackgroundFetchHeadlessTask implements HeadlessTask {
    private static final Handler sMain = new Handler(Looper.getMainLooper());

    @Override
    public void onFetch(Context context, BGTask task) {
        final Context ctx = context.getApplicationContext();
        final String taskId = task.getTaskId();
        final boolean timeout = task.getTimedOut();
        Log.i(MainActivity.TAG, "point=headless event=" + (timeout ? "timeout" : "fetch") + " taskId=" + taskId
                + " timeout=" + timeout);
        Log.i(MainActivity.TAG, "point=task-probe " + MainActivity.probe(ctx));

        Object[] k = MainActivity.knobs(ctx);
        int delayMs = (Integer) k[0];
        boolean withhold = (Boolean) k[1];
        boolean finishOnTimeout = (Boolean) k[2];
        // The drivers' finishAfter(): a timeout finishes at once unless finishOnTimeout is false; a fetch after
        // finishDelayMs unless withholdFinish.
        final long after;
        if (timeout) {
            if (!finishOnTimeout) { withheld(taskId); return; }
            after = 0;
        } else {
            if (withhold) { withheld(taskId); return; }
            after = Math.max(0, delayMs);
        }
        sMain.postDelayed(() -> {
            try {
                BackgroundFetch.getInstance(ctx).finish(taskId);
                Log.i(MainActivity.TAG, "point=task-api api=finish args=" + taskId + " outcome=ok after_ms=" + after);
            } catch (Throwable t) {
                Log.i(MainActivity.TAG, "point=task-api api=finish args=" + taskId + " outcome=threw after_ms=" + after
                        + " message=" + t.getClass().getSimpleName());
            }
        }, after);
    }

    private static void withheld(String taskId) {
        Log.i(MainActivity.TAG, "point=task-api api=finish args=" + taskId + " outcome=withheld after_ms=-");
    }
}
