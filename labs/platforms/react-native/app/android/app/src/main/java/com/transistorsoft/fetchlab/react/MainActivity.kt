package com.transistorsoft.fetchlab.react

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.util.Log

import com.facebook.react.ReactActivity
import com.facebook.react.ReactActivityDelegate
import com.facebook.react.defaults.DefaultNewArchitectureEntryPoint.fabricEnabled
import com.facebook.react.defaults.DefaultReactActivityDelegate

import java.lang.ref.WeakReference
import java.lang.reflect.Modifier

/**
 * React Native fetch-lab host (WO-002) — bg-forge's lab protocol (bg-forge labs/README.md "The lab protocol"),
 * modelled on bg-forge's labs/platforms/react-native MainActivity at 49d38d47f.
 *
 * Scenario args (intent extras, all optional): configB64, hooksB64, eventsB64, scenario, nativeLabel, view,
 * stopOnTerminate, finishNow (a second `am start`), and the fetch knobs noConfigure, finishDelayMs,
 * withholdFinish, finishOnTimeout (lab.yaml notes).  Extras of any other name are tolerated.
 *
 * React Native keeps its JavaScript context across a recreation (ReactHost lives on the Application) while the
 * React root re-mounts, so the driver's main() runs again; it configures once per JS context.
 */
class MainActivity : ReactActivity() {
  companion object {
    const val TAG = "LAB"
    var sCreates = 0
    var sJsBoots = 0
    var sCurrent: WeakReference<Activity>? = null

    /** The launch intent, stashed in onCreate so args() never depends on currentActivity: after a recreation
     *  the re-mounted driver asks for its args before ReactHost hands the new Activity over. */
    var sIntent: Intent? = null

    const val PKG = "com.transistorsoft.tsbackgroundfetch."

    fun decode(intent: Intent?, extra: String, fallback: String): String =
      intent?.getStringExtra(extra)
        ?.let { String(android.util.Base64.decode(it, android.util.Base64.DEFAULT)) } ?: fallback

    fun id(o: Any?): String =
      if (o == null) "null" else o.javaClass.simpleName + "@" + Integer.toHexString(System.identityHashCode(o))

    /** An extra of whatever type `am start` sent it as (--ez / --ei / --es), read as a boolean. */
    @Suppress("DEPRECATION")
    fun bool(intent: Intent?, key: String, fallback: Boolean): Boolean =
      when (val v = intent?.extras?.get(key)) {
        is Boolean -> v
        is Number -> v.toInt() != 0
        is String -> v.equals("true", ignoreCase = true) || v == "1"
        else -> fallback
      }

    /** An extra of whatever type `am start` sent it as, read as an int. */
    @Suppress("DEPRECATION")
    fun int(intent: Intent?, key: String, fallback: Int): Int =
      when (val v = intent?.extras?.get(key)) {
        is Number -> v.toInt()
        is String -> v.trim().toIntOrNull() ?: fallback
        else -> fallback
      }

    // --- the fetch knobs: launch args the HEADLESS task needs too, so persisted -----------------------------

    private const val KNOBS = "lab-knobs"
    const val DEFAULT_FINISH_DELAY_MS = 1000

    /** The launch's finish knobs, written where the headless task (no intent) can read them: the LAST launch's. */
    fun saveKnobs(context: Context, intent: Intent?) {
      context.applicationContext.getSharedPreferences(KNOBS, Context.MODE_PRIVATE).edit()
        .putInt("finishDelayMs", int(intent, "finishDelayMs", DEFAULT_FINISH_DELAY_MS))
        .putBoolean("withholdFinish", bool(intent, "withholdFinish", false))
        .putBoolean("finishOnTimeout", bool(intent, "finishOnTimeout", true))
        .commit()   // commit, not apply: a kill right after the launch must not lose them
    }

    fun knobs(context: Context): Triple<Int, Boolean, Boolean> {
      val p = context.applicationContext.getSharedPreferences(KNOBS, Context.MODE_PRIVATE)
      return Triple(p.getInt("finishDelayMs", DEFAULT_FINISH_DELAY_MS), p.getBoolean("withholdFinish", false),
        p.getBoolean("finishOnTimeout", true))
    }

    // --- the probe -------------------------------------------------------------------------------------------

    /** A class's own singleton, read from its static field BY TYPE — never by calling getInstance(), which would
     *  build it and change what is being observed.  null: the class is absent, or no instance exists yet. */
    private fun singleton(className: String): Pair<Class<*>, Any?>? = try {
      val cls = Class.forName(className)
      val field = cls.declaredFields.firstOrNull { Modifier.isStatic(it.modifiers) && it.type == cls }
      cls to field?.apply { isAccessible = true }?.get(null)
    } catch (e: ClassNotFoundException) {
      null
    }

    private fun call(pair: Pair<Class<*>, Any?>?, method: String): String {
      val instance = pair?.second ?: return "-"
      return runCatching { pair.first.getMethod(method).invoke(instance).toString() }.getOrElse { "err" }
    }

    /**
     * Native state no log line carries (bg-forge README §2): the SDK's headless and background flags from
     * LifecycleManager, whether BackgroundFetch has been built, and the task ids it has persisted.  Reflective,
     * so the host compiles against no SDK API and a renamed member reads `err` instead of breaking the build.
     */
    fun probe(context: Context): String = try {
      val lm = singleton(PKG + "LifecycleManager")
      val adapter = singleton(PKG + "BackgroundFetch")
      val tasks = context.applicationContext.getSharedPreferences("TSBackgroundFetch", Context.MODE_PRIVATE)
        .getStringSet("tasks", null)?.sorted()?.joinToString(",")?.ifEmpty { null } ?: "-"
      "headless=${call(lm, "isHeadless")} background=${call(lm, "isBackground")} " +
        "adapter=${adapter?.second != null} tasks=$tasks current=${id(sCurrent?.get())}"
    } catch (t: Throwable) {
      "probeError=${t.javaClass.simpleName}"
    }
  }

  private val handler = Handler(Looper.getMainLooper())

  override fun getMainComponentName(): String = "FetchLab"

  override fun createReactActivityDelegate(): ReactActivityDelegate =
    DefaultReactActivityDelegate(this, mainComponentName, fabricEnabled)

  override fun onCreate(savedInstanceState: Bundle?) {
    sCreates++
    sIntent = intent
    saveKnobs(this, intent)
    Log.i(TAG, "point=onCreate activity=${id(this)} pid=${Process.myPid()} creates=$sCreates " +
      "recreated=${savedInstanceState != null} ${probe(this)}")
    super.onCreate(savedInstanceState)
    Log.i(TAG, "point=onCreate:afterSuper activity=${id(this)} ${probe(this)}")
  }

  override fun onResume() {
    super.onResume()
    sCurrent = WeakReference(this)
    handler.postDelayed({ Log.i(TAG, "point=resumed+2s activity=${id(this)} ${probe(this)}") }, 2000)
  }

  override fun onNewIntent(intent: Intent) {
    if (bool(intent, "finishNow", false)) {
      Log.i(TAG, "point=finishNow activity=${id(this)}")
      finish()
      return
    }
    super.onNewIntent(intent)
  }

  override fun onDestroy() {
    handler.removeCallbacksAndMessages(null)
    Log.i(TAG, "point=onDestroy activity=${id(this)} isChangingConfigurations=$isChangingConfigurations " +
      "isFinishing=$isFinishing ${probe(this)}")
    super.onDestroy()
    // The plugin's onHostDestroy (LifecycleManager.setHeadless(true)) has run inside super.onDestroy().
    Log.i(TAG, "point=onDestroy:afterSuper activity=${id(this)} ${probe(this)}")
  }
}
