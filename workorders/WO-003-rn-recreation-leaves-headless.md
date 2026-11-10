---
id: WO-003
title: A configuration-change recreation of the React Native Activity leaves TSBackgroundFetch headless, so the next fetch with the app on screen is stopped (stopOnTerminate true) or stranded (false)
status: done
created: 2026-11-09
platforms: [android]
submodules: [react-native/background-fetch, cordova/background-fetch]
branch: fix/WO-003-recreation-leaves-headless
issues: []
supersedes: []
---

> **Found by** WO-002's device lab: suspected defect 10, which the scenario `recreate-foreground-android` was written to measure (PREDICTED RED) and did.
>
> **Evidence markers:** **MEASURED** = seen in a lab capture; **PROVEN** = read in source; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at.

## Diagnosis

**MEASURED** (`labs/captures/react-native/recreate-foreground-android/FIX/run`, Lab1 `emulator-5580`, Android 16 / API 36, 2026-11-09 11:42; native `4.1.1-master-5de5242`, react-native plugin `a3c6996`, both clean). With `stopOnTerminate: true`, the app on screen the whole time:

| Time | Line |
|---|---|
| 43.333 | `☯️ onCreate`, `onStart`, `onResume` (the launch) |
| 53.409 | `point=trigger bold`: the system bold-text setting. The lab app's `configChanges` omit `fontWeightAdjustment`, so Android recreates the Activity. |
| 53.448 | `point=onDestroy … isChangingConfigurations=true isFinishing=false headless=false` |
| 53.449 | `☯️  HeadlessMode? true` |
| 53.453 | `point=onCreate … recreated=true headless=true` |
| 55.458 | `point=resumed+2s … headless=true`; no `☯️ onStart` or `onResume` after the trigger |
| 57.822 | `- Background Fetch event received`, then `- Stopping on terminate`, `- stop`, `- jobFinished`, `- cancel taskId=react-native-background-fetch, jobId=999` |

The scored result: `driverEvents -`, `fetchFinished 0`, `stoppingOnTerminate 1`, `jobsCancelled 999`, `headless true`, `jobIds -`, `configure@0=never-fired` → FAIL. So the app's `configure()` callback never sees the fetch, and the periodic job is gone for good. A fetch never fires again until the app calls `configure()` again.

**The code path (PROVEN, source):**
- **React Native calls the plugin's `onHostDestroy` on a recreation.** `RNBackgroundFetchModule.java:113-117` (react-native plugin `master` `1532161`; `android/` unchanged since `cd4fc41`) calls `LifecycleManager.getInstance().setHeadless(true)` with no check of why the Activity is going away. React Native's own path is `ReactActivityDelegate.onDestroy` → `ReactHostImpl.onHostDestroy` → `ReactContext.onHostDestroy`, with no `isChangingConfigurations` check (from the scenario's description; INFERRED from RN 0.88's source, not re-read here).
- **Nothing sets it back.**
  - The plugin's `onHostResume` (`:98-103`) only calls `initializeBackgroundFetch()` (`:169-175`), which sets a local `initialized` flag and does nothing else.
  - `LifecycleManager` (native at the pin `5de5242`, `LifecycleManager.java`) clears `mIsHeadless` only in its `ProcessLifecycleOwner` `onStart` (`:142-159`) and `onResume` (`:185-194`). `ProcessLifecycleOwner` does not dispatch either when a new Activity resumes within 700 ms of the old one pausing, which is what a recreation does (MEASURED: no `☯️` line after the trigger).
- **The next delivery takes the headless branch.**
  - `BackgroundFetch.doFetch` (`BackgroundFetch.java:272-285`): not headless → the bridge's callback; headless with `stopOnTerminate: true` → `- Stopping on terminate` and `stop(taskId)` (MEASURED above); headless with `false` → `fireHeadlessEvent`.
  - With `stopOnTerminate: false` the RN `HeadlessTask` returns at once because the ReactContext is RESUMED (`HeadlessTask.java:67-70`), so neither JS callback runs and the BGTask is not finished until the SDK's 60 s timeout. INFERRED; not measured. The scenario has no `false` step yet.
  - A timeout takes the same branch: `BGTask.onTimeout` (`BGTask.java:207-222`) sends it to the headless path instead of the bridge's `onTimeout`. INFERRED.
- **How long it lasts (INFERRED):** until something resets the flag. `ProcessLifecycleOwner` sends `ON_START` the next time the whole app comes back from the background (after more than 700 ms), so a user who leaves the app and returns clears it. A user who stays in the app does not.

**What causes a recreation** is any configuration change missing from the Activity's `android:configChanges`. The React Native template lists `keyboard|keyboardHidden|orientation|screenLayout|screenSize|smallestScreenSize|uiMode`, so font scale, bold text, locale and density changes recreate it. PROVEN for the lab app, which keeps the template's list (`labs/platforms/react-native/app/android/app/src/main/AndroidManifest.xml:21`). INFERRED for customers' apps, which start from that template.

## Proposed fix

**In the React Native plugin (`RNBackgroundFetchModule.java`):**
1. **`onHostDestroy`**: when the current Activity is changing configuration (`getCurrentActivity() != null && getCurrentActivity().isChangingConfigurations()`), leave the headless flag alone. A real destruction (finish, Back on older Android, process teardown) still sets it. INFERRED: in `onHostDestroy`, `getCurrentActivity()` is still the old Activity. The fix's lab run proves or refutes this. If it is `null` there, change 2 alone still closes the defect, and the window below stays open.
2. **`onHostResume`**: set `LifecycleManager.getInstance().setHeadless(false)`. An Activity has resumed, so the app is not headless. This is what the Flutter plugin already does when an Activity attaches (`BackgroundFetchModule.java:102-103`). It also covers any other route that leaves the flag set while an Activity is on screen.

Change 2 alone would leave a window of about 5–10 ms between the old Activity's destroy and the new one's resume. A fetch landing in that window would still be stopped. Change 1 closes it.

**Cordova (DECISION D2, below):** `CDVBackgroundFetch.java:151-155` has the same shape: `onDestroy` sets the flag and nothing resets it. The matching fix is to skip it in `onDestroy` when `cordova.getActivity().isChangingConfigurations()`, and to override `onResume(boolean)` to clear it.

**DECISION D1: where the fix lives.**
- **Recommended: in each bridge** (React Native here, Cordova per D2). The bridges own the decision to call `setHeadless`. The native library cannot tell a recreation from a destruction without Activity callbacks it doesn't register today. A bridge fix also ships without a native release.
- **Alternative: in native `LifecycleManager`.** Register `Application.ActivityLifecycleCallbacks`, clear the flag on any Activity resume, and ignore `setHeadless(true)` while an Activity is changing configuration. One change would fix every bridge. But it changes the native library's headless model for every consumer (including background-geolocation's `TSLocationManager`, which links it), and it needs a native Android release. That is a bigger change than this defect warrants.

**DECISION D2: Cordova in this work order or its own.**
- **Recommended: in this work order,** because the code shape is the same and the fix is the same three lines. Its verification is manual (below) until WO-002 has a Cordova lab host.
- **Alternative:** a separate work order once WO-002's Cordova host has measured it.

## Scope check

- **`setHeadless(true)` in `onHostDestroy` must keep working for a real destruction.** Two green WO-002 scenarios pin that, both MEASURED 2026-11-09 on Lab1:
  - `stop-on-terminate` step `finish`: `finishNow` destroys the Activity (`isFinishing=true`), then the fetch is stopped (`stoppingOnTerminate 1`, `jobsCancelled 999`).
  - `headless-activity-destroyed`: the Activity is destroyed with `stopOnTerminate: false`, and the fetch goes to the RN headless task (`headlessEvents.fetch 1`, `rnHeadlessStarts 1`).
  Neither has `isChangingConfigurations=true`, so change 1 leaves both paths as they are.
- **`setHeadless(false)` in `onHostResume`:**
  - It fires listeners (`LifecycleManager.java:91-95` → `fireHeadlessChangeListeners`). `onHeadlessChange` callbacks registered by a consumer would see a `false`. NOT CHECKED: who registers one. Native's own `BackgroundFetch` doesn't (grep at the pin: only `LifecycleManager.java` calls it).
  - A headless JS context (no Activity) never gets `onHostResume`, so a headless delivery is not affected. INFERRED from RN's lifecycle.
  - A warm process launched into the foreground after a headless fetch gets `onHostResume` → `false`, which is correct.
- **The timeout path (`BGTask.java:207`) reads the same flag,** so it is fixed by the same change. No separate change is needed.
- **No public API changes.** No change to `index.js`, the types, or the native library.

## Platform parity

- **iOS:** N/A. iOS has no headless mode or headless flag. `stopOnTerminate` is a no-op property there (`TSBackgroundFetch.m:388-393`, per WO-002's Diagnosis), and the iOS SDK has no `LifecycleManager`.
- **Android, React Native:** affected. MEASURED above.
- **Android, Cordova:** the same shape, PROVEN in source: `CDVBackgroundFetch.java:151-155` `onDestroy` → `setHeadless(true)`, and the class overrides no `onResume`/`onStart`. Whether Cordova destroys its plugins on a recreation is INFERRED: `CordovaActivity.onDestroy` → `appView.handleDestroy()` → `pluginManager.onDestroy()`. NOT CHECKED: which config changes the cordova-android template's `configChanges` omits. NOT MEASURED.
- **Android, Flutter:** not affected by this path, PROVEN in source.
  - On a recreation Flutter calls `onDetachedFromActivityForConfigChanges` / `onReattachedToActivityForConfigChanges`, and both are empty (`BackgroundFetchPlugin.java:35-46`). Only `onDetachedFromActivity` (`:48-50`) clears the Activity and sets the flag.
  - Attaching an Activity sets `false` (`BackgroundFetchModule.java:102-103`).
  - NOT MEASURED.
- **Android, Capacitor:** not affected by this path, PROVEN in source: the plugin never calls `setHeadless` (grep of `capacitor/background-fetch/android/src`). Its opposite gap, never going headless when the Activity is destroyed, is WO-002's suspected defect 4.
- **Native Android (TSBackgroundFetch used directly):** apps that call `setHeadless` themselves are on their own. The native example is NOT CHECKED.

## Test plan

- **The lab measurement that found it:** `recreate-foreground-android` (WO-002) turns green on the fix branch, React Native on Android, both Lab AVDs. Build it with the plugin checkout on `fix/WO-003-recreation-leaves-headless` (the RN lab stages whatever checkout `lab.yaml`'s `default_checkout` names).
- **New step, carrying the anchor:** add a `stop-on-terminate-false` step to `recreate-foreground-android` with `stopOnTerminate: false`, `enableHeadless: true`. On the fix it expects the driver's delivery (`driverEvents fetch=1`, `headlessEvents.fetch 0`, `rnHeadlessStarts 0`). Today it should show the stranded BGTask (PREDICTED: no driver event, `[BGTask] timeout` at +60 s). Its description names WO-003.
- **The regression guard:** the full RN Android Tier 1 sweep stays green, especially `stop-on-terminate` (both steps), `headless-activity-destroyed` and `headless-kill`.
- **Cordova (if D2 = this work order):** manual, on the Cordova example app on a Lab AVD. Configure with `stopOnTerminate: true`, then set bold text on (`adb shell settings put secure font_weight_adjustment 300`). Check `☯️  HeadlessMode? true` before the fix and its absence after, then `adb shell cmd jobscheduler run -f <pkg> 999` 4 s later: the JS callback runs and nothing is stopped.
- **Changelog:** a `## Unreleased` entry in each fixed plugin, `(WO-003)`.

## Out of scope

- WO-002's suspected defect 4 (Capacitor never going headless).
- Changing native `LifecycleManager` (D1's alternative) unless Chris picks it.
- Adding entries to the lab app's or the example apps' `configChanges`.
- RN's `HeadlessTask` RESUMED early return (`HeadlessTask.java:67-70`). It is correct when the app really is on screen; the bug is the flag that sent the fetch there.

## Not an acceptable shortcut

- **Removing `setHeadless(true)` from `onHostDestroy`.** A finished Activity must still make the next fetch headless or stop it. `stop-on-terminate`'s `finish` step and `headless-activity-destroyed` pin that, and both go red without it.
- **Adding `fontWeightAdjustment|fontScale|…` to the Activity's `configChanges`,** in the lab app, the example, or as advice to customers. It hides the lab's measurement, and every app with a recreation it didn't list still hits the defect.
- **Relying on `ProcessLifecycleOwner` to reset the flag.** It does not dispatch for a recreation within 700 ms (MEASURED: no `☯️` line after the trigger). That gap is why the defect exists.
- **Making RN's `HeadlessTask` start a task while RESUMED.** That would run the headless JS task with the app on screen, and the foreground `configure()` callback would still miss the event.
- **Fixing it in the lab** (the driver or probe resetting `LifecycleManager`). The lab measures the shipped plugin and must not compensate for it.

## Verification

- [x] `recreate-foreground-android` green on the fix branch, React Native on Android, Lab1 and Lab2 (2026-11-10, the fix not yet committed: `plugin=1532161+dirty`)
- [x] Its `stop-on-terminate-false` step (WO-003) red before the fix and green after (Lab1 red, Lab1 and Lab2 green)
- [x] React Native Android Tier 1 sweep green on the fix branch (no regression in `stop-on-terminate`, `headless-activity-destroyed`, `headless-kill`): 12/12, Lab1, 2026-11-10
- [x] Cordova: manual bold-text recreation on its example app, before and after (if D2 keeps it here): Lab1, 2026-11-10
- [x] Changelog entries carry `(WO-003)`

## Log

- 2026-11-09 — created from WO-002's first React Native Android board.
  - WO-002 listed this as suspected defect 10, INFERRED from code.
  - `recreate-foreground-android` measured it red on Lab1, exactly as predicted.
  - Not reproduced yet: Lab2, the `stopOnTerminate: false` path, and Cordova.
- 2026-11-10 — **approved** by Chris ("approve WO-003"), with the recommended decisions.
  - **D1:** the fix goes in each bridge, not in native `LifecycleManager`.
  - **D2:** Cordova is in this work order, verified by hand until WO-002 has a Cordova host.
  - Not started: no branch, no code.
- 2026-11-10 — **in-progress**, at Chris's request ("Do this task here"). Branch `fix/WO-003-recreation-leaves-headless` in `react-native/background-fetch` (from `master` `1532161`) and `cordova/background-fetch` (from `master` `3e77e80`).
  - **Diagnosis re-verified against current source:** the citations hold (`RNBackgroundFetchModule.java:98-117,169-175`; `CDVBackgroundFetch.java:151-155`, which overrides no `onResume`).
  - **The INFERRED step is now PROVEN, for React Native 0.88.0** (`labs/platforms/react-native/app/node_modules/react-native/ReactAndroid`): in `onHostDestroy`, `getCurrentActivity()` is still the old Activity.
    - `ReactContext.onHostDestroy()` runs the listeners (`onHostDestroyImpl`, `ReactContext.java:353-360`) before it clears `mCurrentActivity` (`:338-341`).
    - `BridgelessReactContext` does not override `getCurrentActivity()`.
    - `ReactHostImpl.moveToHostDestroy` clears its own `currentActivity` only after `moveToOnHostDestroy` (`ReactHostImpl.kt:891-895`).
    - So change 1 can read `isChangingConfigurations()` there.
  - **`setHeadless` side effects** (native `LifecycleManager.java:86-96,128-139,206-216` at `5de5242`): it fires the headless-change listeners only while the 50 ms startup event is pending, and they are one-shot. So `setHeadless(false)` on each `onHostResume` either only sets the flag, or settles the startup listeners with `false`. The only listener in native is its own log line (`:46-50`).
- 2026-11-10 — **The new step, red before the fix (MEASURED).** `recreate-foreground-android` gains `stop-on-terminate-false` (`stopOnTerminate: false`, `enableHeadless: true`; its comment and the scenario's description carry `(WO-003)`). On Lab1 with the unfixed plugin (`fix/WO-003…` at `1532161`, nothing changed yet), both steps were red.
  - `run`: as 2026-11-09.
  - `stop-on-terminate-false`:
    - **The lines:** `onDestroy … isChangingConfigurations=true`, then `☯️  HeadlessMode? true`, then the new Activity's probe `headless=true`. The fetch 4 s later logs `- Background Fetch event received` and nothing after it: no headless JS task, no `finish`, no `jobFinished`. JobScheduler still held 999.
    - **The score:** `driverEvents -`, `fetchFinished 0`, `jobFinished 0`, `headless true`, `configure@0=never-fired`. The PREDICTED stranded BGTask is MEASURED. The step's `after: 30` ends before the SDK's 60 s timeout, so that line was not reached.
    - The captures are kept outside the repo (session scratchpad); the fixed run overwrote their capture paths.
- 2026-11-10 — **Cordova before the fix (MEASURED,** Lab1, the example app copied out of the checkout so its `platforms/` and `plugins/` never land in the submodule; cordova-android 15).
  - **The Test plan, adjusted:** it said to configure `stopOnTerminate: true`. The example configures `false` with `enableHeadless: true`, and it was run as it is: the flag sends the fetch to the headless path either way.
  - **The recreation:** cordova-android's generated manifest lists `orientation|keyboardHidden|keyboard|screenSize|locale|smallestScreenSize|screenLayout|uiMode|navigation`, without `fontScale` or `fontWeightAdjustment`, so bold text recreates the Activity (this was NOT CHECKED; it is now PROVEN from the manifest). Bold text gave `☯️  HeadlessMode? true`, then the new WebView's `configure()` (`Re-configured existing task`).
  - **The fetch:** `run -f … 999` 4 s later gave `- Background Fetch event received`, then `Headless task error: com.transistorsoft.cordova.backgroundfetch.BackgroundFetchHeadlessTask` (the example has no headless class). The JS `EVENT:` callback never ran.
- 2026-11-10 — **The fix** (uncommitted, awaiting Chris's diff review).
  - **React Native, `RNBackgroundFetchModule.java`:**
    - `onHostResume` calls `setHeadless(false)`;
    - `onHostDestroy` sets it `true` only when `getCurrentActivity()` is null or not `isChangingConfigurations()`.
  - **Cordova, `CDVBackgroundFetch.java`:**
    - `onDestroy`, the same check on `cordova.getActivity()`;
    - a new `onResume(boolean)` that calls `setHeadless(false)`.
  - **Changelogs:** both plugins' `## Unreleased` gain an `[Android]` entry with `(WO-003)`.
  - **Found while writing the Cordova half (PROVEN, source):**
    - **`onResume` does not run on the recreation itself.** The plugin's `<feature>` has no `onload` param, so Cordova creates the plugin on its first `exec` (the new WebView's `configure()`). `PluginManager.onResume` (cordova-android 15, `PluginManager.java:292-300`) reaches only plugins that already exist, and the new Activity resumes before its new plugin does. So for Cordova the `onDestroy` check alone closes the defect. `onResume` covers a resume with the plugin already loaded.
    - **Recorded, not changed:** making the plugin `onload`, or clearing the flag in `pluginInitialize`, is not in this work order.
- 2026-11-10 — **After the fix (MEASURED).**
  - **React Native, Lab1** (the app rebuilt from the fixed checkout; `run.json` records the plugin as dirty: the fix is not committed): **both steps PASS.**
    - Each logs `onDestroy … isChangingConfigurations=true` and no `☯️  HeadlessMode? true`. The new Activity's probe reads `headless=false`.
    - The fetch 4 s later goes to the driver (`app=event=fetch n=1`, `event id=configure@0 n=1`), then `finish … after_ms=1000 outcome=ok`, `- finish:` and `- jobFinished`.
    - `lifecycleTally` is `onCreate=1,onResume=1,onStart=1`: no `headless:true`.
  - **Cordova, Lab1:**
    - The recreation logs no `☯️  HeadlessMode? true`, either when bold text went on or when it was restored, and the new WebView configured again.
    - The fetch reached the JS callback (`EVENT: cordova-background-fetch`), then `[CDVBackgroundFetch] finish()`, `- finish:` and `- jobFinished` 2.0 s later (the example's own delay).
    - The demo was then uninstalled from Lab1, and bold text is back at 0.
  - **Lab2, React Native:** both steps PASS (92 s).
  - **The regression guard: the React Native Android Tier 1 sweep, 12 of 12 PASS** (15 steps, Lab1, 696 s, every `RESULT` reading `plugin=1532161+dirty`).
    - It includes `stop-on-terminate`'s `finish` step and `headless-activity-destroyed`. Those pin that a real destruction still makes the app headless: change 1 left that path as it was.
    - Its full-window set and the unfixed run's captures are in `labs/baselines/2026-11-10-wo003-fix-react-native-android-lab1` (this worktree, gitignored).
    - The tracked captures were restored to the 2026-11-09 board: these runs' `run.json` record a dirty plugin.
    - Once the fix is committed in the submodule, `recreate-foreground-android` is re-run on Lab1, and its two steps' `summary.txt` + `run.json` are committed with the plugin's real commit.
  - **Verification:** every box is ticked, all measured on the fix before its commit. Status stays `in-progress` until the commits.
- 2026-11-10 — **Committed** after Chris's diff review ("Yes, commit all seven"). Status `verify` until the re-run on the committed fix.
  - `react-native/background-fetch` `cffcbb1` and `cordova/background-fetch` `8017279`, each on `fix/WO-003-recreation-leaves-headless`, not merged.
  - In the parent repo, branch `fix/WO-003-recreation-leaves-headless` (from `master` `273af07`): the scenario's new step and this Log. The submodule pointers are not bumped until the plugin branches merge.
- 2026-11-10 — **Re-run on the committed fix, Lab1: both steps PASS** (96 s). Both `run.json` record plugin `cffcbb157037`, dirty no, and native `5de52423e1c5`, dirty no. These two steps' `summary.txt` + `run.json` replace the board's 2026-11-09 `run` capture (FAIL on `a3c6996`) and add `stop-on-terminate-false`. The board's other cells stay 2026-11-09's.
  - **Status `done`:** every Verification box is ticked.
  - **Not done here (release work, each its own approval):** merging the two plugin branches into their `master`s, bumping the parent's submodule pointers, and releasing the plugins.
