package com.transistorsoft.fetchlab.flutter

import android.app.Application
import android.os.Build
import android.os.Process
import android.util.Log
import com.transistorsoft.fetchlab.labprobe.LabProbePlugin

/**
 * (WO-002) Every process of the app logs `point=appCreate` — the foreground one and a process JobScheduler starts for
 * a headless fetch — as the Capacitor and React Native hosts do: appProcesses and the own-pid filter read it.
 */
class LabApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        val process = if (Build.VERSION.SDK_INT >= 28) Application.getProcessName() else "-"
        Log.i(LabProbePlugin.TAG, "point=appCreate pid=${Process.myPid()} process=$process ${LabProbePlugin.probe(this)}")
    }
}
