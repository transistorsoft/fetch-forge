---
id: WO-007
title: On Capacitor Android, an Activity destroyed while its process lives never makes TSBackgroundFetch headless, so the next fetch goes to the destroyed Bridge. Nothing is delivered headless, nothing finishes, and stopOnTerminate never applies
status: draft
created: 2026-11-12
platforms: [android]
submodules: [capacitor/background-fetch]
branch: fix/WO-007-capacitor-android-destroy-not-headless
issues: []
supersedes: []
---

> **Found by** WO-002's device lab. This is its suspected defect 4 (`WO-002-background-fetch-labkit.md:64`). The first Capacitor Android Tier 1 sweep read it red in the two cells written to measure it, as their descriptions predicted: `labs/harness/scenarios/stop-on-terminate.yaml:31-39` and `headless-activity-destroyed.yaml:25-33`. Both edits are in the working tree and not yet committed.
>
> **Evidence markers:** **MEASURED** = seen in a lab capture; **PROVEN** = read in source; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at.
>
> **Citation bases:**
> - **Native:** `399765c` (TSBackgroundFetch 4.2.1), read with `git -C native/background-fetch show 399765c:android/…`. Its Android sources are identical to tag `4.1.1`; only `build.gradle` and the versioning properties differ.
> - **Bridges,** at their checked-out commits: Capacitor `9dd204a`, React Native `85a6bf4`, Cordova `38ad0fe`, Flutter `11d53b8`. Types: `5c9685d`.
> - **Capacitor's framework:** `@capacitor/android` 8.5.3, the lab app's copy (`labs/platforms/capacitor/app/node_modules/@capacitor/android/capacitor/src/main/java/com/getcapacitor/`).
> - **androidx `ProcessLifecycleOwner`:** the lifecycle-process 2.10.0 sources jar in the Gradle cache. Which version the lab APK resolves is NOT CHECKED.
> - **Captures:** sweep `20261112-113743`, variant `FIX`, native label `4.1.1-master-399765c`, Android 16 / API 36.
>   - Capacitor's reds ran on Lab1 (`emulator-5580`). React Native's controls ran on Lab2 (`emulator-5582`).
>   - Line numbers refer to the on-disk `logcat.txt` and `jobscheduler.txt`. Git keeps only `run.json` and `summary.txt` (`labs/.gitignore:6-10` ignores the rest), and this sweep's are not committed yet: the cited Capacitor captures are untracked (`termination-calibration`'s are modified), and React Native's `run.json` files are modified in the working tree.

## Diagnosis

**MEASURED: `stop-on-terminate`, step `finish`.** Config: `stopOnTerminate: true`, `enableHeadless: true`. Source: `labs/captures/capacitor/stop-on-terminate/FIX/finish/logcat.txt`.

| Time | Line |
|---|---|
| 23.807 | `point=finishNow activity=MainActivity@aae7986` (`:543`): the Activity's own `finish()` (`MainActivity.java:180-185`) |
| 24.507, 24.861 | `☯️  onPause`, `☯️  onStop` (`:675`, `:692`): ProcessLifecycleOwner's events, which leave the headless flag alone |
| 24.862 | `point=onDestroy … isChangingConfigurations=false isFinishing=true headless=false`, then `App destroyed`, then `point=onDestroy:afterSuper … headless=false` (`:694-697`) |
| 28.046 | `- Background Fetch event received: capacitor-background-fetch`, then `Notifying listeners for event fetch` (`:706-707`). The app logs nothing more in the window. |
| 33.610 | The second forced run: `point=host i=1 action=job 999 expect=absent waited_ms=5468 missed=present said=Running_job_FORCED` (`:713`) |

- **Never logged:** `☯️  HeadlessMode? true`, `- Stopping on terminate`, `- finish:`, `- jobFinished`.
- **At the step's end** JobScheduler is still running job 999 (`jobscheduler.txt:1558-1560`):
  - `Active jobs: … #u0a220/999 … Running for: +31s953ms, timeout at: +4m28s53ms`.
  - Its history has the forced run's `START-P` and no `STOP-P` (`:1553`).
- **Score:** FAIL.
  - `stoppingOnTerminate 0` (want 1)
  - `jobsCancelled -` (want 999)
  - `jobFinished 0` (want 1)
  - `headless false` (want true)
  - `jobIds 999` (want `-`)
  - `actionsDone action1=missed(present)`

**MEASURED: `headless-activity-destroyed`.** Config: `stopOnTerminate: false`, `enableHeadless: true`. Source: `…/headless-activity-destroyed/FIX/run/logcat.txt`.
- **The log:** the same sequence (`:571`, `:713-717`, `:730-731`), then nothing. No `point=headless`, no `point=task-api`, no `- finish:`.
- **At the end:** job 999 is still running (`jobscheduler.txt:1563-1565`).
- **Score:** FAIL.
  - `headlessEvents.fetch 0` (want 1)
  - `headlessCalls -` (want `finish:ok=1`)
  - `fetchFinished 0` (want 1)
  - `jobFinished 0` (want 1)
  - `headless false` (want true)

**What `actionsDone action1=missed(present)` means.** The `finish` step fires `job: 999` twice, each 3 s after `point=onDestroy` (`stop-on-terminate.yaml:83-84`).
- **The two actions:** action0 expects the job to run. action1 is `expect: absent`, because stopOnTerminate should have cancelled the job by then. The host spaces them 5.5 s apart.
- **How the host scores them:**
  - It runs `cmd jobscheduler run -f <pkg> 999`.
  - JobScheduler answers `Running job [FORCED]` for a job it holds, and `Could not find job 999` for one it does not.
  - `Running job` where absent was expected becomes `missed=present` (`fetch_pack.py:318-321`).
  - `actionsDone` scores any `missed=` mark as `action<i>=missed(<why>)` (`labkit.py:4320-4321`).
- **So** JobScheduler still held job 999 after the fetch: the lazy stop never ran. React Native's second run reads `said=Could_not_find_job_999…` (below).
- **New, MEASURED:** a forced run of a job that is still executing starts nothing. There is no second `event received`, and the history shows a single `START-P`. The Tier 1 design had left this NOT CHECKED.

**React Native passes both scenarios on the same sweep** (Lab2, plugin `85a6bf4`):
- **`stop-on-terminate/FIX/finish/logcat.txt`:**
  - `point=onDestroy … isFinishing=true` (`:411`), `☯️  HeadlessMode? true` (`:419`), `onDestroy:afterSuper … headless=true` (`:422`);
  - then `- Background Fetch event received`, `- Stopping on terminate`, `- stop: react-native-background-fetch`, `- jobFinished`, `- cancel taskId=react-native-background-fetch, jobId=999` (`:433-437`);
  - the second run reads `said=Could_not_find_job_999…` (`:458`).
- **`headless-activity-destroyed/FIX/run/logcat.txt`:** `☯️  HeadlessMode? true` (`:424`), then `[HeadlessTask] start taskId: 1` and `LAB point=headless event=fetch` (`:439-440`), then `- finish:` and `- jobFinished` (`:450-451`).

**Capacitor's headless path itself works.** A new process starts headless (`LifecycleManager.java:40`), and all three of Capacitor's cells that deliver in a new process pass on the same sweep (`termination-calibration`'s `kill` among them):
- `stop-on-terminate`'s `kill` logs `- Stopping on terminate` and `- cancel …jobId=999` (`…/kill/logcat.txt:700-703`);
- in `headless-kill`, the lab's Java `BackgroundFetchHeadlessTask` runs and finishes (`…/headless-kill/FIX/run/logcat.txt:549-561`).

Only a destroy inside a live process fails.

**The code path (PROVEN, source):**
- **The SDK cannot see the destroy by itself.**
  - `LifecycleManager` starts headless (`LifecycleManager.java:40`) and observes `ProcessLifecycleOwner` (`:121-124`, posted from `BackgroundFetch.java:79`).
  - ON_START and ON_RESUME clear the flag (`:154`, `:192`). ON_PAUSE and ON_STOP set only `mIsBackground` (`:168-183`).
  - Its ON_DESTROY handler would set the flag (`:161-166`). But `ProcessLifecycleOwner` never dispatches ON_DESTROY (`ProcessLifecycleOwner.kt:32-33`).
  - So in a live process, only a bridge's `setHeadless(true)` (`:86-96`) makes the SDK headless.
- **Capacitor's plugin never calls it.**
  - `BackgroundFetchPlugin.java` (`:1-167`) overrides only `load()` and its `@PluginMethod`s. It has no `handleOnDestroy`, no `handleOnResume` and no reference to `LifecycleManager`.
  - Capacitor calls each plugin's `handleOnDestroy()` only from `Bridge.onDestroy()` (`Bridge.java:1397-1400`). `BridgeActivity.onDestroy()` calls that after `super.onDestroy()` (`BridgeActivity.java:127-133`). The base method is empty (`Plugin.java:1004`).
  - Capacitor's own plugins use the hook (`CapacitorHttp.java:37`, `CapacitorCookies.java:31`).
  - The plugin has never had one: `git log -S` over `android/` finds no commit that adds or removes `handleOnDestroy`, `setHeadless` or `LifecycleManager`.
- **So `doFetch` takes the foreground branch** (`BackgroundFetch.java:264-292`). Its four branches:
  - not headless: `mFetchCallback.onFetch` (`:272-275`);
  - headless with stopOnTerminate: `- Stopping on terminate`, then `stop()` (`:276-278`);
  - headless with a job service: `fireHeadlessEvent` (`:279-285`);
  - headless without one: a warning, then `finish()` and `stop()` (`:286-291`).
- **The callback belongs to the destroyed Bridge.**
  - `mFetchCallback` (`:72`) is the one the last `configure()` set (`:85`). Here that is the `Callback` built in `BackgroundFetchPlugin.configure` (`:40-53`) for the finished Activity's plugin instance.
  - Its `onFetch` calls `notifyListeners("fetch", …)` (`:41-46`). `Plugin.notifyListeners` logs the measured `Notifying listeners for event fetch` (`Plugin.java:666`), then resolves the dead page's saved `addListener` calls (`:683-686`).
  - Each resolve posts through `MessageHandler.sendResponseMessage` (`PluginCall.java:53-56`; `MessageHandler.java:101-139`). It posts to a WebView that `Bridge.onDetachedFromWindow` destroys (`BridgeActivity.java:136-141` → `Bridge.java:1412-1415`). That Android detaches the Activity's window right after `onDestroy`, so before the fetch, is INFERRED: the framework source was not read.
  - MEASURED: nothing is logged and nothing crashes (`crashes 0`, no `sendResponseMessage: error`). No JavaScript runs (`driverEvents -`).
- **Nothing finishes the BGTask.**
  - These finish it: the app's `finish(taskId)` (`BackgroundFetch.java:195-201`), a `stop` (`:151-154`, `:162-165`), a `scheduleTask` of the same taskId (`:182-185`), a reschedule (`BGTask.java:124-127`), and the headless branches that have no job service or no config (`BackgroundFetch.java:286-291`; `BGTask.java:221-227`). None of them runs here.
  - The SDK's 60 s timer (`BGTask.java:28`, `:85`) fires after the 35 s window. Its `onTimeout` takes the same not-headless branch to the same dead callback (`:207-211`). INFERRED.
  - Next, JobScheduler's own timeout calls `onStopJob`. That reaches the dead callback again, then calls `jobFinished` (`FetchJobService.java:63-76`). INFERRED.
  - The `BGTask` stays in `mTasks` until something finishes it or the process dies: a later `configure()` (`BackgroundFetch.java:88-95` → `BGTask.java:123-127`), a `stop()`, or the next run's `finish(taskId)`, which finds this stale task first (`BGTask.java:32-39`). INFERRED.
  - `finish()` (`BGTask.java:106-121`) removes it. So would `BGTask.clear()` (`:62-66`), from `stop(null)` (`BackgroundFetch.java:158`), but Capacitor's `stop` never passes null (`BackgroundFetchPlugin.java:80-81`).
  - In the lab, the job ended only when the harness force-stopped the app at the step's end (`am force-stop`, `backends.py:95-96`; `run.py:1349-1352`). Lab1's next dump reads `STOP-P: #u0a220/999 … unexpectedly disconnected` (`headless-activity-destroyed/FIX/run/jobscheduler.txt:1555`), which matches the `finish` step's `terminated_post` to the millisecond.

**React Native's hook (PROVEN, source; MEASURED above):**
- `RNBackgroundFetchModule` registers as a `LifecycleEventListener` (`:25`).
- Its `onHostDestroy` (`:116-126`) calls `setHeadless(true)` unless the current Activity `isChangingConfigurations()`. That guard is WO-003's (`:121-122`).
- Its `onHostResume` clears the flag (`:98-106`).
- **When the hooks arrived:** React Native added this one in 4.2.0 (`3540808`). Cordova added its own in 7.1.3 (`4e0596a`; `CHANGELOG.md:46-47`, dated 2023-07-07).

**Not a 4.2.1 regression (PROVEN; the dating INFERRED).**
- The SDK started relying on bridges in native `b331a49` (2022-10-11). That commit replaced the SDK's own check of the task stack (`isMainActivityActive`, `getRunningTasks`) with `LifecycleManager`.
- Capacitor's plugin never got the hook, so Capacitor apps have had this gap since that change (INFERRED).
- The native Android sources are unchanged from `4.1.1` to `399765c`.

**Who reaches it (INFERRED; how often is NOT CHECKED).** Any Capacitor Android app whose BridgeActivity is destroyed while its process lives:
- **The app calls `finish()`.** This is the lab's route.
- **Back on Android 11 and below,** which finishes the root Activity. Android 12+ moves the task to the back instead (INFERRED, WO-002).
- **The task is removed from Recents while something keeps the process alive,** for example background-geolocation's foreground service. Whether `onDestroy` runs in that case is NOT CHECKED.
  - background-geolocation does not set fetch's headless flag either. In bg-forge `addc50501`, no Android source in `capacitor/background-geolocation/android` or `native/` references `tsbackgroundfetch`, apart from a demo app (PROVEN, grep).

**The cost:**
- **With `stopOnTerminate: false` and `enableHeadless: true`,** every such fetch is lost. That is the documented headless setup (`capacitor/background-fetch/README.md:26`; `types/background-fetch/src/interfaces.ts:11-13`, `:38-43`).
- **With the default `true`,** the job is never stopped. Each run is held until JobScheduler's own timeout and delivers nothing (INFERRED: in the lab the harness's force-stop ended it first).
  - For this forced run that timeout was about 5 minutes (MEASURED: `stop-on-terminate/FIX/finish/jobscheduler.txt:1560`). A scheduled run's limit is NOT CHECKED.

## Proposed fix

**Change 1, the defect: add `handleOnDestroy()` to `capacitor/background-fetch/android/src/main/java/com/transistorsoft/bgfetch/capacitor/BackgroundFetchPlugin.java`.**
- It calls `LifecycleManager.getInstance().setHeadless(true)`, unless `getActivity()` is changing configurations.
- It is React Native's `onHostDestroy` (`RNBackgroundFetchModule.java:116-126`) and Cordova's `onDestroy` (`CDVBackgroundFetch.java:160-170`), in Capacitor's hook.
- **It must tell a recreation from a real destroy.**
  - Capacitor calls `handleOnDestroy` on a recreation too. MEASURED in `recreate-foreground-android/FIX/run/logcat.txt` (Lab2): `point=onDestroy … isChangingConfigurations=true` (`:850`), then `App destroyed` (`:853`), then the new instance's `BackgroundFetchPlugin load` (`:1009`).
  - Without the guard, the recreation would set the flag. That is WO-003's defect moved to Capacitor:
    - with `stopOnTerminate: true`, a fetch with the app on screen is stopped;
    - with `false`, it goes to the Java headless task instead of the page.
  - WO-003 MEASURED this on React Native; here it is INFERRED.
  - `recreate-foreground-android` passes on Capacitor today, on both steps, with `headless false` and `lifecycleTally onCreate=1,onResume=1,onStart=1`. It must stay that way.
- **The flag can be read there.**
  - `Plugin.getActivity()` returns the Bridge's `final` Activity (`Plugin.java:208-210` → `Bridge.java:114`, `:206`, `:499-501`). Like React Native's `getCurrentActivity()` in `onHostDestroy` (`RNBackgroundFetchModule.java:119-121`; WO-003's Log), it is the Activity being destroyed. Unlike it, it is a `final` field that is never cleared, and it is never null under a `BridgeActivity`.
  - It is read after `Activity.onDestroy` returns (`BridgeActivity.java:128`, then `:130`). Whether `isChangingConfigurations()` still holds at that point is INFERRED. The lab's probe reads it before `super` (`MainActivity.java:192`). The fix's run will settle it.

**Change 2 (D2): add `handleOnResume()`, which calls `setHeadless(false)`.**
- React Native's `onHostResume` (`:98-106`) and Cordova's `onResume` (`:152-158`) have done this since WO-003.
- It covers a new BridgeActivity that resumes after a real destroy without `ProcessLifecycleOwner` sending ON_START. INFERRED.
  - ON_START follows only an ON_STOP (`ProcessLifecycleOwner.kt:89-95`, `:128-133`). So none is sent when the relaunch comes within 700 ms of the old Activity's pause (`:69`, `:109-114`), or while another of the app's Activities is still started.
  - After the measured finish, ON_STOP went out first (MEASURED: `☯️  onStop` at 24.861, `point=onDestroy` at 24.862; `stop-on-terminate/FIX/finish/logcat.txt:692`, `:694`). A relaunch there would get ON_START, which clears the flag (`LifecycleManager.java:154`). INFERRED.
- Unlike Cordova's plugin, Capacitor's new instance exists before its Activity resumes (MEASURED: `load` at `:1009` comes before `App resumed` at `:1041`). So change 2 runs on a recreation too.
- **It also hides a missing guard from the lab.**
  - With change 2, an unguarded change 1 leaves the flag set only between `App destroyed` and the new `App resumed`. That gap was 37 ms and 34 ms in today's two recreate captures (MEASURED).
  - Both steps fire the job 4 s after the trigger (`recreate-foreground-android.yaml:67`, `:94`), so they would still pass.
  - The guard's evidence is then `lifecycleTally`, which is recorded, not asserted: it must show no `headless:true`. That entry comes from a `☯️  HeadlessMode? true` line (`LifecycleManager.java:88-90`). See D4 and the Test plan.

**Anchors:**
- `// (WO-007)` at both methods.
- An `[Android]` entry under the plugin's `## Unreleased`, carrying `(WO-007)`.
- The two scenarios' descriptions name WO-007 where they now say "its own work order". This is prose only.

**Decisions:**

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Where the fix lives | The Capacitor plugin; or native `LifecycleManager` (for example `ActivityLifecycleCallbacks`) for every bridge | The plugin, as in WO-003's D1. It needs no native release: `setHeadless` has been public since `b331a49`, and the plugin resolves `tsbackgroundfetch:4.1.+` (`android/build.gradle:58-59`). Not an acceptable shortcut sets conditions on the native route. |
| D2 | Change 2 | Take it; or ship change 1 alone | Take it, for parity with React Native and Cordova |
| D3 | Where it ships | In the plugin's next release, whose `## Unreleased` already holds WO-001's iOS entries; or in an Android-only patch first | Your call. The fix touches no iOS or native code. |
| D4 | How to prove the guard | (a) Read `lifecycleTally` in the fix's recreate captures. (b) Also run a throwaway build without the guard, to watch `headless:true=1` appear while the steps still pass. (c) Add a new asserted metric (lab work). | (a). Add (b) only if you want proof that the guard matters on Capacitor. |

## Scope check

- **Who calls `handleOnDestroy`:** in `@capacitor/android` 8.5.3, only `Bridge.onDestroy` (`Bridge.java:1399`), which only `BridgeActivity.onDestroy` calls (`:130`). PROVEN, grep.
  - It runs on a finish, on a recreation (MEASURED above), and on any other destroy. An Activity destroyed under "Don't keep activities" counts as terminated, as on React Native (INFERRED).
  - NOT CHECKED: apps that host the Bridge themselves (in a Fragment, or with several Bridges) and call `bridge.onDestroy()` on their own schedule. If one Bridge is destroyed while another of the app's Activities stays on screen, the SDK becomes headless with the app visible. That lasts until the next BridgeActivity resume (change 2) or ON_START.
- **Who calls `handleOnResume` (change 2):** `Bridge.onResume` (`:1357-1360`), on every BridgeActivity resume.
  - That includes the first launch, which comes before `LifecycleManager` registers. MEASURED: `App resumed` at 13.697, before `☯️  onCreate` at 13.709 (`stop-on-terminate/FIX/finish/logcat.txt:200`, `:223`).
  - At that point `setHeadless(false)` only sets the flag, because no startup event is pending (`:91`). Registration then replays ON_CREATE, which sets it true (`:138`), and ON_START, which sets it false (`:154`). INFERRED.
- **Who reads the flag:**
  - `BackgroundFetch.doFetch` (`:272`) and `BGTask.onTimeout` (`:207`). For a destroyed Activity, both change as intended.
  - `setHeadless(true)` also logs `☯️  HeadlessMode? true` and settles any pending startup listeners (`LifecycleManager.java:86-96`), as it does for React Native and Cordova.
- **What customers will see:**
  - After a destroy, the default `stopOnTerminate: true` cancels the job at the next fetch.
  - `false` with `enableHeadless` sends that fetch to the app's `BackgroundFetchHeadlessTask`.
  - Both are what the types document, and what React Native and Cordova have done since 2023. Today both fetches go nowhere. The changelog entry should say so.
- **Unchanged:** the JS surface (`src/`), the types, the plugin's iOS side, and the native library.

## Platform parity

- **Android, React Native: not affected.** PROVEN (`RNBackgroundFetchModule.java:116-126`) and MEASURED: both scenarios pass on the same sweep.
- **Android, Cordova: not affected in the plugin (PROVEN in source).**
  - `CDVBackgroundFetch.java:160-170` sets the flag on any `onDestroy` that is not a recreation. `:152-158` clears it on `onResume`.
  - That cordova-android calls a loaded plugin's `onDestroy` when its Activity finishes is INFERRED (`CordovaActivity.onDestroy` → `appView.handleDestroy()` → `pluginManager.onDestroy()`, per WO-003).
  - The finish case is NOT MEASURED. There is no Cordova lab host yet, and WO-003's manual check covered only the recreation.
- **Android, Flutter: not affected in the plugin (PROVEN in source).**
  - `onDetachedFromActivity` (`BackgroundFetchPlugin.java:47-50`) calls `setActivity(null, null)`, which calls `setHeadless(true)` (`BackgroundFetchModule.java:108-109`).
  - The config-change detach is empty (`BackgroundFetchPlugin.java:34-39`), and attaching an Activity clears the flag (`BackgroundFetchModule.java:102-103`).
  - That Flutter's embedding calls `onDetachedFromActivity` when its Activity finishes is INFERRED. NOT MEASURED.
- **Android, apps that use TSBackgroundFetch directly:** they make their own `setHeadless` calls. NOT CHECKED.
- **iOS: N/A, because iOS has no headless mode.**
  - TSBackgroundFetch has a `stopOnTerminate` property (`TSBackgroundFetch.h:20`), `YES` by default (`TSBackgroundFetch.m:55`), which no bridge's iOS side sets. Its only use is a log line in `onAppTerminate`, which does nothing on purpose (`:433-438`).
  - The types mark `stopOnTerminate` and `enableHeadless` Android-only (`interfaces.ts:12`, `:39`).
  - Capacitor's iOS plugin removes its listener in `deinit` (`BackgroundFetchPlugin.swift:37-38`), so no fetch can reach a dead plugin. A listener removed mid-task is WO-005's ground.

## Test plan

- **Red before the fix:** already MEASURED (above). Nothing needs to be re-run first.
- **On the fix branch, Capacitor Android, Lab1 and Lab2.** The lab stages whatever the checkout named by `lab.yaml`'s `default_checkout` has, and `run.json` records its commit and dirty flag.
  - **`stop-on-terminate`, step `finish`** (the values the scenario already expects):
    - `☯️  HeadlessMode? true` at `onDestroy`, and `onDestroy:afterSuper … headless=true`;
    - then `- Stopping on terminate`, `- stop: capacitor-background-fetch`, `- jobFinished` and `- cancel taskId=capacitor-background-fetch, jobId=999`;
    - the second run answers `Could not find job 999`;
    - `kill` stays green.
  - **`headless-activity-destroyed`:** the lab's Java task logs `point=headless event=fetch …`. Then the SDK logs `- finish:` and `- jobFinished`, and the task logs `point=task-api api=finish … outcome=ok`. That is the order `headless-kill` shows (`…/headless-kill/FIX/run/logcat.txt:549`, `:559-561`).
  - **`recreate-foreground-android`, both steps:** still pass, and `lifecycleTally` shows no `headless:true` (D4). This is the guard's proof.
- **Regression:** the Android Tier 1 sweep on the fix.
  - React Native: 12/12. No React Native code changes.
  - Capacitor: 11/12. `api-smoke` stays red, only for its own finding (`scheduleTask#4`, `stop#5` and `stop#7` resolve undefined).
- **No unit test.** The plugin's `android/src/test` holds only the template's `ExampleUnitTest.java`. The lab cells are the test, and their descriptions carry the id.

## Out of scope

- **`api-smoke`'s Capacitor red.** `scheduleTask` and `stop` resolve undefined where the types declare a boolean. That is its own finding.
- **What a delivery to a dead listener leaves behind.** That is a BGTask that is never finished, the SDK's timeout that only notifies, and a stale `BGTask` that can shadow the next run's finish (WO-002 suspected defect 3). This fix closes one route to them, not the others.
- **`mFetchCallback` keeps the destroyed plugin alive** until the next `configure()`, and through it the plugin's Bridge and Activity (INFERRED). A relaunched page that does not call `configure()` before the next fetch still delivers to the dead plugin.
- **Every page boot's `configure()` reschedules job 999,** which restarts its interval. The Tier 1 design recorded this observation.
- **Capacitor iOS.**
- **Native headless detection** (D1's alternative), unless you choose it.

## Not an acceptable shortcut

- **Changing the lab or the scenarios to avoid the red.**
  - That includes:
    - a Capacitor `platform_expect` or `not_applicable` that relaxes any of this defect's values. The existing ones (`headless-activity-destroyed.yaml:72-75`, `recreate-foreground-android.yaml:49-52`) cover only React Native's own HeadlessTask lines and Capacitor's reschedule, and stay;
    - dropping `capacitor` from `platforms`;
    - another trigger or a longer window;
    - the driver or probe calling `setHeadless`.
  - The expectations are the values a sound bridge gives, and React Native gives them on the same sweep. The descriptions said in advance that "nothing here is relaxed for it" (`stop-on-terminate.yaml:37-38`).
  - Naming WO-007 in the descriptions is prose only.
- **Fixing it in the SDK's `LifecycleManager` for every bridge without proving that React Native's behaviour stays identical.**
  - Native destroy detection would add a second writer of the flag, beside React Native's `onHostDestroy`, Cordova's `onDestroy` and Flutter's detach.
  - Its order against those writers, on a recreation, a finish and a headless launch, decides what every bridge and native consumer sees.
  - It would need a native release, with React Native's full Tier 1 green and unchanged on it (`recreate-foreground-android`, `stop-on-terminate`, `headless-activity-destroyed`). It would also need Cordova and Flutter checks, which the lab cannot run yet.
  - WO-003's D1 kept this in the bridges for the same reason.
- **A `handleOnDestroy` without the `isChangingConfigurations()` guard.** It moves WO-003 to Capacitor. With change 2 the steps would still pass (above), so `lifecycleTally` must be read.
- **Going headless in `handleOnPause` or `handleOnStop`.** A backgrounded app still has its page. With the default `stopOnTerminate: true`, the job would be cancelled whenever the user switches apps (INFERRED).
- **Waiting for `ProcessLifecycleOwner`.** It never sends ON_DESTROY (`ProcessLifecycleOwner.kt:32-33`), so `LifecycleManager.onDestroy` (`:161-166`) can never fire.
- **Retaining or queueing the event for the next page** (`notifyListeners(…, true)`).
  - The dead page's listener calls are still registered, so retention never triggers (`Plugin.java:668-681`).
  - A fetch replayed at the next launch is long past its BGTask and JobScheduler window, and stopOnTerminate would still never apply.
- **Finishing the task, or calling `stop()`, from the plugin when its Bridge is gone.**
  - Finishing hides the event from the app's headless task.
  - A `stop()` in `handleOnDestroy` would also cancel the job for `stopOnTerminate: false` apps, which are the ones that want headless fetches.

## Verification

- [ ] D1–D4 decided
- [ ] `stop-on-terminate` (both steps) and `headless-activity-destroyed` green on the fix, Capacitor Android, Lab1 and Lab2
- [ ] `recreate-foreground-android` (both steps) green on the fix, Lab1 and Lab2, with no `headless:true` in `lifecycleTally`
- [ ] Android Tier 1 sweep on the fix: React Native 12/12; Capacitor 11/12, with `api-smoke` red only for its own finding
- [ ] `// (WO-007)` at the changed lines; the Capacitor CHANGELOG's `## Unreleased` `[Android]` entry carries `(WO-007)`; the scenario descriptions name WO-007 (prose only)
- [ ] Platform parity: Cordova and Flutter not affected, as cited; iOS N/A

## Log

- 2026-11-12 — **Created (draft)** from WO-002's first Capacitor Android Tier 1 sweep.
  - **The sweep:** `20261112-113743`, variant `FIX`, native `4.1.1-master-399765c`, Capacitor `9dd204a`, React Native `85a6bf4`. React Native: 12/12 PASS. Capacitor: 9 PASS, 3 FAIL.
  - **Two of the three FAILs are this defect,** WO-002's suspected defect 4, red as the scenarios predicted. The third is `api-smoke`'s own finding.
  - **New since the prediction (MEASURED):**
    - the delivery to the destroyed WebView logs no error and crashes nothing;
    - a forced run of a job that is still executing starts nothing;
    - the stranded job ended only when the harness force-stopped the app.
  - **Not done yet:** no branch, no code, nothing re-run.
  - **Corrected after two skeptics' review** (16 corrections; none declined).
