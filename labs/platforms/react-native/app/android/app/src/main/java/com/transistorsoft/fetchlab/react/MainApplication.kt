package com.transistorsoft.fetchlab.react

import android.app.Application
import android.os.Build
import android.os.Process
import android.util.Log
import com.facebook.react.PackageList
import com.facebook.react.ReactApplication
import com.facebook.react.ReactHost
import com.facebook.react.ReactNativeApplicationEntryPoint.loadReactNative
import com.facebook.react.defaults.DefaultReactHost.getDefaultReactHost

class MainApplication : Application(), ReactApplication {

  override val reactHost: ReactHost by lazy {
    getDefaultReactHost(
      context = applicationContext,
      packageList =
        PackageList(this).packages.apply {
          add(LabPackage())        // the lab's args/probe/verdict/knobs channel (WO-002)
        },
      // A debug build with a pre-made `--dev false` bundle (build.sh): never look for a Metro server.
      useDevSupport = false,
    )
  }

  override fun onCreate() {
    super.onCreate()
    // (WO-002) Every process start, Activity or not: a headless launch by JobScheduler logs this and no onCreate.
    Log.i(MainActivity.TAG, "point=appCreate pid=${Process.myPid()} process=${if (Build.VERSION.SDK_INT >= 28) Application.getProcessName() else "-"} " +
      MainActivity.probe(this))
    loadReactNative(this)
  }
}
