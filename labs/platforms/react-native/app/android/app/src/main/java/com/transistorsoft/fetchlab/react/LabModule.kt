package com.transistorsoft.fetchlab.react

import com.facebook.react.bridge.Arguments
import com.facebook.react.bridge.Promise
import com.facebook.react.bridge.ReactApplicationContext
import com.facebook.react.bridge.ReactContextBaseJavaModule
import com.facebook.react.bridge.ReactMethod

/** JavaScript side of the lab: scenario args, a boot counter, the native probe, the host's verdict, and the
 *  persisted finish knobs the headless task reads. */
class LabModule(private val reactContext: ReactApplicationContext) :
  ReactContextBaseJavaModule(reactContext) {

  override fun getName(): String = "Lab"

  /** The launch's args.  Only what the driver honours is put here: the boot line echoes this map, so it is the
   *  record of which extras this app implements (bg-forge README §1). */
  @ReactMethod
  fun args(promise: Promise) {
    // The intent MainActivity.onCreate stashed, never reactContext.currentActivity (null after a recreation
    // until ReactHost hands the new Activity over).
    val intent = MainActivity.sIntent
    val map = Arguments.createMap()
    map.putBoolean("stopOnTerminate", MainActivity.bool(intent, "stopOnTerminate", true))
    map.putBoolean("noConfigure", MainActivity.bool(intent, "noConfigure", false))
    map.putInt("finishDelayMs", MainActivity.int(intent, "finishDelayMs", MainActivity.DEFAULT_FINISH_DELAY_MS))
    map.putBoolean("withholdFinish", MainActivity.bool(intent, "withholdFinish", false))
    map.putBoolean("finishOnTimeout", MainActivity.bool(intent, "finishOnTimeout", true))
    map.putString("scenario", intent?.getStringExtra("scenario") ?: "")
    map.putString("nativeLabel", intent?.getStringExtra("nativeLabel") ?: "")
    // The header's top inset: the platform's status_bar_height resource, never the window (a getWindow()
    // from the JS thread is the off-main read bg-forge's decor-race probe counts).
    val res = reactContext.resources
    val sb = res.getIdentifier("status_bar_height", "dimen", "android")
    map.putDouble("statusBarHeight",
      if (sb > 0) res.getDimensionPixelSize(sb) / res.displayMetrics.density.toDouble() else 0.0)
    map.putString("config", MainActivity.decode(intent, "configB64", "{}"))
    map.putString("hooks", MainActivity.decode(intent, "hooksB64", "[]"))
    map.putString("events", MainActivity.decode(intent, "eventsB64", "[]"))
    map.putString("view", intent?.getStringExtra("view") ?: "")
    map.putInt("boot", ++MainActivity.sJsBoots)
    promise.resolve(map)
  }

  @ReactMethod
  fun probe(promise: Promise) {
    promise.resolve(MainActivity.probe(reactContext))
  }

  /** (WO-002) The finish knobs as the LAST launch persisted them (MainActivity.saveKnobs): the headless task has
   *  no intent of its own. */
  @ReactMethod
  fun knobs(promise: Promise) {
    val (delay, withhold, onTimeout) = MainActivity.knobs(reactContext)
    val map = Arguments.createMap()
    map.putInt("finishDelayMs", delay)
    map.putBoolean("withholdFinish", withhold)
    map.putBoolean("finishOnTimeout", onTimeout)
    promise.resolve(map)
  }

  /** The host's verdict for the device view: the file run.py pushes into this app's files dir
   *  (backends.Adb.push_verdict), or "" before the first push.  Read here, never judged. */
  @ReactMethod
  fun verdict(promise: Promise) {
    val f = java.io.File(reactContext.filesDir, "lab-verdict.json")
    promise.resolve(runCatching { if (f.exists()) f.readText() else "" }.getOrDefault(""))
  }
}
