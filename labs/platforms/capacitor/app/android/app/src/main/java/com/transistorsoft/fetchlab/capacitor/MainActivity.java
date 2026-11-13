package com.transistorsoft.fetchlab.capacitor;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.Process;
import android.util.Log;

import com.getcapacitor.BridgeActivity;

import java.lang.ref.WeakReference;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Set;

/**
 * Capacitor fetch-lab host (WO-002) — bg-forge's lab protocol, ported from bg-forge's labs/platforms/capacitor
 * MainActivity (addc50501) with the fetch React Native host's semantics (labs/platforms/react-native MainActivity.kt).
 *
 * Scenario args (intent extras, all optional): configB64, hooksB64, eventsB64, scenario, nativeLabel, view,
 * stopOnTerminate, finishNow (a second `am start`), restartOnFinish (WO-007: finishNow restarts the app instead), and
 * the fetch knobs noConfigure, finishDelayMs, withholdFinish, finishOnTimeout (lab.yaml notes).  Extras of any other
 * name are tolerated.
 *
 * A recreation builds a new Bridge, new plugin instances and a new WebView: the page reloads and the driver boots
 * again.  BridgeActivity.onDestroy runs super.onDestroy() and THEN bridge.onDestroy() (every plugin's
 * handleOnDestroy()), so onDestroy:afterSuper below is after both.
 */
public class MainActivity extends BridgeActivity {
    static final String TAG = "LAB";
    static int sCreates = 0;
    static int sJsBoots = 0;
    static WeakReference<Activity> sCurrent;

    /** The launch intent, stashed in onCreate so Lab.args() never depends on getActivity(): after a recreation the
     *  reloaded page asks for its args from a plugin instance of the new Bridge. */
    static Intent sIntent;

    static final String PKG = "com.transistorsoft.tsbackgroundfetch.";
    private static final String KNOBS = "lab-knobs";
    static final int DEFAULT_FINISH_DELAY_MS = 1000;

    private final Handler handler = new Handler(Looper.getMainLooper());

    // --- launch extras: read whatever type `am start` sent them as (--ez / --ei / --es) -------------------------

    static String decode(Intent intent, String extra, String fallback) {
        String b64 = (intent == null) ? null : intent.getStringExtra(extra);
        return (b64 == null) ? fallback : new String(android.util.Base64.decode(b64, android.util.Base64.DEFAULT));
    }

    static String string(Intent intent, String key) {
        String s = (intent == null) ? null : intent.getStringExtra(key);
        return (s == null) ? "" : s;
    }

    @SuppressWarnings("deprecation")
    static boolean bool(Intent intent, String key, boolean fallback) {
        Object v = (intent == null || intent.getExtras() == null) ? null : intent.getExtras().get(key);
        if (v instanceof Boolean) return (Boolean) v;
        if (v instanceof Number) return ((Number) v).intValue() != 0;
        if (v instanceof String) return "true".equalsIgnoreCase((String) v) || "1".equals(v);
        return fallback;
    }

    @SuppressWarnings("deprecation")
    static int integer(Intent intent, String key, int fallback) {
        Object v = (intent == null || intent.getExtras() == null) ? null : intent.getExtras().get(key);
        if (v instanceof Number) return ((Number) v).intValue();
        if (v instanceof String) {
            try { return Integer.parseInt(((String) v).trim()); } catch (NumberFormatException e) { return fallback; }
        }
        return fallback;
    }

    static String id(Object o) {
        return (o == null) ? "null" : o.getClass().getSimpleName() + "@" + Integer.toHexString(System.identityHashCode(o));
    }

    // --- the fetch knobs: launch args the HEADLESS task needs too, so persisted ----------------------------------

    /** The launch's finish knobs, written where the headless task (no intent) can read them: the LAST launch's. */
    static void saveKnobs(Context context, Intent intent) {
        context.getApplicationContext().getSharedPreferences(KNOBS, Context.MODE_PRIVATE).edit()
                .putInt("finishDelayMs", integer(intent, "finishDelayMs", DEFAULT_FINISH_DELAY_MS))
                .putBoolean("withholdFinish", bool(intent, "withholdFinish", false))
                .putBoolean("finishOnTimeout", bool(intent, "finishOnTimeout", true))
                .commit();   // commit, not apply: a kill right after the launch must not lose them
    }

    /** {finishDelayMs, withholdFinish, finishOnTimeout} as the last launch persisted them. */
    static Object[] knobs(Context context) {
        SharedPreferences p = context.getApplicationContext().getSharedPreferences(KNOBS, Context.MODE_PRIVATE);
        return new Object[]{p.getInt("finishDelayMs", DEFAULT_FINISH_DELAY_MS), p.getBoolean("withholdFinish", false),
                p.getBoolean("finishOnTimeout", true)};
    }

    // --- the probe (React Native's): reflective, never building an SDK singleton ----------------------------------

    /** A class's own singleton, read from its static field BY TYPE — never by calling getInstance(), which would build
     *  it and change what is being observed.  null: the class is absent; {cls, null}: no instance exists yet. */
    private static Object[] singleton(String className) {
        try {
            Class<?> cls = Class.forName(className);
            for (Field f : cls.getDeclaredFields()) {
                if (Modifier.isStatic(f.getModifiers()) && f.getType() == cls) {
                    f.setAccessible(true);
                    return new Object[]{cls, f.get(null)};
                }
            }
            return new Object[]{cls, null};
        } catch (ClassNotFoundException e) {
            return null;
        } catch (Throwable t) {
            return null;
        }
    }

    private static String call(Object[] pair, String method) {
        if (pair == null || pair[1] == null) return "-";
        try {
            return String.valueOf(((Class<?>) pair[0]).getMethod(method).invoke(pair[1]));
        } catch (Throwable t) {
            return "err";
        }
    }

    /** Native state no log line carries: the SDK's headless and background flags from LifecycleManager, whether
     *  BackgroundFetch has been built, and the task ids it has persisted.  Reflective, so the host compiles against no
     *  SDK API and a renamed member reads `err` instead of breaking the build. */
    static String probe(Context context) {
        try {
            Object[] lm = singleton(PKG + "LifecycleManager");
            Object[] adapter = singleton(PKG + "BackgroundFetch");
            Set<String> set = context.getApplicationContext().getSharedPreferences("TSBackgroundFetch", Context.MODE_PRIVATE)
                    .getStringSet("tasks", null);
            String tasks = "-";
            if (set != null && !set.isEmpty()) {
                List<String> sorted = new ArrayList<>(set);
                Collections.sort(sorted);
                tasks = String.join(",", sorted);
            }
            Activity current = (sCurrent != null) ? sCurrent.get() : null;
            return "headless=" + call(lm, "isHeadless") + " background=" + call(lm, "isBackground")
                    + " adapter=" + (adapter != null && adapter[1] != null) + " tasks=" + tasks + " current=" + id(current);
        } catch (Throwable t) {
            return "probeError=" + t.getClass().getSimpleName();
        }
    }

    // --- the Activity --------------------------------------------------------------------------------------------

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        sCreates++;
        sIntent = getIntent();
        saveKnobs(this, getIntent());
        registerPlugin(LabPlugin.class);   // before super.onCreate(), which builds the Bridge and loads every plugin
        Log.i(TAG, "point=onCreate activity=" + id(this) + " pid=" + Process.myPid() + " creates=" + sCreates
                + " recreated=" + (savedInstanceState != null) + " " + probe(this));
        super.onCreate(savedInstanceState);
        // The new Bridge has run every plugin's load() by now.
        Log.i(TAG, "point=onCreate:afterSuper activity=" + id(this) + " " + probe(this));
    }

    @Override
    public void onResume() {
        super.onResume();
        sCurrent = new WeakReference<>(this);
        handler.postDelayed(() -> Log.i(TAG, "point=resumed+2s activity=" + id(this) + " " + probe(this)), 2000);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        if (bool(intent, "finishNow", false)) {
            // (WO-007) restartOnFinish rides on this Activity's OWN launch: the trigger's `am start` carries finishNow
            // alone.  The app restarts itself the way apps do: finish() FIRST (singleTask would otherwise hand the
            // relaunch to this live instance's onNewIntent), then startActivity() of a copy of the launch, so the new
            // Activity boots with the same args.  Synchronous: onDestroy clears this Activity's handler.  The new one
            // resumes before this one is destroyed (restartOrder reads it).
            boolean restart = bool(getIntent(), "restartOnFinish", false);
            Log.i(TAG, "point=finishNow activity=" + id(this) + " restart=" + restart);
            finish();
            if (restart) {
                Intent relaunch = new Intent(this, MainActivity.class).putExtras(getIntent());
                relaunch.removeExtra("finishNow");   // BridgeActivity.load() replays getIntent() into onNewIntent
                startActivity(relaunch);
            }
            return;
        }
        super.onNewIntent(intent);
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        Log.i(TAG, "point=onDestroy activity=" + id(this) + " isChangingConfigurations=" + isChangingConfigurations()
                + " isFinishing=" + isFinishing() + " " + probe(this));
        super.onDestroy();
        Log.i(TAG, "point=onDestroy:afterSuper activity=" + id(this) + " " + probe(this));
    }
}
