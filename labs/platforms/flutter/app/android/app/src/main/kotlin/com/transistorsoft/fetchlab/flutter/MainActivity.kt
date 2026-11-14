package com.transistorsoft.fetchlab.flutter

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.util.Log
import com.transistorsoft.fetchlab.labprobe.LabProbePlugin
import io.flutter.embedding.android.FlutterActivity
import java.lang.ref.WeakReference

/**
 * Flutter fetch-lab host (WO-002): the template's FlutterActivity with the lab protocol's lifecycle lines, after the
 * Capacitor and React Native hosts' MainActivity.
 *
 * Scenario args arrive as intent extras (configB64, hooksB64, eventsB64, scenario, nativeLabel, view, stopOnTerminate,
 * finishNow — a second `am start` —, restartOnFinish, and the fetch knobs noConfigure, finishDelayMs, withholdFinish,
 * finishOnTimeout).  The Dart driver reads them through LabProbePlugin's `args`; the knobs are persisted for the
 * headless task, which has no intent.
 *
 * Each Activity has its own FlutterEngine (FlutterActivity's default), so a recreation or a restart boots the Dart
 * driver again.  The background_fetch plugin hears the destroy inside super.onDestroy()
 * (FlutterActivityAndFragmentDelegate onDetach), so onDestroy:afterSuper below is after it — but which hook depends on
 * the destroy: a FINISHING one calls onDetachedFromActivity (setActivity(null, null): headless true), a recreation
 * onDetachedFromActivityForConfigChanges, which the plugin leaves empty (BackgroundFetchPlugin.java:34-38), and the
 * engine's destroy then reaches only onDetachedFromEngine: after a recreation no plugin hook has touched the flag.
 */
class MainActivity : FlutterActivity() {
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        sCreates++
        // Before super: super.onCreate builds the engine, whose Dart driver asks `args` for this launch.
        LabProbePlugin.launchIntent = intent
        LabProbePlugin.saveKnobs(this, intent)
        Log.i(TAG, "point=onCreate activity=${LabProbePlugin.id(this)} pid=${Process.myPid()} creates=$sCreates " +
            "recreated=${savedInstanceState != null} ${LabProbePlugin.probe(this)}")
        super.onCreate(savedInstanceState)
        Log.i(TAG, "point=onCreate:afterSuper activity=${LabProbePlugin.id(this)} ${LabProbePlugin.probe(this)}")
    }

    override fun onResume() {
        super.onResume()
        LabProbePlugin.current = WeakReference(this)
        handler.postDelayed({ Log.i(TAG, "point=resumed+2s activity=${LabProbePlugin.id(this)} ${LabProbePlugin.probe(this)}") }, 2000)
    }

    override fun onNewIntent(intent: Intent) {
        if (LabProbePlugin.bool(intent, "finishNow", false)) {
            // (WO-007) restartOnFinish rides on this Activity's OWN launch (`this.intent`: the parameter shadows it), as
            // the trigger's `am start` carries finishNow alone.  The app restarts itself the way apps do: finish() FIRST
            // (singleTop would otherwise hand the relaunch to this live instance's onNewIntent), then startActivity() of
            // a copy of the launch.  Synchronous: onDestroy clears this Activity's handler.  The new Activity resumes
            // before this one is destroyed (restartOrder reads it).
            val restart = LabProbePlugin.bool(this.intent, "restartOnFinish", false)
            Log.i(TAG, "point=finishNow activity=${LabProbePlugin.id(this)} restart=$restart")
            finish()
            if (restart) {
                startActivity(Intent(this, MainActivity::class.java).putExtras(this.intent).apply { removeExtra("finishNow") })
            }
            return
        }
        super.onNewIntent(intent)
    }

    override fun onDestroy() {
        handler.removeCallbacksAndMessages(null)
        Log.i(TAG, "point=onDestroy activity=${LabProbePlugin.id(this)} isChangingConfigurations=$isChangingConfigurations " +
            "isFinishing=$isFinishing ${LabProbePlugin.probe(this)}")
        super.onDestroy()
        Log.i(TAG, "point=onDestroy:afterSuper activity=${LabProbePlugin.id(this)} ${LabProbePlugin.probe(this)}")
    }

    companion object {
        const val TAG = LabProbePlugin.TAG
        var sCreates = 0
    }
}
