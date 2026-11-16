package com.transistorsoft.fetchlab.cordova;

import android.content.Intent;
import android.util.Log;

import org.apache.cordova.CallbackContext;
import org.apache.cordova.CordovaPlugin;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

/** (WO-002) JavaScript side of the lab: scenario args, a boot counter, the native probe, the host's verdict, the
 *  persisted finish knobs, and the driver's log line (the Capacitor fetch host's LabPlugin, as a Cordova plugin named
 *  `Lab`; its log action is bg-forge's Cordova one). */
public class LabPlugin extends CordovaPlugin {
    @Override
    public boolean execute(String action, JSONArray args, CallbackContext callbackContext) throws JSONException {
        switch (action) {
            case "args": {
                // Only what the driver honours: the boot line echoes this map, so it is the record of which extras this
                // app implements.  The intent MainActivity.onCreate stashed, never cordova.getActivity()'s.
                Intent intent = MainActivity.sIntent;
                JSONObject result = new JSONObject();
                result.put("stopOnTerminate", MainActivity.bool(intent, "stopOnTerminate", true));
                result.put("noConfigure", MainActivity.bool(intent, "noConfigure", false));
                result.put("finishDelayMs", MainActivity.integer(intent, "finishDelayMs", MainActivity.DEFAULT_FINISH_DELAY_MS));
                result.put("withholdFinish", MainActivity.bool(intent, "withholdFinish", false));
                result.put("finishOnTimeout", MainActivity.bool(intent, "finishOnTimeout", true));
                result.put("restartOnFinish", MainActivity.bool(intent, "restartOnFinish", false));   // (WO-007)
                result.put("scenario", MainActivity.string(intent, "scenario"));
                result.put("nativeLabel", MainActivity.string(intent, "nativeLabel"));
                result.put("config", MainActivity.decode(intent, "configB64", "{}"));
                result.put("hooks", MainActivity.decode(intent, "hooksB64", "[]"));
                result.put("events", MainActivity.decode(intent, "eventsB64", "[]"));
                result.put("view", MainActivity.string(intent, "view"));
                // Counted here, not in onCreate: a boot is a page that asked for its args.
                result.put("boot", ++MainActivity.sJsBoots);
                callbackContext.success(result);
                return true;
            }
            case "knobs": {
                // The finish knobs as the LAST launch persisted them (MainActivity.saveKnobs).
                Object[] k = MainActivity.knobs(cordova.getContext());
                JSONObject result = new JSONObject();
                result.put("finishDelayMs", (int) (Integer) k[0]);
                result.put("withholdFinish", (boolean) (Boolean) k[1]);
                result.put("finishOnTimeout", (boolean) (Boolean) k[2]);
                callbackContext.success(result);
                return true;
            }
            case "probe": {
                JSONObject result = new JSONObject();
                result.put("probe", MainActivity.probe(cordova.getContext()));
                callbackContext.success(result);
                return true;
            }
            case "verdict": {
                // The host's verdict for the device view: the file run.py pushes into this app's files dir
                // (backends.Adb.push_verdict), or "" before the first push.  Read here, never judged.  A plain stream:
                // java.nio.file.Files is API 26 and this app's minSdk is 24.
                java.io.File f = new java.io.File(cordova.getContext().getFilesDir(), "lab-verdict.json");
                java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
                if (f.exists()) {
                    try (java.io.FileInputStream in = new java.io.FileInputStream(f)) {
                        byte[] buf = new byte[4096];
                        for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
                    } catch (java.io.IOException e) {
                        out.reset();
                    }
                }
                JSONObject result = new JSONObject();
                result.put("value", new String(out.toByteArray(), java.nio.charset.StandardCharsets.UTF_8));
                callbackContext.success(result);
                return true;
            }
            case "log":
                // A driver line, VERBATIM under the app's own tag (the harness reads a LAB-tagged line as "LAB " + it):
                // Cordova 15 does not intercept the WebView console, whose own format would cut the lines' last field.
                // No reply: lab.js passes no callbacks for a line.
                Log.i(MainActivity.TAG, args.getString(0));
                return true;
        }
        return false;
    }
}
