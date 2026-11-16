---
id: WO-010
title: On Cordova Android, when an app restarts itself the old Activity's destroy arrives after the new Activity has resumed, and CDVBackgroundFetch.onDestroy then makes TSBackgroundFetch headless with the new page on screen, so that page's next fetch is stopped (stopOnTerminate true) or sent to the headless task (false)
status: draft
created: 2026-11-16
platforms: [android]
submodules: [cordova/background-fetch]
branch: fix/WO-010-cordova-android-restart-ordering
issues: []
supersedes: []
---

> **Found by** WO-002's device lab. `restart-foreground-android` is WO-007's lab guard (WO-002 Log, 2026-11-13). It was PREDICTED RED for Cordova from source and read red on both steps in all three Cordova Android sweeps: `20261115-183522`, `20261115-190943` and `20261115-215127`. WO-007's Platform parity (`WO-007-capacitor-android-destroy-not-headless.md:199`) and its Log (2026-11-13, `:316`) had already called Cordova's exposure INFERRED.
>
> **Evidence markers:** **MEASURED** = seen in a lab capture; **PROVEN** = read in source; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at; **SUSPECTED** = a defect inferred from source that no device run reproduced.
>
> **Citation bases,** source read on 2026-11-16:
> - **The plugin:** `cordova/background-fetch` `368eb85` (7.3.1 plus Unreleased). Its Android file is `src/android/CDVBackgroundFetch.java`, cited as `CDVBackgroundFetch.java`.
> - **The other bridges:** React Native `8d9ab19`, Capacitor `3fa169a` (its `android/` is identical to `4d431c1`'s, the commit the lab measured: `git diff` is empty), Flutter `11d53b8`. Types: `dc4f5b8`.
> - **Native:** `399765c` (TSBackgroundFetch 4.2.1, lab label `4.1.1-master-399765c`), read with `git -C native/background-fetch show 399765c:<path>` (the checkout sits on `0670713`). Cited as `native/background-fetch/<path>:<line> (399765c)`. The Android classes are under `android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/`, shortened to `native/background-fetch/…/<Class>.java`.
> - **cordova-android 15.0.0:** the lab host's copy, read only (`labs/platforms/cordova/app/node_modules/cordova-android/framework/src/org/apache/cordova/`), cited as `cordova-android/<File>.java`.
> - **React Native 0.88.0:** the lab host's copy (`labs/platforms/react-native/app/node_modules/react-native/ReactAndroid/src/main/java/com/facebook/react/`).
> - **Captures:** `labs/captures/cordova/restart-foreground-android/FIX/{run,stop-on-terminate-false}`.
>   - They are sweep `20261115-215127`'s: variant `FIX`, Lab2 (`emulator-5582`), Android 16 / API 36, plugin `368eb85`, native `4.1.1-master-399765c`, both clean. Each `run.json`'s `timings.t0` matches that sweep's `board.json`. `run.json` carries no sweep id.
>   - A sweep's `board.json` is at `labs/harness/.claims/sweep-<id>/board.json`. It is local: `labs/harness/.claims/` is gitignored (`labs/.gitignore:14`).
>   - They were committed in `82d306e`. The earlier commit `1796d96` held sweep `20261115-190943`'s, which WO-002's Cordova Android entry called "the committed captures" (corrected in WO-002's 2026-11-16 entry).
>   - So git keeps `summary.txt` and `run.json` for `20261115-190943` (`1796d96`) and `20261115-215127` (`82d306e`). For `20261115-183522` it keeps only that the sweep was red (WO-002's Log, `WO-002-background-fetch-labkit.md:1260`; `restart-foreground-android.yaml:46`). That sweep's per-metric reds are in its local `board.json` alone.
>   - `logcat.txt` is gitignored (`labs/.gitignore:6`). The on-disk copies are the same sweep's: same timestamps, and the tracked files are clean. Line numbers refer to them.
>   - All three red sweeps ran this scenario on Lab2. None ran it on Lab1 for Cordova. Runs outside a sweep: NOT CHECKED.

## Diagnosis

**What the scenario does** (`labs/harness/scenarios/restart-foreground-android.yaml:1-54`).
- **Launch, then settle 10 s.** The driver calls `configure()` on the page's first boot.
- **Trigger `finish` with `restartOnFinish`.** The host calls `finish()`, then `startActivity()` with a copy of its own launch intent (`labs/platforms/cordova/app/native/MainActivity.java:193-206`). That is how an app restarts itself. The new Activity loads the page again, and the page calls `configure()` again on boot 2.
- **The job.** `job 999` runs 4 s after the old Activity's `point=onDestroy` (`:80`, `:109`). The window is 30 s.
- **What it expects,** the values a sound bridge gives (`:15-25`, `:81-127`):
  - `restartOrder resumed-first`;
  - one process;
  - the page's own delivery: `driverEvents fetch=1`, `driverTally fetch:cordova-background-fetch=1`, `configure@0` fired once;
  - `stoppingOnTerminate 0`, `headless false`, `jobIds 999`;
  - in the second step, `headlessEvents.fetch 0` as well.
- **`restartOrder`** reads the finishing Activity's `point=onDestroy … current=` line (`labs/harness/fetch_pack.py:1212-1234`). `current` is the host's record of the Activity resumed most recently (`MainActivity.java:185-187`). So `resumed-first` proves that the ordering under test happened.
- **`headless`** is the probe's last reading in the window (`labs/harness/labkit.py:3454-3456`).

**MEASURED: step `run`** (`stopOnTerminate: true`, `enableHeadless: false`). Source: `…/FIX/run/logcat.txt`, sweep `20261115-215127`.

| Time | Line |
|---|---|
| 26.091, 26.186 | Boot 1: `Resumed the activity.` (`:166`), then `getPlugin - put: BackgroundFetch` (`:223`). The plugin is created 95 ms after its Activity resumed. |
| 36.276 | `point=trigger finish` (`:543`) |
| 36.301, 36.302 | The old Activity: `Paused the activity.` (`:556`), `point=finishNow activity=MainActivity@9bc47d restart=true` (`:558`) |
| 36.311 | The new Activity: `point=onCreate activity=MainActivity@5436206 … creates=2` (`:578`) |
| 36.321 | `startupPlugins: put - BackgroundFetch` (`:613`): the new PluginManager reserves the plugin and does not create it |
| 36.324 | The new Activity: `Resumed the activity.` (`:620`). Its PluginManager has no BackgroundFetch instance to resume. |
| 36.351 | The new page's first BackgroundFetch exec (its first call to this plugin; the lab plugin's came at 36.349, `:627`): `[CDVBackgroundFetch pluginInitialize]`, `getPlugin - put: BackgroundFetch`, then `configure()`, `Re-configured existing task` and `- cancel …jobId=999` (`:631-636`) |
| 36.799, 36.800 | The old Activity: `Stopped the activity.` (`:779`), then `point=onDestroy activity=MainActivity@9bc47d isChangingConfigurations=false isFinishing=true … current=MainActivity@5436206` (`:780`), then `CordovaActivity.onDestroy()` (`:781`) |
| 36.801 | `☯️  HeadlessMode? true` (`:784`) |
| 36.804 | `point=onDestroy:afterSuper … headless=true … current=MainActivity@5436206` (`:787`) |
| 38.329 | The new Activity's `point=resumed+2s … headless=true` (`:794`) |
| 41.119 | `- Background Fetch event received`, `- Stopping on terminate`, `- stop: cordova-background-fetch`, `- jobFinished`, `- cancel …jobId=999` (`:814-818`) |
| 21:57:04.357 | The last tick: `headless=true … tasks=-` (`:971`). The flag stayed set for the rest of the 30 s window. |

- **No `☯️  onStart` or `onResume` line after the trigger.** ProcessLifecycleOwner sent nothing. The only `☯️` line after the trigger is the `HeadlessMode? true` at `:784`.
- **Score** (`summary.txt`): FAIL.
  - **Red:**
    - `driverEvents -` (want `fetch=1`) and `driverTally -`;
    - `eventAssertions configure@0=never-fired`: the page's callback never ran;
    - `stoppingOnTerminate 1` (want 0);
    - `fetchFinished 0` (want 1): a stop finishes the BGTask without a `- finish:` line;
    - `headless true` (want false);
    - `jobIds -` (want 999): the periodic job is gone.
  - **Green:**
    - `restartOrder resumed-first`, `appProcesses 1`, `crashes 0`, `fetchReceived 1`, `jobFinished 1`;
    - `jobsCancelled 999`. That holds either way: boot 2's reschedule cancels 999 too (`:636`), as `platform_expect` records (`:64`).
  - **`lifecycleTally`** reads `headless:true=1,onCreate=1,onResume=1,onStart=1`.

**MEASURED: step `stop-on-terminate-false`** (`stopOnTerminate: false`, `enableHeadless: true`). Source: `…/FIX/stop-on-terminate-false/logcat.txt`, the same sweep.
- **The same order:**
  - boot 1's resume (08.349, `:181`) comes before its plugin's creation (08.456, `:252`);
  - the new Activity resumes at 18.599 (`:444`);
  - its plugin is created at 18.635 (`:478`);
  - the old `point=onDestroy … current=MainActivity@6217e60` is at 19.082 (`:607`);
  - `☯️  HeadlessMode? true` follows at 19.083 (`:611`).
- **The fetch goes to the Java headless task with the page on screen.**
  - 23.450: `- Background Fetch event received`, then `point=headless event=fetch` and `point=task-probe … current=MainActivity@6217e60` (`:634-636`).
  - 24.453-24.454: the task finishes it (`- finish:`, `- jobFinished`, `point=task-api api=finish … outcome=ok`, `:640-642`).
  - The ticks read `headless=true` to the end (`:722`, 46.642).
- **Score:** FAIL.
  - **Red:** `driverEvents -`, `driverTally -`, `eventAssertions configure@0=never-fired`, `headlessEvents.fetch 1` (want 0), `headless true` (want false).
  - **Green:** everything else, including `fetchFinished 1`, `jobIds 999` and `stoppingOnTerminate 0`.

**The board around it** (sweep `20261115-215127`, its local `board.json`, path above): Cordova Android 11 PASS, 2 FAIL. The other FAIL is `api-smoke`, WO-009's finding. The cells that pin the plain finish and the recreation are green:
- `stop-on-terminate` (Lab2). Its `finish` step: `point=onDestroy … current=` the same Activity (`…/stop-on-terminate/FIX/finish/logcat.txt:488`), then `☯️  HeadlessMode? true` (`:492`), then `- Stopping on terminate` (`:504`).
- `headless-activity-destroyed` (Lab2).
- `recreate-foreground-android` (Lab1).

**React Native and Capacitor pass this scenario.** Sweep `20261113-104449`, both steps each:
- **React Native** (Lab1, `8d9ab19`), in `labs/captures/react-native/restart-foreground-android/FIX/run/logcat.txt`:
  - `point=onDestroy … current=MainActivity@ee38ebe` (`:520`), then `onDestroy:afterSuper … headless=false` (`:528`);
  - the fetch reaches the page: `LAB app=event=fetch n=1` (`:552`).
- **Capacitor** (Lab2, `4d431c1`), in `labs/captures/capacitor/restart-foreground-android/FIX/run/logcat.txt`:
  - `App resumed` (`:781`) comes before the old `point=onDestroy … current=MainActivity@4178925` (`:901`);
  - then `headless=false` (`:905`), and the page's `app=event=fetch` (`:1008`).
- **Neither logs `HeadlessMode? true`.** `lifecycleTally` reads `onCreate=1,onResume=1,onStart=1` in all four captures.

**The code path (PROVEN, source).**
- **The plugin acts on any destroy that is not a recreation.**
  - `CDVBackgroundFetch.onDestroy` (`:160-170`) calls `setHeadless(true)` unless `cordova.getActivity().isChangingConfigurations()`.
  - It does not ask whether its Activity is still the current one. This is WO-007's first approved condition, the one WO-007's probe measured red.
  - The plugin's only other lifecycle override is `onResume` (`:152-158`), which calls `setHeadless(false)`. It has no `onPause`, `onStart`, `onStop` or `onReset`.
  - The plugin never decides headless itself. `BackgroundFetch.doFetch` reads the flag (`native/background-fetch/…/BackgroundFetch.java:272-291 (399765c)`), and so does `BGTask.onTimeout` (`native/background-fetch/…/BGTask.java:207 (399765c)`).
- **Each CordovaActivity has its own plugin instance.**
  - `CordovaActivity.onCreate` makes its own `CordovaInterfaceImpl` (`cordova-android/CordovaActivity.java:161`). `init()` makes its own WebView (`:167-172`), and `CordovaWebViewImpl.init` makes its own PluginManager (`cordova-android/CordovaWebViewImpl.java:99-120`, `:105`).
  - A plugin's `cordova.getActivity()` is that interface's Activity (`cordova-android/CordovaInterfaceImpl.java:59-60`, `:86-88`; `cordova-android/CordovaPlugin.java:53-61`).
  - MEASURED: `getPlugin - put: BackgroundFetch` appears once per boot (`:223`, `:632`).
- **How the destroy reaches the plugin:**
  - `CordovaActivity.onDestroy` (`:371-378`) runs `super.onDestroy()`, then `appView.handleDestroy()` (`CordovaWebViewImpl.java:526-534`);
  - that calls `PluginManager.onDestroy` (`cordova-android/PluginManager.java:331-339`), which calls every instantiated plugin's `onDestroy`.
  - That includes the old Activity's own `CDVBackgroundFetch`, created on boot 1.
- **How the resume reaches it,** and why it cannot help here:
  - `CordovaActivity.onResume` (`:323-336`) → `handleResume` (`CordovaWebViewImpl.java:495-502`) → `PluginManager.onResume` (`:292-300`). That reaches only plugins that already exist.
  - The Android `<feature>` has no `onload` (`plugin.xml:71-75`), where the iOS one has (`:46`). So `startupPlugins` only reserves the entry (`PluginManager.java:104-118`), and the plugin is created on the page's first BackgroundFetch `exec` (`:137-138` → `getPlugin`, `:175-192`).
  - MEASURED on both boots of both restart steps: `startupPlugins: put - BackgroundFetch`, then `Resumed the activity.`, then `getPlugin - put: BackgroundFetch` (`run/logcat.txt:139`, `:166`, `:223`; `:613`, `:620`, `:632`). Boot 1 of `stop-on-terminate` `finish`, `headless-activity-destroyed` and `recreate-foreground-android` shows the same resume-then-create order. So no instance ever sees the resume of the Activity it belongs to.
  - The new instance cannot record that its Activity is current, and the old one does not ask.
- **Nothing clears the flag afterwards.**
  - `LifecycleManager` clears it only on ProcessLifecycleOwner's ON_START and ON_RESUME (`native/background-fetch/…/LifecycleManager.java:142-159`, `:185-194 (399765c)`), and none was sent (MEASURED above).
  - The new instance's `onResume` runs only at its Activity's next resume, for example when the user leaves the app and comes back (INFERRED).
- **So there is one cause, and one reason the fix is more than WO-007's.**
  - **The cause:** the old instance's `onDestroy` acts on a destroy of an Activity that is no longer current. WO-002's scenario description says the same (`restart-foreground-android.yaml:45-51`).
  - **Why the fix is bigger:** the lazy creation leaves the plugin no moment at which to record which Activity is current. That is why WO-002's Log says the fix "needs another record of the current Activity (onload, or ActivityLifecycleCallbacks)" (WO-002 Log, 2026-11-15, Cordova Android).

**A second ordering, SUSPECTED (INFERRED from source; not measured).** A relaunch that resumes after the old Activity's destroy can also leave the flag set on Cordova, where it does not on Capacitor. The destroy sets the flag correctly, since no page exists at that moment. Two things then fail to clear it:
- **the plugin:** the new instance does not exist when its Activity resumes;
- **ProcessLifecycleOwner:** it sends ON_START only after an ON_STOP. ON_STOP needs both the 700 ms timeout after the last Activity's pause (which sends ON_PAUSE) and no Activity started, so it goes out at the later of the two (`ProcessLifecycleOwner.kt:51-53`, `:69`, `:89-95`, `:109-119`, `:128-133`; lifecycle-process 2.10.0, the version WO-007 read; the Cordova app's version is NOT CHECKED). A relaunch that starts after ON_STOP gets ON_START, and `LifecycleManager.onStart` clears the flag (`LifecycleManager.java:142-159 (399765c)`).

So the flag stays set only if the relaunch resumes after the old destroy AND before ON_STOP, which goes out no earlier than 700 ms after the old pause, or while another of the app's Activities keeps the app started (WO-007's Change 2, `WO-007-capacitor-android-destroy-not-headless.md:153`). Whether a single-Activity relaunch can land there depends on when the old destroy comes:
- **On Lab2 a single-Activity relaunch has no window** (INFERRED from MEASURED timings). In every plain finish measured there (seven captures, four bridges), the destroy came about 1.0 s after the pause, 0-4 ms after ProcessLifecycleOwner's `☯️  onPause` (+0.70 s) and `☯️  onStop`. Cordova, sweep `20261115-215127`: `stop-on-terminate/FIX/finish/logcat.txt` `Paused the activity.` 59.877 (`:355`), `☯️  onPause` 00.578 (`:478`), `☯️  onStop` 00.900 (`:486`), `point=onDestroy` 00.901 (`:488`); `headless-activity-destroyed/FIX/run/logcat.txt` 48.151, 48.852, 49.201, 49.202 (`:409`, `:533`, `:541`, `:543`). React Native's, Capacitor's and Flutter's `stop-on-terminate/FIX/finish`, and React Native's and Flutter's `headless-activity-destroyed/FIX/run`, show the same order: `finishNow` → `point=onDestroy` +1.04 to +1.07 s, after both `☯️` lines. A relaunch that resumes before such a destroy is the resumed-first case; one after it gets ON_START (INFERRED).
- **On Lab1 a plain finish's destroy has landed inside the 700 ms.** Capacitor's `headless-activity-destroyed/FIX/run/logcat.txt` (sweep `20261113-183802`, Lab1 `emulator-5580`): `App paused` and `point=finishNow` at 07.669 (`:819-820`), `point=onDestroy` at 08.053 (`:947`, +0.38 s), then `☯️  onPause` and `☯️  onStop` together at 08.371 (`:957-958`). A relaunch resuming between 08.053 and 08.371 would get no ON_START, and today's Cordova plugin would keep `headless=true` with the page on screen (INFERRED). No Cordova plain finish has run on Lab1 (NOT MEASURED).
- The 0.5 s pause-to-destroy gaps of this order's restarts (36.301 → 36.800; 18.580 → 19.082) are resumed-first timings: the new Activity resumed 23 ms after the pause (`:556` → `:620`) and cancelled ProcessLifecycleOwner's pause timeout (`ProcessLifecycleOwner.kt:97-107`). They say nothing about this case.

The case stays SUSPECTED. The scenario asserts only `resumed-first`, so no lab cell reads it (D4).

**When it arrived (PROVEN; who and how often INFERRED).**
- **7.1.3** (`4e0596a`, 2023-07-08; `CHANGELOG.md:46-47`) added `onDestroy` with an unconditional `setHeadless(true)`.
- **WO-003** (`8017279`, in `## Unreleased`, `CHANGELOG.md:4`) narrowed it to non-recreations. So this is not a WO-003 regression: Cordova apps that restart themselves have been exposed since 7.1.3.
- **Who reaches it:** any Cordova Android app whose CordovaActivity is destroyed after another of its CordovaActivities resumed. For example:
  - `finish()` then `startActivity()`, as the lab does;
  - a `NEW_TASK|CLEAR_TASK` relaunch;
  - a user relaunch within a second of a finish;
  - a second CordovaActivity finished back onto the first. The first resumes before the second is destroyed (INFERRED; NOT MEASURED, and the lab host has one Activity class).

**The cost** (MEASURED for the first two):
- **`stopOnTerminate: true`** (the default): the page's fetch is stopped and job 999 cancelled. No fetch fires again until the app calls `configure()` again.
- **`stopOnTerminate: false` with `enableHeadless: true`:** each fetch goes to the Java headless task beside the live page, and the page's callback never runs.
- **`false` without `enableHeadless`:** the SDK's no-jobService branch finishes and stops the task (`BackgroundFetch.java:286-291 (399765c)`; INFERRED, not run).
- **How long:** until ProcessLifecycleOwner's next ON_START (the app in the background for more than 700 ms) or a resume of the new Activity (INFERRED). In the lab it lasted the whole window.

## Proposed fix

**Change 1, under D1 (a): make the plugin `onload` on Android.** In `plugin.xml`, give the Android `<feature name="BackgroundFetch">` (`:71-75`) `<param name="onload" value="true"/>`, as the iOS feature already has (`:46`).
- **What cordova-android does with it:**
  - it reads the param (`cordova-android/ConfigXmlParser.java:150-151`) and creates onload plugins in `startupPlugins` (`PluginManager.java:104-118`);
  - `startupPlugins` runs from `PluginManager.init` (`:92-99`), which runs from `CordovaWebViewImpl.init` (`:119`) and `CordovaActivity.init` (`:167-172`);
  - those run when the app calls `loadUrl(launchUrl)` in `onCreate` (`CordovaActivity.java:281-289`; its documented template, `:67-71`; the lab's `MainActivity.java:180`).
- **So every CordovaActivity has its own instance before its first `onResume`,** and `PluginManager.onResume` reaches it (INFERRED from the source above; the fix's capture will show it).
- **On its own,** change 1 makes WO-003's `onResume` run on every CordovaActivity resume, a recreation's included. That closes the SUSPECTED second ordering above wherever it occurs (INFERRED): a relaunch inside the 700 ms after a destroy that came early, as Capacitor's Lab1 plain finish did, or a relaunch while another of the app's Activities is started. On Lab2 the measured plain-finish timing leaves a single-Activity relaunch no such window (INFERRED), so this is not what change 1 is for: change 2's record needs it (D1).
- **Unchanged:** `pluginInitialize` (`CDVBackgroundFetch.java:23-27`) only calls `BackgroundFetch.getInstance`, which is idempotent (`BackgroundFetch.java:58-69 (399765c)`). It now runs at page load instead of at the first `exec`.

**Change 2: WO-007's rule in `CDVBackgroundFetch.java`.**
- **The record:** a static `WeakReference<Activity>` holds the CordovaActivity resumed most recently. `onResume` (`:152-158`) sets it to `cordova.getActivity()` and keeps its `setHeadless(false)`.
  - It is static because each CordovaActivity has its own instance (above).
  - It is weak so that it never keeps a destroyed Activity alive.
- **The test:** `onDestroy` (`:160-170`) goes headless only when `cordova.getActivity()` is that Activity AND it is not changing configurations.
- **This is Capacitor's condition** (`capacitor/background-fetch/android/src/main/java/com/transistorsoft/bgfetch/capacitor/BackgroundFetchPlugin.java:38-66`), in Cordova's hooks.
- **The cases:**
  - **A plain finish of an Activity whose plugin saw it resume:** the destroyed Activity is the current one, so the SDK goes headless. Unchanged: `stop-on-terminate` `finish` and `headless-activity-destroyed`.
  - **A plain finish of an Activity that built its WebView after its first resume** (`init()` or `loadUrl` called late): its plugin never recorded it, so the SDK does not go headless until that Activity's next resume, where today's code does. See the Scope check and D1 (a′).
  - **A recreation:** WO-003's guard still applies. Unchanged: `recreate-foreground-android`.
  - **A self-restart:** the destroyed Activity is no longer current, so the destroy is ignored. This is the defect.
  - **A destroy before the relaunch resumes:** the SDK goes headless, then the new instance's `onResume` clears it. Change 1 is needed for this only inside the SUSPECTED window above. After ProcessLifecycleOwner's ON_STOP, its ON_START clears the flag on today's plugin too (INFERRED).
- **Comments:**
  - WO-003's comment at `:155-156` says Cordova creates the plugin on its first exec, so a recreation's new Activity resumes before it exists and "onDestroy's check below is what covers that". Under D1 (a) the premise is no longer true: the new instance's `onResume` also runs on a recreation. So the comment changes with the code.
  - The comment at `:162-164` changes because change 2 adds the current-Activity condition to the `if` below it. It then names both destroys that are not a termination, as Capacitor's does (`BackgroundFetchPlugin.java:53-60`). Under D1 (a) its reason also narrows: going headless on a recreation would misroute only a fetch inside the few milliseconds before the new instance's `onResume` (Not an acceptable shortcut).
- **No Gradle change is expected.**
  - `WeakReference` is in the JDK.
  - The class already calls `cordova.getActivity().isChangingConfigurations()` and `LifecycleManager`, and the lab compiled it (MEASURED: sweep `20261115-215127` built `368eb85`). WO-007's compile surprise came from Capacitor's classpath (WO-007 Log, 2026-11-13).
  - It also needs no native release. `setHeadless` is public in every `4.1.+` the plugin resolves (`src/android/build.gradle:6`; `LifecycleManager.java:86 (399765c)`).

**Anchors:**
- **Branch:** `fix/WO-010-cordova-android-restart-ordering` in `cordova/background-fetch`. The parent repo's lab prose goes on a branch of the same name.
- **Code:** `// (WO-010)` at the static field, `onResume` and `onDestroy`; `<!-- (WO-010) -->` at the `plugin.xml` param.
- **CHANGELOG:** an `[Android]` entry under the plugin's `## Unreleased`, for example:

  > * [Android] An app that restarts itself (`finish()` then `startActivity()`, or a relaunch within a second of a finish) no longer leaves the plugin in headless mode with the new page on screen. The old Activity's destroy arrives after the new one resumes, and it put the plugin in headless mode, so the page's next fetch was stopped (`stopOnTerminate: true`) or sent to the headless task (`false`). The plugin now loads at startup on Android, as it does on iOS. (WO-010)

- **The scenario:** `restart-foreground-android.yaml`'s Cordova paragraph (`:45-54`) already names WO-010 and the committed sweep `20261115-215127` with its times 36.324 / 36.351 / 36.800 (WO-002's 2026-11-16 entry). The fix adds its green sweep there. This is prose only.

**Decisions:**

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | The record of the current Activity | (a) `onload` on Android (change 1), with the record set in `onResume` (change 2). (a′) As (a), but an empty record counts as current, and `onDestroy` clears the record when it names the destroyed Activity. That keeps today's plain finish for an app that builds its WebView after its first resume (Scope check), and also keeps today's restart defect for that app. Without it, that path's regression is recorded as accepted. (b) Keep lazy creation. Register `Application.ActivityLifecycleCallbacks` once, from the first `pluginInitialize`, and record the last resumed `CordovaActivity`. Because the first Activity resumes before the first exec (MEASURED), an empty record must count as "current". (c) The native `LifecycleManager` tracks Activities for every bridge. (d) WO-012's Flutter rule in place of change 2's record: a static count of live instances, raised in `pluginInitialize` and lowered in `onDestroy`, which goes headless only when no instance is left and the Activity is not changing configurations. Change 1 stays: a lazy new instance may not exist at the old destroy (Not an acceptable shortcut). On the page-reload route `PluginManager.init` destroys the old instance before `startupPlugins` creates the new one (`PluginManager.java:95-98`), so the count reaches zero and goes headless there, as today and under (a) (the order PROVEN, the outcome INFERRED; D3). | (a). Every CordovaActivity's first resume reaches its own instance, which is what change 2's record needs. It is Capacitor's shape and condition, and it adds no process-wide callback. It moves `pluginInitialize` from the first exec to page load. It would also close the SUSPECTED destroyed-first relaunch, but that is no deciding advantage: on Lab2 a single-Activity relaunch has no window (Diagnosis, INFERRED). (a′) and (d) are no worse than today on the late-WebView path (INFERRED), at the cost of a condition that differs from Capacitor's; Chris's call. WO-012 recommends (d)'s rule for Flutter because ActivityAware has no resume callback, so a record there would need `ActivityLifecycleCallbacks` or a new dependency; it says the two orders need not match. Cordova has `onResume`, so it can take WO-007's literal rule, the condition Capacitor's plugin uses (unreleased, under its `## Unreleased`) and the lab measured green. (d) asks whether any page is left, not whether the destroyed Activity is the one on screen. (b) needs a callback that is never removed, a filter on `CordovaActivity` (an app hosting a CordovaWebView in its own Activity would never match: NOT CHECKED), a condition unlike Capacitor's, and its own `setHeadless(false)` on resume to close the second ordering. (c) is in Not an acceptable shortcut. |
| D2 | Where it ships | (a) The plugin's next release. Its `## Unreleased` already holds WO-003's Android entry and WO-001's iOS entries (`CHANGELOG.md:3-10`), independent of WO-009. (b) Hold it for whatever release carries WO-009's JS API decision. | (a). The fix is Android-only and changes no JS API. WO-003, also unreleased, fixes the same hook. |
| D3 | The page-reload route (Scope check, SUSPECTED): `PluginManager.init` runs `onDestroy` with the same Activity live | (a) Its own work order, once a scenario measures it. (b) Fold it in here, with a test that the Activity is being destroyed (`isFinishing()` or `isDestroyed()`; the framework's behaviour INFERRED, not read). | (a). It is unmeasured, it has a different trigger (no Activity destroy), and change 2 does not make it worse. |
| D4 | Measuring the SUSPECTED destroyed-first relaunch, which no scenario asserts | (a) WO-007's `adb` probe on the Cordova host, outside labkit, before and after the fix, with the relaunch timed into the window (after the old destroy, before ProcessLifecycleOwner's ON_PAUSE) and counted only if the unfixed build reads red (Test plan). (b) Also a WO-002 follow-up step asserting `restartOrder destroyed-first`, under the same precondition: outside the window it would pass on every build. (c) Neither: the case stays SUSPECTED. | (a), on Lab1, where a plain finish's destroy has landed inside the 700 ms (Capacitor, MEASURED). WO-007's fixed 1.5 s relaunch comes after ON_STOP, gets ON_START, and would read green before and after the fix (INFERRED), so it shows nothing about change 1. If no run reaches the window, record that and leave the case SUSPECTED. (b) is WO-002's call. |

## Scope check

- **Who calls `CDVBackgroundFetch.onDestroy`:** `PluginManager.onDestroy` (`:331-339`), from three places (PROVEN, grep of cordova-android 15.0.0):
  1. **`CordovaWebViewImpl.handleDestroy` (`:526-534`)**, on every CordovaActivity destroy (`CordovaActivity.java:371-378`). This is the route change 2 filters.
  2. **`PluginManager.init` (`:92-99`),** on every page load after the first: `loadUrlIntoView` with a URL already loaded (`CordovaWebViewImpl.java:128-142`). JavaScript reaches it through `navigator.app.loadUrl` (`labs/platforms/cordova/app/node_modules/cordova-android/templates/project/assets/www/cordova.js:1553-1555` → `CoreAndroid.java:103-104`, `:170-220` → `showWebPage`, `CordovaWebViewImpl.java:214-228`).
     - The Activity stays current and is not changing configurations. So the plugin goes headless today, and still will after change 2. SUSPECTED, NOT MEASURED: D3.
     - Under D1 (a), the new onload instance is created at once (`startupPlugins`) but gets no `onResume` until the next one.
  3. **`setPluginEntries`** on an initialized PluginManager (`:74-87`).
- **Who calls `onResume`:** `PluginManager.onResume` (`:292-300`), on every CordovaActivity resume (`CordovaWebViewImpl.java:495-502`).
  - Under D1 (a), that includes each Activity's first resume.
  - `CordovaActivity.onResume` skips plugins while `appView` is null (`:327-329`), that is before the app calls `init()` or `loadUrl` (`:281-284`).
  - **The consequence (INFERRED):** an app that calls `init()` or `loadUrl` after its Activity's first resume creates the onload instance after that resume. The record then stays empty, or names another Activity, until that Activity's next resume. With Capacitor's condition (`activity != null && activity == sResumedActivity.get()`, `BackgroundFetchPlugin.java:62`), a plain finish in that span would not go headless, where today's code does (`CDVBackgroundFetch.java:165-168`). That is WO-007's defect, on that path. D1 (a′) or (d) keeps today's behaviour there; otherwise it is an accepted regression.
  - The template's `cdvStartInBackground` path (`moveTaskToBack(true)` in `onCreate`, lab `MainActivity.java:174-178`) is another route where a resume may be missed: NOT CHECKED.
  - The lab host loads in `onCreate` (`MainActivity.java:180`), so no cell would catch either.
- **Who reads the flag:** `doFetch` (`BackgroundFetch.java:272 (399765c)`) and `BGTask.onTimeout` (`BGTask.java:207 (399765c)`). A restart's fetch and timeout both reach the page after the fix (INFERRED).
- **What onload changes besides the record (INFERRED):**
  - The instance exists from page load, on the UI thread. It no longer waits for the first `exec`.
  - The SDK's `LifecycleManager` is registered at app start (`BackgroundFetch.java:76-80 (399765c)`) instead of at the first JS call.
  - An app that returns from a non-Cordova Activity to a CordovaActivity the system rebuilt now gets `setHeadless(false)` at that resume. Today nothing clears the flag there unless ProcessLifecycleOwner sends ON_START.
- **Several CordovaActivities:** only the destroy of the one resumed most recently makes the SDK headless. A non-Cordova Activity on top does not update the record. So the last CordovaActivity's destroy still goes headless, which is right: no page is left. INFERRED, NOT MEASURED.
- **What customers see:** a self-restarting app's fetch reaches the new page's `configure()` callback, as on React Native and Capacitor. A recreation behaves as today, and so does a plain finish of an Activity that loaded its page in `onCreate`. A plain finish of an Activity that built its WebView after its first resume behaves as today only under D1 (a′) or (d) (above).
- **Unchanged:**
  - the JS API (`src/typescript/`, `www/index.js`) and the types;
  - the iOS side (`src/ios/`, `plugin.xml:27-62`);
  - the native library.
- **NOT CHECKED:**
  - cordova-android older than 15.0.0: the plugin declares `>=7.0.0` (`plugin.xml:16`), and change 1's onload-before-first-resume order was read only in 15.0.0;
  - the Cordova plugin hosted by Capacitor's Cordova compatibility layer, which has a plugin of its own;
  - apps that host a CordovaWebView without `CordovaActivity`;
  - how often apps restart themselves.

## Platform parity

- **iOS: N/A,** because iOS has no headless mode and no Activity recreation or restart ordering to get wrong.
  - TSBackgroundFetch's `stopOnTerminate` (`native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.h:20 (399765c)`) is only logged in `onAppTerminate`, which does nothing on purpose (`TSBackgroundFetch.m:433-438 (399765c)`). The file has no headless state.
  - Cordova's iOS plugin has no destroy or headless hook: `pluginInitialize` (`src/ios/CDVBackgroundFetch.m:42-49`) and `dealloc`, which removes its listener (`:192-195`). It is already `onload` (`plugin.xml:46`).
  - The scenario is `not_applicable` on iOS (`restart-foreground-android.yaml:56-57`). The types mark `stopOnTerminate` and `enableHeadless` Android-only (`types/background-fetch/src/interfaces.ts:12`, `:39`).
- **Android, React Native: not affected.** MEASURED green (sweep `20261113-104449`, Lab1, `8d9ab19`).
  - The module's `onHostDestroy` has Cordova's unfiltered condition (`RNBackgroundFetchModule.java:116-126`).
  - React Native's framework calls it only for its current Activity. `ReactDelegate.onHostDestroy` passes the Activity (`ReactDelegate.kt:161-169`). `ReactHostImpl.onHostDestroy(activity)` acts only when it `===` `currentActivity` (`ReactHostImpl.kt:309-318`), which `onHostResume` sets (`:246-249`).
- **Android, Capacitor: not affected.** MEASURED green (sweep `20261113-104449`, Lab2, `4d431c1`). WO-007's rule is at `BackgroundFetchPlugin.java:38-66`. A Capacitor plugin loads with its Bridge, so its `handleOnResume` sees its Activity's first resume (WO-007: `load` before `App resumed`).
- **Android, Flutter: affected by the same rule.** MEASURED red on both steps (sweep `20261113-183048`, Lab2, `11d53b8`). That is WO-012's.
- **Android, apps using TSBackgroundFetch directly:** they make their own `setHeadless` calls. NOT CHECKED.

## Test plan

- **Red before the fix:** already MEASURED in three sweeps (above). Nothing needs to be re-run first.
- **On the fix branch, Cordova Android, Lab1 and Lab2.** The lab stages the checkout `lab.yaml`'s `default_checkout` names, and `run.json` records its commit and dirty flag. No sweep has run this scenario on Lab1 for Cordova, so run it there by `labkit run` if the sweep's lane puts it on Lab2 again.
  - **`restart-foreground-android`, both steps:** the scenario's values as they stand.
    - `restartOrder resumed-first`, `appProcesses 1`;
    - `driverEvents fetch=1`, `driverTally fetch:cordova-background-fetch=1`, `eventAssertions ok`;
    - `fetchReceived 1`, `fetchFinished 1`, `jobFinished 1`, `stoppingOnTerminate 0`;
    - `headless false`, `jobIds 999`, `jobsCancelled 999` (boot 2's reschedule);
    - in `stop-on-terminate-false`, also `headlessEvents.fetch 0` and `fetchTimeouts 0`.
    - No `☯️  HeadlessMode? true` after the trigger, so `lifecycleTally` has no `headless:true`.
  - **Under D1 (a), proof that onload took effect:** read it in those captures. On boot 1 and boot 2, `getPlugin - put: BackgroundFetch` comes before `Resumed the activity.`, and no `startupPlugins: put - BackgroundFetch` line appears.
  - **`stop-on-terminate` `finish` and `headless-activity-destroyed`:** still green, each with exactly one `☯️  HeadlessMode? true`, between `point=onDestroy` and `onDestroy:afterSuper` (today `finish/logcat.txt:488`, `:492`, `:495` and `headless-activity-destroyed/FIX/run/logcat.txt:543`, `:546`, `:550`). Under D1 (a) these prove that the record names the Activity on boot 1. Under (a′) or (b) an empty record also counts as current, so they prove less.
  - **`stop-on-terminate` `kill`:** still green, the SDK's alone (`stop-on-terminate.yaml:31`). Its one `☯️  HeadlessMode? true` comes at the job's new process's start, not at a finish: `kill/logcat.txt:644`, pid 30857, 54 ms after that process's `☯️  onCreate` (`:637`), from `LifecycleManager`'s 50 ms start-up timer (`LifecycleManager.java:46-50`, `:127-140 (399765c)`). That process runs no CordovaActivity, so this change does not touch it (INFERRED), and it says nothing about the record.
  - **`recreate-foreground-android`, both steps:** still green, with no `headless:true` in `lifecycleTally`. Under D1 (a) that line, recorded and not asserted, is the only evidence that WO-003's guard held (Not an acceptable shortcut).
- **Regression: the full Cordova Android Tier 1 sweep on the fix.** 12 of 13, with `api-smoke` red only for WO-009's finding, or 13 of 13 if WO-009's fix is in the swept code. WO-009 changes the same plugin (`CDVBackgroundFetch.java:47-49` among its files), and either order can land first, so the Log records each sweep's plugin stamp (`run.json`'s `plugin_source`) and any other order's code in the checkout. React Native and Capacitor code does not change, so they need no re-sweep. Flutter stays red here until WO-012.
- **D4 (a):** WO-007's probe on the Cordova host, outside labkit, before and after the fix, on Lab1.
  - Launch with `stopOnTerminate: false` and `enableHeadless: true`, then `am start --ez finishNow true`, then relaunch. Then `cmd jobscheduler run -f … 999`.
  - **0.1 and 0.3 s** re-confirm the resumed-first case the scenario already reads. After the fix the fetch must reach the page.
  - **The destroyed-first case:** choose each delay from the run's own times, so that the relaunch resumes after the old `point=onDestroy` and before ProcessLifecycleOwner's `☯️  onPause` (the old `Paused the activity.` + 0.70 s). A fixed 1.5 s relaunch comes after ON_STOP and gets ON_START, so it should read fetch → page on both builds (INFERRED). WO-007's 1.5 s rows could not tell that clear from `handleOnResume`'s either (`WO-007-capacitor-android-destroy-not-headless.md:306-307`, `:323`).
  - **Such a run counts only if the unfixed build reads red:** `☯️  HeadlessMode? true` at the old destroy, no `☯️  onStart` or `☯️  onResume` between it and the fetch, and the fetch at `point=headless`. Then, in the same window, the fix must send the fetch to the page. `setHeadless(false)` logs nothing (`LifecycleManager.java:86-90 (399765c)`), so the plugin's clear is read from where the fetch lands.
  - **Otherwise** the run is logged as "window not reached", not as green, and the case stays SUSPECTED.
- **The lab: no expectation or `platform_expect` change.** The Cordova row (`restart-foreground-android.yaml:64`) stays.
  - This work order makes prose-only lab edits, as WO-007 did:
    - the scenario's Cordova paragraph adds the green sweep (it names WO-010 already);
    - under D1 (a), the two lab texts that say the plugin is created on its first exec: `labs/platforms/cordova/lab.yaml:66` and `labs/platforms/cordova/app/native/MainActivity.java:35-37`.
  - A `destroyed-first` step (D4 (b)) would be WO-002's, under D4's precondition.
- **No unit test.** The plugin has no test sources, and none of its scripts (`build`, `prepublishOnly`, `preflight`, `release`; `package.json`) compiles or tests the Java. `scripts/preflight.sh` checks the TypeScript build and the tarball, not the Java. The lab cells are the test, and the scenario's description carries the id.

## Out of scope

- **The sibling work orders drafted alongside this one:**
  - **WO-009** (`WO-009-cordova-callback-api.md`): Cordova's JS API is callback-style. `start`/`stop`/`status`/`scheduleTask` return nothing where the types declare Promises; `stop(taskId)` throws on iOS and cancels every task on Android; on iOS, `start`'s native half is unordered against a later `stop`. Its `api-smoke` red does not depend on this fix.
  - **WO-011** (`WO-011-flutter-stop-returns-int.md`): Flutter's `stop()` is `Future<int>` and resolves the status, where the types declare a boolean.
  - **WO-012** (`WO-012-flutter-android-restart-ordering.md`): Flutter Android fails `restart-foreground-android`. The plugin does not act on the current Activity alone (WO-007's rule), and its EventChannel is held process-wide rather than on the plugin instance. The same scenario, another bridge: no Flutter file changes here.
  - **WO-013** (`WO-013-stop-no-arg-scope.md`): `stop()` with no argument means different things on Android. There React Native and Capacitor stop only their own fetch task, while Cordova and Flutter cancel every task, as the types' doc says. On iOS all four bridges cancel every task. The scope with a second task alive is unmeasured; it needs a new lab scenario.
- **The page-reload route** (`PluginManager.init` → `onDestroy`), unless D3 (b).
- **A restarted page that never calls `configure()`.** `mFetchCallback` still holds the old instance's callback (`BackgroundFetch.java:85 (399765c)`), whose WebView is destroyed. The fetch would go there. This is WO-007's Out-of-scope item, on Cordova.
- **Every page boot's `configure()` reschedules job 999** (`Re-configured existing task`, `BackgroundFetch.java:88-95 (399765c)`; MEASURED `:635-636`), which restarts its interval. Recorded, not changed.
- **The headless task class:** `src/android/BackgroundFetchHeadlessTask.java` exists, but `plugin.xml` does not install it (WO-002's Cordova host supplies its own; suspected defect 3 covers a missing class).
- **Cordova iOS. Native headless detection** (D1 (c)).

## Not an acceptable shortcut

- **Porting Capacitor's condition while the plugin stays lazy.** `activity != null && activity == sResumedActivity.get()` (`BackgroundFetchPlugin.java:62`) needs a record that a lazy instance never makes.
  - In every Cordova capture read for this order, the plugin is created after its Activity resumed (MEASURED, for example `stop-on-terminate/FIX/finish/logcat.txt:161`, `:224`; `headless-activity-destroyed/FIX/run/logcat.txt:168`, `:251`). So a plain finish on boot 1 would find no record and never go headless.
  - That is WO-007's defect, moved to Cordova: `stop-on-terminate` `finish` and `headless-activity-destroyed` would read red (INFERRED).
- **Setting the record, or clearing the flag, in `pluginInitialize` or `configure()`** on a lazy plugin, or in `configure()` under either D1 option. That depends on the new page's first BackgroundFetch exec landing before the old destroy.
  - It did by 449 ms in `run` (36.351 → 36.800) and 447 ms in the other step (18.635 → 19.082). A page that calls `configure()` later, or not on its first screen, loses.
  - Clearing the flag there is undone anyway, because the old destroy comes afterwards.
- **Under D1 (a), setting the record in `pluginInitialize` (at creation) instead of `onResume`.**
  - The onload instance is created during the new Activity's `onCreate`: `startupPlugins` runs from `CordovaWebViewImpl.init` (`:119`), inside `loadUrl` (`CordovaActivity.java:281-289`; lab `MainActivity.java:180`). In a restart that comes before the old destroy (MEASURED today: `point=onCreate` 36.311, `:578`; `startupPlugins` 36.321, `:613`; the old `point=onDestroy` 36.800, `:780`). So every lab cell would pass: the host has one Activity class.
  - It fails on A → B → back, with two CordovaActivities. A resumes without being re-created, so a record set at creation still names B, and B's later destroy goes headless with A's page on screen. `onResume` names A there (INFERRED).
  - Capacitor sets its record in `handleOnResume`, not `load()` (`BackgroundFetchPlugin.java:42-49`).
- **Testing `isFinishing()`.** A restart's old Activity is finishing (MEASURED: `isFinishing=true`, `run/logcat.txt:780`).
- **Asking the system which of the app's Activities or tasks exist** (`ActivityManager`). 7.1.2 (2022-10-11) left that legacy mechanism, and its `GET_TASKS` permission, for `LifecycleManager` (`CHANGELOG.md:49-50`).
- **Delaying `setHeadless(true)` in `onDestroy`.** A fetch inside the delay goes to the dead page, and the order is still a race.
- **Removing `setHeadless(true)` from `onDestroy`, or WO-003's guard.**
  - Without the first, a plain finish's fetch goes to the destroyed WebView: WO-007's defect, which `stop-on-terminate` `finish` and `headless-activity-destroyed` pin.
  - Without the second, a recreation's destroy goes headless again. Under D1 (a) the new instance's `onResume` clears it 28 ms later in today's recreate capture (old `point=onDestroy` 02.751, new `Resumed the activity.` 02.779; `recreate-foreground-android/FIX/run/logcat.txt:488`, `:711`). Only a fetch inside that gap takes the headless branch. Both recreate steps fire the job 4 s after the trigger (`recreate-foreground-android.yaml:80`, `:107`), so they would still pass (INFERRED). Only `lifecycleTally` (`headless:true`) shows the missing guard, which is why the Test plan reads it (its `recreate-foreground-android` line). WO-007 recorded the same masking (`WO-007-capacitor-android-destroy-not-headless.md:156-159`, `:250`).
- **Fixing it in the native `LifecycleManager` for every bridge** without proving that every bridge's behaviour stays identical.
  - It would add a second writer of the flag beside each bridge's hooks.
  - It needs a native release, with the full Android Tier 1 green and unchanged on React Native, Capacitor and Cordova, and Flutter's board unchanged apart from WO-012's `restart-foreground-android` reds (its `api-smoke` red, WO-011's, stays). Flutter has been an Android lab bridge since 2026-11-13 (11 of 13, `WO-002-background-fetch-labkit.md:1182`), and its `setActivity` writes the flag too (`BackgroundFetchModule.java:103`, `:109`, from `BackgroundFetchPlugin.java:49`; `11d53b8`).
  - WO-003's and WO-007's D1 kept this in the bridges.
- **Changing the lab to avoid the red.** That includes:
  - a Cordova `platform_expect` relaxing `driverEvents`, `driverTally`, `eventAssertions`, `stoppingOnTerminate`, `fetchFinished`, `headless`, `headlessEvents.fetch` or `jobIds`;
  - a `not_applicable` for cordova, or dropping it from `platforms`;
  - delaying the host's relaunch until `restartOrder` reads `destroyed-first`;
  - the host or driver calling `setHeadless`;
  - setting `onload` in the lab app's own `config.xml`. That fixes the lab and no customer: the param must ship in the plugin's `plugin.xml`.

  The scenario's values are the ones React Native and Capacitor give on the same native label, and its description calls Cordova's red "a finding for its own work order" (`:54`) and names WO-010 (`:46`). Adding the green sweep there is prose only.

## Verification

- [ ] D1–D4 decided
- [ ] `restart-foreground-android` (both steps) green on the fix, Cordova Android, Lab1 and Lab2: `restartOrder resumed-first`, no `☯️  HeadlessMode? true` after the trigger
- [ ] Under D1 (a): `getPlugin - put: BackgroundFetch` before `Resumed the activity.` on boot 1 and boot 2, and no `startupPlugins: put - BackgroundFetch`
- [ ] `stop-on-terminate` `finish` and `headless-activity-destroyed` green on the fix, one `HeadlessMode? true` each, between `point=onDestroy` and `onDestroy:afterSuper`; `stop-on-terminate` `kill` green, its one headless line at the new process's start; `recreate-foreground-android` green with no `headless:true`
- [ ] Cordova Android Tier 1 sweep on the fix: 12/13, `api-smoke` red only for WO-009's finding, or 13/13 if WO-009's fix is in the swept code; the Log records each sweep's plugin stamp and any other order's code in the checkout
- [ ] D4 (a), on Lab1: at 0.1 and 0.3 s the fetch reaches the page after the fix; a destroyed-first run inside the window red before the fix and green after it, or recorded as "window not reached" (the case then stays SUSPECTED)
- [ ] `// (WO-010)` at the changed lines and `<!-- (WO-010) -->` in `plugin.xml`; the CHANGELOG's `## Unreleased` `[Android]` entry carries `(WO-010)`; the scenario's Cordova paragraph cites the green sweep (prose only; it names WO-010 already)
- [ ] Under D1 (a): `lab.yaml:66` and `MainActivity.java:35-37` no longer say the plugin is created on its first exec (prose only)
- [ ] Platform parity: iOS N/A as cited; React Native and Capacitor green, MEASURED; Flutter recorded for WO-012

## Log

- 2026-11-16 — **Created (draft)** from WO-002's lab board: `restart-foreground-android` red on Cordova Android.
  - **The sweeps:** `20261115-183522`, `20261115-190943` and `20261115-215127`, all Lab2, plugin `368eb85`, native `4.1.1-master-399765c`. The same 12 red metrics each time, in each sweep's local, gitignored `labs/harness/.claims/sweep-<id>/board.json`. Git keeps the summaries of `20261115-190943` (`1796d96`) and `20261115-215127` (`82d306e`); for `20261115-183522` only that it was red (Citation bases).
  - **The committed captures** (`labs/captures/cordova/restart-foreground-android/FIX/{run,stop-on-terminate-false}`, commit `82d306e`) are `20261115-215127`'s. They are not `20261115-190943`'s, as WO-002's Cordova Android entry said (corrected in its 2026-11-16 entry): `1796d96` held those, and the Cordova iOS slice's re-sweep replaced them.
  - **Controls:** React Native and Capacitor green on `20261113-104449`; Flutter red on `20261113-183048` (WO-012).
  - **Checked by hand while writing:**
    - the plugin at `368eb85`;
    - cordova-android 15.0.0's `PluginManager`, `CordovaWebViewImpl`, `CordovaActivity`, `CordovaInterfaceImpl`, `CordovaPlugin`, `ConfigXmlParser`, `CoreAndroid` and `cordova.js`;
    - React Native's module at `8d9ab19` and RN 0.88.0's `ReactHostImpl` and `ReactDelegate`;
    - Capacitor's plugin at `3fa169a`;
    - native `399765c`;
    - every capture line cited.
  - **WO-002's summary held:** the one cause, and lazy creation mattering only to a fix.
  - **Added here:**
    - the porting trap (Capacitor's condition on a lazy plugin);
    - the SUSPECTED destroyed-first relaunch;
    - the SUSPECTED page-reload route.
  - **Not done yet:** no branch, no code, nothing run.
  - **Reviewed (three lenses, three skeptics per finding): 29 findings, 25 upheld.** The substantive changes:
    - **The destroyed-first case** rests on plain-finish timing: no window on Lab2 (destroy after ON_STOP), possibly one on Lab1 (a Capacitor destroy at +0.38 s). Still SUSPECTED.
    - **Change 1 and D1 (a):** closing that case is no longer a deciding advantage.
    - **D4 and the Test plan:** no fixed 1.5 s relaunch; a run timed into the window counts only if red before the fix.
    - **`kill`** proves nothing about the record; only `finish` and `headless-activity-destroyed` do.
    - **Scope check:** the late-WebView plain-finish regression, with D1 (a′); cordova-android before 15.0.0 NOT CHECKED.
    - **Not an acceptable shortcut:** a record set at creation; the guard masked under D1 (a); Flutter in the native bar; `fetchFinished` in the lab list.
    - **Citations:** `board.json` is local and gitignored; `GET_TASKS` was dropped in 7.1.2, not 7.1.0.
  - **Cross-checked with WO-009..WO-013 (2026-11-16):**
    - **Sibling summaries:** WO-009's start ordering is iOS-only; WO-013's split is Android-only, with Cordova among the bridges that cancel every task.
    - **Sweep count:** 12/13 with WO-009's red, or 13/13 if its fix is in the swept code; the Log records each sweep's plugin stamp and any other order's code.
    - **D1 (d):** WO-012's Flutter rule (a count of live instances, with onload) added as an option; (a) kept, since Cordova has the resume hook Flutter lacks. The page-reload order read in `PluginManager.init`.
    - **The scenario:** its Cordova paragraph already names WO-010 and `20261115-215127`'s times (WO-002's 2026-11-16 entry), so the fix adds only its green sweep. The two "WO-002 says -190943" notes now say that entry corrected it.
