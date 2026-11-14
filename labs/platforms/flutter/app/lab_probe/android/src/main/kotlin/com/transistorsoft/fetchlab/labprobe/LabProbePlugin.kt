package com.transistorsoft.fetchlab.labprobe

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.util.Base64
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel
import java.io.File
import java.lang.ref.WeakReference
import java.lang.reflect.Modifier

/**
 * (WO-002) The Flutter fetch lab's native half: the `lab/probe` channel the Dart driver and its headless task call.
 *
 * A Flutter plugin, not a channel on MainActivity, because the headless task runs in the background_fetch plugin's own
 * FlutterEngine (its HeadlessTask builds `new FlutterEngine(context)`), which registers the pubspec's plugins and
 * nothing the Activity adds: there the task reads the persisted knobs and the probe through this same channel.
 *
 * Methods (the Capacitor host's LabPlugin, key for key):
 *   args    → the launch's args (MainActivity stashes its intent in [launchIntent]); `boot` counts page boots per process
 *   knobs   → {finishDelayMs, withholdFinish, finishOnTimeout} as the LAST launch persisted them (lab-knobs)
 *   probe   → the native probe line
 *   verdict → the host's verdict (run.py writes files/lab-verdict.json), or ""
 */
class LabProbePlugin : FlutterPlugin, MethodChannel.MethodCallHandler {
    private var channel: MethodChannel? = null
    private var context: Context? = null

    override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        context = binding.applicationContext
        channel = MethodChannel(binding.binaryMessenger, "lab/probe").also { it.setMethodCallHandler(this) }
    }

    override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
        channel?.setMethodCallHandler(null)
        channel = null
    }

    override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
        val ctx = context ?: return result.error("detached", "no context", null)
        when (call.method) {
            "args" -> {
                val i = launchIntent
                result.success(mapOf(
                    "stopOnTerminate" to bool(i, "stopOnTerminate", true),
                    "noConfigure" to bool(i, "noConfigure", false),
                    "finishDelayMs" to int(i, "finishDelayMs", DEFAULT_FINISH_DELAY_MS),
                    "withholdFinish" to bool(i, "withholdFinish", false),
                    "finishOnTimeout" to bool(i, "finishOnTimeout", true),
                    "restartOnFinish" to bool(i, "restartOnFinish", false),
                    "scenario" to string(i, "scenario"),
                    "nativeLabel" to string(i, "nativeLabel"),
                    "config" to decode(i, "configB64", "{}"),
                    "hooks" to decode(i, "hooksB64", "[]"),
                    "events" to decode(i, "eventsB64", "[]"),
                    "view" to string(i, "view"),
                    "boot" to ++boots,
                ))
            }
            "knobs" -> result.success(knobs(ctx))
            "probe" -> result.success(probe(ctx))
            "verdict" -> result.success(
                File(ctx.filesDir, "lab-verdict.json").let { if (it.exists()) it.readText() else "" })
            else -> result.notImplemented()
        }
    }

    companion object {
        const val TAG = "LAB"
        const val DEFAULT_FINISH_DELAY_MS = 1000
        private const val KNOBS = "lab-knobs"
        private const val PKG = "com.transistorsoft.tsbackgroundfetch."

        /** The launch intent, set by MainActivity.onCreate before its engine attaches.  A recreation's or a restart's
         *  new Activity sets its own; the headless engine has none (and never calls `args`). */
        @JvmStatic @Volatile var launchIntent: Intent? = null

        /** The Activity resumed most recently (MainActivity.onResume): the probe's `current=`, which restartOrder reads. */
        @JvmStatic @Volatile var current: WeakReference<Activity>? = null

        /** Page boots in this process: each Dart engine's driver calls `args` once. */
        @JvmStatic var boots = 0

        // --- launch extras: read whatever type `am start` sent them as (--ez / --ei / --es) --------------------------

        @JvmStatic fun decode(intent: Intent?, extra: String, fallback: String): String =
            intent?.getStringExtra(extra)?.let { String(Base64.decode(it, Base64.DEFAULT)) } ?: fallback

        @JvmStatic fun string(intent: Intent?, key: String): String = intent?.getStringExtra(key) ?: ""

        @Suppress("DEPRECATION")
        @JvmStatic fun bool(intent: Intent?, key: String, fallback: Boolean): Boolean =
            when (val v = intent?.extras?.get(key)) {
                is Boolean -> v
                is Number -> v.toInt() != 0
                is String -> v.equals("true", ignoreCase = true) || v == "1"
                else -> fallback
            }

        @Suppress("DEPRECATION")
        @JvmStatic fun int(intent: Intent?, key: String, fallback: Int): Int =
            when (val v = intent?.extras?.get(key)) {
                is Number -> v.toInt()
                is String -> v.trim().toIntOrNull() ?: fallback
                else -> fallback
            }

        @JvmStatic fun id(o: Any?): String =
            if (o == null) "null" else o.javaClass.simpleName + "@" + Integer.toHexString(System.identityHashCode(o))

        // --- the fetch knobs: launch args the HEADLESS task needs too, so persisted ----------------------------------

        /** The launch's finish knobs, where the headless task (no intent) can read them: the LAST launch's.  commit, not
         *  apply: a kill right after the launch must not lose them. */
        @JvmStatic fun saveKnobs(context: Context, intent: Intent?) {
            context.applicationContext.getSharedPreferences(KNOBS, Context.MODE_PRIVATE).edit()
                .putInt("finishDelayMs", int(intent, "finishDelayMs", DEFAULT_FINISH_DELAY_MS))
                .putBoolean("withholdFinish", bool(intent, "withholdFinish", false))
                .putBoolean("finishOnTimeout", bool(intent, "finishOnTimeout", true))
                .commit()
        }

        @JvmStatic fun knobs(context: Context): Map<String, Any> {
            val p = context.applicationContext.getSharedPreferences(KNOBS, Context.MODE_PRIVATE)
            return mapOf("finishDelayMs" to p.getInt("finishDelayMs", DEFAULT_FINISH_DELAY_MS),
                         "withholdFinish" to p.getBoolean("withholdFinish", false),
                         "finishOnTimeout" to p.getBoolean("finishOnTimeout", true))
        }

        // --- the probe (the Capacitor and React Native hosts'): reflective, never building an SDK singleton ----------

        /** A class's own singleton, read from its static field BY TYPE — never by calling getInstance(), which would
         *  build it and change what is being observed.  null: the class is absent; (cls, null): no instance yet. */
        private fun singleton(className: String): Pair<Class<*>, Any?>? = try {
            val cls = Class.forName(className)
            val f = cls.declaredFields.firstOrNull { Modifier.isStatic(it.modifiers) && it.type == cls }
            if (f == null) Pair(cls, null) else { f.isAccessible = true; Pair(cls, f.get(null)) }
        } catch (t: Throwable) {
            null
        }

        private fun call(pair: Pair<Class<*>, Any?>?, method: String): String {
            val instance = pair?.second ?: return "-"
            return try { pair.first.getMethod(method).invoke(instance).toString() } catch (t: Throwable) { "err" }
        }

        /** Native state no log line carries: LifecycleManager's headless and background flags, whether BackgroundFetch
         *  has been built, and the task ids the SDK has persisted.  Reflective: the plugin declares the SDK
         *  `implementation`, so neither this module nor the app compiles against it, and a renamed member reads `err`. */
        @JvmStatic fun probe(context: Context): String = try {
            val lm = singleton(PKG + "LifecycleManager")
            val adapter = singleton(PKG + "BackgroundFetch")
            val set = context.applicationContext.getSharedPreferences("TSBackgroundFetch", Context.MODE_PRIVATE)
                .getStringSet("tasks", null)
            val tasks = if (set.isNullOrEmpty()) "-" else set.sorted().joinToString(",")
            "headless=${call(lm, "isHeadless")} background=${call(lm, "isBackground")} " +
                "adapter=${adapter?.second != null} tasks=$tasks current=${id(current?.get())}"
        } catch (t: Throwable) {
            "probeError=" + t.javaClass.simpleName
        }
    }
}
