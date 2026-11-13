package com.transistorsoft.fetchlab.capacitor;

import android.content.Intent;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/** JavaScript side of the lab: scenario args, a boot counter, the native probe, the host's verdict, and the persisted
 *  finish knobs (React Native's LabModule, as a Capacitor plugin named `Lab`). */
@CapacitorPlugin(name = "Lab")
public class LabPlugin extends Plugin {

    /** The launch's args.  Only what the driver honours is put here: the boot line echoes this map, so it is the record
     *  of which extras this app implements.  The intent MainActivity.onCreate stashed, never getActivity()'s. */
    @PluginMethod
    public void args(PluginCall call) {
        Intent intent = MainActivity.sIntent;
        JSObject result = new JSObject();
        result.put("stopOnTerminate", MainActivity.bool(intent, "stopOnTerminate", true));
        result.put("noConfigure", MainActivity.bool(intent, "noConfigure", false));
        result.put("finishDelayMs", MainActivity.integer(intent, "finishDelayMs", MainActivity.DEFAULT_FINISH_DELAY_MS));
        result.put("withholdFinish", MainActivity.bool(intent, "withholdFinish", false));
        result.put("finishOnTimeout", MainActivity.bool(intent, "finishOnTimeout", true));
        result.put("restartOnFinish", MainActivity.bool(intent, "restartOnFinish", false));   // (WO-007) MainActivity
        result.put("scenario", MainActivity.string(intent, "scenario"));
        result.put("nativeLabel", MainActivity.string(intent, "nativeLabel"));
        result.put("config", MainActivity.decode(intent, "configB64", "{}"));
        result.put("hooks", MainActivity.decode(intent, "hooksB64", "[]"));
        result.put("events", MainActivity.decode(intent, "eventsB64", "[]"));
        result.put("view", MainActivity.string(intent, "view"));
        result.put("boot", ++MainActivity.sJsBoots);
        call.resolve(result);
    }

    @PluginMethod
    public void probe(PluginCall call) {
        JSObject result = new JSObject();
        result.put("probe", MainActivity.probe(getContext()));
        call.resolve(result);
    }

    /** (WO-002) The finish knobs as the LAST launch persisted them (MainActivity.saveKnobs). */
    @PluginMethod
    public void knobs(PluginCall call) {
        Object[] k = MainActivity.knobs(getContext());
        JSObject result = new JSObject();
        result.put("finishDelayMs", (Integer) k[0]);
        result.put("withholdFinish", (Boolean) k[1]);
        result.put("finishOnTimeout", (Boolean) k[2]);
        call.resolve(result);
    }

    /** The host's verdict for the device view: the file run.py pushes into this app's files dir
     *  (backends.Adb.push_verdict), or "" before the first push.  Read here, never judged.  A plain stream:
     *  java.nio.file.Files is API 26 and this app's minSdk is 24. */
    @PluginMethod
    public void verdict(PluginCall call) {
        java.io.File f = new java.io.File(getContext().getFilesDir(), "lab-verdict.json");
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream();
        if (f.exists()) {
            try (java.io.FileInputStream in = new java.io.FileInputStream(f)) {
                byte[] buf = new byte[4096];
                for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
            } catch (java.io.IOException e) {
                out.reset();
            }
        }
        JSObject result = new JSObject();
        result.put("value", new String(out.toByteArray(), java.nio.charset.StandardCharsets.UTF_8));
        call.resolve(result);
    }
}
