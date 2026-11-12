package com.transistorsoft.fetchlab.capacitor;

import android.app.Application;
import android.os.Build;
import android.os.Process;
import android.util.Log;

/**
 * (WO-002) Every process start, Activity or not: a headless launch by JobScheduler logs this line and no onCreate.
 * React Native's MainApplication.onCreate line (labs/platforms/react-native), which own-pid scoping and the
 * appProcesses metric read.
 */
public class LabApplication extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        String process = (Build.VERSION.SDK_INT >= 28) ? Application.getProcessName() : "-";
        Log.i(MainActivity.TAG, "point=appCreate pid=" + Process.myPid() + " process=" + process + " "
                + MainActivity.probe(this));
    }
}
