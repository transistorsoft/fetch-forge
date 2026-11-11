---
id: WO-002
title: background-fetch has no device lab. Nothing calls a bridge's API or fires a fetch or headless event on a real emulator or simulator. Build a LabKit for fetch on bg-forge's harness
status: in-progress
created: 2026-11-05
platforms: [android, ios, tooling]
submodules: [native/background-fetch, react-native/background-fetch, cordova/background-fetch, capacitor/background-fetch, flutter/background_fetch, types/background-fetch]
branch: feat/WO-002-fetch-labkit
issues: []
supersedes: []
---

> **Model:** bg-forge's device lab, LabKit: `/Users/chris/workspace/background-geolocation/bg-forge/labkit` and `bg-forge/labs/`. It is defined by bg-forge WO-026, WO-027 and WO-143, with board time-sharing from WO-106.
>
> **Where these facts come from:** two read-only research agents read the code on 2026-11-05.
> - **PROVEN (source):** the agents read it in code.
> - **INFERRED:** reasoning, not run.
> - **NOT CHECKED:** not looked at.
> - **SUSPECTED:** a defect inferred from source that no device run has reproduced.
>
> When it was written, nothing in this work order had been run on a device except WO-001's LLDB fetch on the iOS DemoApp. Device runs since are in the Log (from 2026-11-05: `termination-calibration` and the first slice on both Lab AVDs); a fact they settled is marked **MEASURED** where it is cited.

## Diagnosis

### What exists today (PROVEN, source)

| Asset | What it covers | What it can't cover |
|---|---|---|
| Android Robolectric suite: `native/background-fetch/android/tsbackgroundfetch/src/test/java/com/transistorsoft/tsbackgroundfetch/TSBackgroundFetchTest.java` (36 tests) | BGTask finish/timeout guards, jobId (999, `hashCode`, 0), prefs round-trip, stop all vs one, reconfigure | Every bridge, JobScheduler on a real OS, headless execution, process death, boot |
| iOS XCTest: `native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetchTests/TSBackgroundFetchTests.m` (39 tests) | TSBGTask persistence, main-queue callbacks, multi-subscriber finish counting driven through `performFetchWithCompletionHandler` (`:625-705`) | Every bridge, app lifecycle (UIScene), a real app process |
| Example apps per bridge, plus the native Kotlin and SwiftUI examples | Manual demos | Nothing is scored or repeatable |

**No test calls a bridge's public API and checks the result,** and no test fires an event into a running app. That is the gap bg-forge closed for background-geolocation with WO-026 (one lab per platform over a shared harness) and WO-027 (an API conformance suite).

### The regressions that a lab would have caught (PROVEN, source)

Every one of these shipped and was fixed in a bridge or native release. A unit test could not have caught any of them, because each needs a real process, lifecycle or build:

- **RN 4.4.2:** a cold-start race in headless mode (`HeadlessTask.java:135,158`). Also bridgeless headless (4.2.6) and the `isTaskRunning` guard (4.2.7). (Corrected 2026-11-09: 4.2.7's guard cannot be pinned on the lab's RN 0.88, whose `HeadlessJsTaskContext.finishTask` ignores an id it does not run, `HeadlessJsTaskContext.kt:141-152`.)
- **Flutter:**
  - 1.6.2: `MissingPluginException` after a relaunch while a foreground service kept the process alive.
  - 1.1.3: "FlutterJNI was detached" on the back button.
  - 1.2.0: a launch with the screen off was detected as headless.
  - 1.1.2: the release-build entry point `@pragma('vm:entry-point')`.
- **Capacitor 8.1.4:** `proguard-rules.pro` was missing from the npm package, so R8 stripped `BackgroundFetchHeadlessTask` in release builds.
- **Cordova:** 7.1.3, `onDestroy` → `setHeadless(true)`; 7.2.3, a missing `scheduleProcessingTask` argument.
- **Native Android:**
  - `stopOnTerminate` didn't cancel jobs (2020; 786b557: `save()` skipped a `stopOnTerminate:true` config. The same commit added a silent "no config: cancel by jobId" fallback, so "the job is gone" alone does not catch a revert);
  - an NPE after upgrading (2020);
  - `onBoot` didn't re-arm AlarmManager tasks (2020);
  - headless detection moved to `LifecycleManager` (2022);
  - Android 14 exact alarms (2023);
  - the 5 s duplicate-job guard (2023).
- **Native iOS 4.1.0:** BGTask handlers auto-register from `+load` (UIScene hosts depend on it), `TSBGTask` delay and type persistence, and subscriber counting.
- **Types / docforge:** the `NETWORK_TYPE_UNMETERED`, `NOT_ROAMING` and `CELLULAR` constants were transposed (types `7be684d`, docforge `37af5f2`). (Corrected 2026-11-09: in documentation only — the types' JSDoc `Value:` and table for a day, 9c296a8 → 7be684d, and docforge from 2cec8ae to 37af5f2. No bridge ever shipped a wrong runtime value; the live site was republished 13 minutes after 37af5f2.)

### Suspected defects for the lab to confirm or rule out (SUSPECTED; none reproduced)

Each one that a scenario confirms becomes its own work order.

1. **Capacitor iOS `start()`** is declared `CAPPluginReturnCallback` (`capacitor/background-fetch/ios/Sources/BackgroundFetchPlugin/BackgroundFetchPlugin.swift:13`), so it probably resolves a callbackId, not a status.
2. **Android `start()` after `stop()` does nothing.** `stop` destroys the config (`BackgroundFetch.java:166-173`), so `registerTask` logs "failed to find BackgroundFetchConfig" (`:249-251`). Every bridge still resolves `start()`.
3. **A headless class that can't be loaded strands the BGTask.** This happens with a missing Cordova class, a Capacitor `applicationIdSuffix`, or an R8 strip. The BGTask is never finished (`BackgroundFetch.java:280-284`, `BGTask.java:215-219`). (Corrected 2026-11-09: the error does not repeat every 60 s. The timer is one-shot (`BGTask.java:85`), so it logs at delivery, once at +60 s, and again only on onStopJob; the stranded BGTask then shadows later tasks with the same taskId, `BGTask.java:32-39`.)
4. **Capacitor Android has no `onDestroy`/`setHeadless` hook.** With the Activity finished but the process alive, events go to a dead bridge and are never finished. `ProcessLifecycleOwner` never dispatches `ON_DESTROY` (`LifecycleManager.java:161-166`).
5. **Flutter headless burst:** a second event during isolate start-up is stranded until it times out (`flutter/background_fetch/android/.../HeadlessTask.java:114-123`).
6. **iOS stale subscriber:** a subscriber persisted from a previous run, now without a callback, blocks completion until the task expires (`TSBGAppRefreshSubscriber.m:27-43,133-134`, `TSBackgroundFetch.m:358-368`).
7. **iOS reconfigure is ignored:** a new `minimumFetchInterval` is dropped by the `fetchScheduled` guard (`TSBackgroundFetch.m:104`). Flutter's Dart `configure` also binds its callbacks only once (`lib/background_fetch.dart:442`).
8. **The Swift API's `@discardableResult` `onFetch`/`scheduleTask`** remove the listener or stop the task at once if the caller doesn't keep the result (`SwiftInterface/BackgroundFetch.swift:132,147-149,189,210-212`).
9. **A `delay` above 2³¹−1 ms** breaks the `getInt`/`getInteger`/`Integer` casts in RN, Capacitor and Flutter.
10. **RN: a configuration-change recreation leaves the SDK headless** (added 2026-11-09, INFERRED from code). React Native runs the old Activity's `onHostDestroy` on a recreation, and the plugin calls `LifecycleManager.setHeadless(true)` (`RNBackgroundFetchModule.java:113-117`). Nothing resets it: `onHostResume` does not (`:98-103`), and `ProcessLifecycleOwner` sends no ON_START/ON_RESUME when the new Activity resumes within 700 ms. The next fetch, with the app on screen, is then stopped (`stopOnTerminate: true`, the default: `- Stopping on terminate`) or silently stranded (`false`: `HeadlessTask.java:68-70`). `recreate-foreground-android` measures it. **MEASURED** red 2026-11-09 on Lab1 (`Stopping on terminate` with the app on screen): **WO-003**.
11. **iOS: a timed-out fetch is reported to the OS as a success** (added 2026-11-10; PROVEN in source, legacy path MEASURED, device path NOT MEASURED). Once every subscriber has finished, the SDK's `finish` calls `setTaskCompletedWithSuccess:YES` on a BGAppRefreshTask, or the legacy handler with `UIBackgroundFetchResultNewData`, however the fetch ended (`TSBackgroundFetch.m:368-377`). So a fetch the plugin finishes from its onTimeout is reported as a success, which may skew iOS's scheduling of later refreshes. `ios-timeout-sim` measured the legacy half on the Simulator (`newData`, SpringBoard `RetrievedNewData`). The device half needs the manual tier.

### How events can be forced (PROVEN from source; each device command is INFERRED until a lab has run it — those marked MEASURED have, see the Log)

Fetch differs from background-geolocation in one fundamental way: **the OS schedules events; no API call causes them.** A lab therefore needs host actions that fire them.

**Android (emulator and device)**

| Event | How to force it |
|---|---|
| Periodic fetch | `adb shell cmd jobscheduler run -f <pkg> 999`, the fetch job id (`BackgroundFetchConfig.java:265,302-308`). Wait ≥ 1.5 s after `configure`: runs < 1 s after `scheduled_at` are ignored (`FetchJobService.java:24-31`). Space repeats ≥ 5 s apart: duplicates are dropped (`:38-51`). **MEASURED** (RN, Android 16): two forced runs 6.4 s apart, two deliveries (`events-fetch-android`). |
| Custom task | `run -f <pkg> <Java String.hashCode(taskId)>`. The log line `registerTask: <id> (jobId: N)` gives the id. |
| Timeout | Withhold `finish` for the SDK's 60 s (`BGTask.java:28,80-85`), or `cmd jobscheduler timeout <pkg> <jobId>` (API 28+; NOT CHECKED on the lab image) → `onStopJob` (`FetchJobService.java:63-76`). |
| Headless | `stopOnTerminate:false`, `enableHeadless:true`. Kill with `run-as <pkg> kill -9` (bg-forge's `AndroidBackend.kill`), then `run -f 999`. **MEASURED** (RN, Android 16, both Lab AVDs): a new process, the RN headless task, finish, `jobFinished` (`termination-calibration`). `am force-stop` instead cancels job 999. |
| Activity destroyed, process alive | bg-forge's `finishNow` (`trigger: finish`), then `run -f 999`. (Corrected 2026-11-09: not `KEYCODE_BACK` — on Android 12+ Back moves a root launcher Activity's task to the back without destroying it, INFERRED; the harness has no back-key cause either.) |
| Boot | `adb reboot` with `startOnBoot:true, stopOnTerminate:false, enableHeadless:true`. Only that combination persists the JobInfo (`BGTask.java:147`). |
| Package replaced | `adb install -r`, which triggers `BootReceiver` via `MY_PACKAGE_REPLACED`. |
| AlarmManager (`forceAlarmManager`) | adb **cannot** fire it, because the receiver is not exported. Wait for it on time; an exact alarm needs `USE_EXACT_ALARM` in the lab app (`BGTask.java:180-189`). |

**iOS**

| Where | What works |
|---|---|
| Simulator | `BGTaskScheduler` is unavailable there; the SDK falls back to the legacy API (`TSBackgroundFetch.m:117-122`). A **lab hook** in the app must drive events: `[[TSBackgroundFetch sharedInstance] performFetchWithCompletionHandler:applicationState:]` for a fetch; `[TSBGAppRefreshSubscriber onTimeout]` for a timeout; `[[TSBGTask get:id] execute]` / `onTimeout` for a custom task. **PROVEN** in this repo on 2026-11-05: LLDB calling `performFetchWithCompletionHandler:` on the DemoApp delivered the event and the finish. |
| Device only | LLDB `_simulateLaunchForTaskWithIdentifier:` / `_simulateExpirationForTaskWithIdentifier:`. A manual tier. |
| Not available | iOS has no headless mode. `stopOnTerminate` is a no-op property (`TSBackgroundFetch.m:388-393`). NOT CHECKED: whether any `simctl` command can fire a real BGTask. |

### How bg-forge's LabKit is built (PROVEN, source)

**Size and scope**
- One Python harness: 22.5k lines. `labkit.py` alone is 11.5k: about 4.8k of library and 6.7k of offline selftest, with 727 checks.
- 79 scenario YAMLs and 15 variants.
- Five platforms (RN, Cordova, Capacitor, Flutter, and native Kotlin/Swift since WO-143), each run on Android and iOS: 10 cells.
- Four Lab devices: AVDs Lab1/Lab2 on emulator-5580/5582, and iPhone 17 Pro Simulators Lab1/Lab2 on iOS 27.2.
- About 15 GB on disk, mostly regenerable.

**How a platform plugs in.** A platform contributes only `app/`, `build.sh <native label> <plugin checkout>` and `lab.yaml`. Native SDK snapshots are pinned by label: `labs/m2` (Maven) and `labs/xcframeworks` (a CocoaPods `:path` pod), each with a provenance file.

**The lab protocol**
- Args go in as base64 launch payloads.
- One log vocabulary: `LAB app=` from the driver, `LAB point=` from native code.
- API hooks and event assertions are scored **on the host only**; the device never learns what is expected.
- A step ends on evidence, not on a timer.
- Every capture records which SDK and plugin it exercised.

**What can be reused for fetch**

| Classification | Components |
|---|---|
| Generic | the `labkit` CLI; `devices.py` (machine-wide claims in `~/Library/Caches/transistorsoft-lab/claims`); `lab_devices.py` + `avd/`; the comparator engine, `apiAssertions`, `eventAssertions` and score/report (`labkit.py:3697-4762`); `_shared/labapi.js` and `labevents.js`. `labevents.js`'s args-then-callbacks `arm()` (`:195-197`) already fits `configure(config, onEvent, onTimeout)`. |
| Mixed | the spec loader, Capture, METRICS, Grace, `backends.py`, `run.py`, `sweep.py`, `summarize.py`, `lab_setup.py`, the native publish scripts. They carry background-geolocation names at known seams: `SDK_TAG`/`SDK_SUBSYSTEM` (`backends.py:39-40`), the iOS tap predicate (`:1291`), `Capture.sdk` reading `transistor_log.db` (`labkit.py:3193-3215`), `STANDING_FIX` and geo moves, `FGS_PROMOTED` (`:4428`), permission and dialog tables (`backends.py:732-806`), the `SUITE` lists (`sweep.py:64-165`), `assert_native` (`run.py:307`), eviction (`run.py:708-714`), `lab_setup` `SUBMODULES`/`published` (`:42-45,227-239`), and `publish_native.sh:38-40`. |
| Specific to background-geolocation | the vocabulary and its State/Config typing, roundtrip/`invalid_values`/declared, the 79 scenarios, platform apps and build scripts, and most of the selftest. |

**Fetch is small by comparison:**
- 7 methods, 10 flat config keys (`BackgroundFetchConfig`, checked against the types by the lab's selftest), no State object.
- 34 `docforge/db` entries, against background-geolocation's 518. All 34 carry `signatures:`.
- The SDK logs with the `TSBackgroundFetch` tag (`Log.d` on Android, `NSLog "[TSBackgroundFetch]"` on iOS); it has no SQLite log.

## Proposed fix

### Approach (DECISION D1)

**Recommended: vendor the core now, extract a shared core later.**

**Phase A (this work order):** build `fetch-forge/labs/` with bg-forge's generic harness files **vendored byte-identical**, with the bg-forge commit recorded in `labs/VENDORED.md`. Fetch-specific "packs" sit on top: vocabulary, metrics, SDK log adapter, host actions, platforms, scenarios and variants. They plug in through the seams listed in Diagnosis. Where a vendored file has to change, change it upstream in bg-forge first (a bg-forge work order) and re-vendor. Never fork it in place.

**Phase B (follow-up, separate work orders in both repos):** extract the vendored files into a shared LabKit core used by both forges (a repo or submodule), with each product as a pack. Gate: bg-forge's `summarize --all` re-scores all ~1,265 existing captures identically, plus one pilot board.

**Alternatives considered:**
- **(B) Extract the core first.** This is the right end state, but it means refactoring an 11.5k-line module while bg-forge WO-027 and WO-143 are in progress.
- **(C) Fetch as a second product inside bg-forge's labs.** Rejected; see "Not an acceptable shortcut".

### CLI and location (DECISION D2)

Use `fetch-forge/labs/` with a root `./labkit`, so bg-forge's commands and README carry over unchanged. The alternative is a `./forge lab …` subcommand.

### Lab devices: shared with bg-forge, never duplicated

**Same devices, same claims.** Use the same four Lab devices and the **same claims directory** and sweep lock (`devices.py:21-22`, unchanged). The two labs then queue instead of fighting over Lab1/Lab2.

**Distinct package ids and evict by prefix (needs a bg-forge change).**
- Fetch lab apps use package ids distinct from `com.transistorsoft.labs.<p>`, e.g. `com.transistorsoft.fetchlab.<p>`.
- Both labs must evict *every* Transistor lab app by package prefix, not just their own `platforms()` (the eviction loop, `run.py:708-714`).
- Why, corrected 2026-11-05 (review of the first slice):
  - A lab app left installed can deliver a fetch during the other lab's capture. Both labs' apps link TSBackgroundFetch.
  - bg-forge's contamination check (`devices.contamination`, `devices.py:167`, called at `run.py:403`) would **not** see it: it reads foreign launch marks and `app=` lines, and a headless fetch process logs neither (only `point=headless` / `point=task-…`).
  - It would show instead in bg-forge's headless metrics (`labkit.py:3171`, `:3378-3431`), which read `point=headless` / `point=task-api` lines from every process.
  - The risk is smaller than first written: run.py force-stops the app before and after every step and at exit, and force-stop cancels the fetch job (MEASURED, `termination-calibration`). A job is left behind only by a run that died before its cleanup.
  - Until eviction lands, the fetch pack scopes the SDK's lines to the app's own processes and scores every other process's SDK / headless / task lines as `foreignLines` (implicit 0) — fetch's side only; a bg-forge capture is not protected by it.
- This requires a bg-forge work order.

### Pack contents (fetch-specific)

**1. SDK log adapter**
- Android: the logcat tag `TSBackgroundFetch`.
- iOS: messages containing `[TSBackgroundFetch]`; there is no os_log subsystem.
- No DB pull.

**2. Vocabulary.** Seeded from `types/background-fetch` (methods, `BackgroundFetchConfig`/`TaskConfig`/`HeadlessEvent`, the `STATUS_*` and `NETWORK_TYPE_*` constants) and cross-checked against `docforge/db` signatures. No State tree; no roundtrip or `invalid_values` tables until a scenario needs them.

**3. Metrics:**
- fetch, timeout and finish counts per taskId;
- headless events;
- `jobFinished`;
- job scheduled/cancelled, read from `dumpsys jobscheduler <pkg>`;
- `Stopping on terminate`;
- `Caught duplicate Job`;
- completion-handler result;
- iOS `finish <id> (n of m)`;
- `crashes` (generic).

**4. Host actions.** Each is new unless marked reused. Built in the first slice: `job:`, `force_stop:`, and the reused `kill:`; the rest are for later slices.

| Action | What it does | State |
|---|---|---|
| `job:` | `cmd jobscheduler run -f <pkg> <jobId>`, honouring the ≥ 1.5 s and ≥ 5 s guards; `expect: present \| absent` says whether JobScheduler should hold the job, and its answer rides in the mark (`said=`). A dated `unspaced:` fires a repeat inside the 5 s guard on purpose, only where the step states `duplicateJobs` (Tier 1, `events-duplicate`) | built |
| `force_stop:` | `am force-stop <pkg>`, after reading the jobs JobScheduler holds (`held=`, the `jobsAtForceStop` metric) | built (added for `termination-calibration`) |
| `kill:` | Reused from bg-forge: `run-as kill -9` | reused |
| `job_timeout:` | `cmd jobscheduler timeout` | later |
| `back` | `KEYCODE_BACK` | later |
| `reboot` | `adb reboot`, then wait for boot | later |
| `reinstall` | `adb install -r` | later |
| iOS `fire:` | Sends the lab hook a command. **DECISION D3** picks the transport: `xcrun simctl openurl <udid> fetchlab://fire?...` handled by the app (INFERRED to work; unverified), or an `at_s` slot in the launch payload. | later (no iOS host yet) |

**5. Platforms: five**
- RN, Cordova, Capacitor, Flutter, and native Kotlin/Swift.
- Start from the existing example apps. The native SwiftUI example has no AppDelegate, so it needs an adaptor for the hook.
- Each host carries the lab hook (iOS) and the reflective probe of fetch state: Android `LifecycleManager.isHeadless`, the configs in SharedPreferences, and iOS subscribers and tasks in NSUserDefaults.

**6. Native pinning**
- Android: `publish_native.sh` adapted to `:tsbackgroundfetch:publishToMavenLocal`, reading the version from `android/versioning/tsbackgroundfetch.properties` first (`tsbackgroundfetch/build.gradle:103-108`).
- iOS: `build_native_ios.sh` adapted to the TSBackgroundFetch scheme. The simulator slice only, ad-hoc signed, as `scripts/build-ios.sh` already does.
- The Cordova build must defeat the plugin's hardcoded `4.1.+` floor, as bg-forge's does for its own.

**7. iOS delivery channel (DECISION D4).** WO-001 makes SwiftPM the main iOS channel. bg-forge's lab pins CocoaPods `:path` pods and disables SwiftPM.
- **Recommended:** add a variant axis (`channel: pods | spm`). That lets the lab run WO-001's open verification items: mixed channels, the one-channel rule, and the CocoaPods freeze.
- The local SwiftPM pin is `forge link-ios`'s local-path rewrite.

### Scenarios

**Tier 1 (the first board):**

| Scenario | What it pins |
|---|---|
| `api-smoke` | every method on every bridge and OS |
| `api-constants` | the transposed NETWORK_TYPE regression |
| `events-fetch-android` | |
| `events-initial-ignored` | |
| `events-duplicate` | |
| `events-task-android` | |
| `events-timeout-sdk` | |
| `termination-calibration` | see below |
| `stop-on-terminate` | |
| `headless-kill` | |
| `headless-activity-destroyed` | suspected defect 4 |
| `ios-fetch-sim` | |
| `ios-timeout-sim` | |
| `ios-task-sim` | |
| `ios-registration` | UIScene hosts |

`termination-calibration` comes **first**. It compares `am force-stop` with `run-as kill -9` and decides how every headless scenario terminates the app.

**Tier 2:**
- `config-jobinfo-android`, `config-validation-android`
- `alarm-task-android`
- `stop-scope`, `stop-start` (suspected defect 2), `reconfigure` (7), `task-without-configure`
- `headless-kill-timeout` (note RN's 30 s HeadlessJS limit against the SDK's 60 s), `headless-burst` (5), `headless-api-calls`, `headless-missing-class` (3), `headless-relaunch-flutter`, `headless-cold-js-rn`
- `launch-screen-off-flutter`, `boot-persisted`, `package-replaced`, `release-build-headless` (R8 / `vm:entry-point`)
- `ios-fetch-before-configure`, `ios-multi-subscriber`, `ios-stale-subscriber` (6), `ios-stop-start`, `ios-capacitor-start` (1), `swift-overlay-subscription` (8)

**Manual device tier:** `ios-device-bgtask`, using LLDB `_simulateLaunchForTaskWithIdentifier` and `_simulateExpirationForTaskWithIdentifier`.

**Mutation variants** (`must_fail`, as in bg-forge): at least `MUT-no-finish` and `MUT-headless-class-missing`, to prove the scenarios can fail.

### Phases

| Phase | Work |
|---|---|
| 0 | Decisions D1–D4; the bg-forge prefix-eviction work order filed |
| 1 | Vendored core plus `labs/VENDORED.md`; the seam adapters; `./labkit doctor` / `up` / `status` / `down` working against the shared devices |
| 2 | Native pinning for both OSes; the five platform apps building against a pinned label |
| 3 | Tier 1 scenarios, `termination-calibration` first; first board (its captures' `summary.txt` + `run.json` committed); replay baselines kept locally; README |
| 4 | Tier 2; mutation variants; a work order filed for each confirmed suspected defect |
| 5 (separate work orders) | Phase B shared-core extraction |

## Scope check

**What this touches**
- New: `labs/` and `./labkit` in fetch-forge.
- Lab-only host code; no plugin or native source changes. Each plugin needs only what its lab host consumes: a published package or checkout, and `safeExtGet` version overrides that already exist.
- **Cross-repo:** bg-forge needs prefix eviction so the two labs can share devices safely. Every later change to a vendored file goes upstream first.

**What it reads:** `types/background-fetch` and `docforge/db` (vocabulary seed); `native/background-fetch` scripts, adapted rather than modified.

**Disk:** bg-forge's lab is about 15 GB, 8.6 GB of it `platforms/` build output. Keep `labs/platforms/*/app` build output, `labs/m2`, `labs/xcframeworks`, `labs/checkouts` and capture binaries gitignored, as bg-forge does.

**Repo size:** fetch-forge is 5.5 GB.

## Platform parity

| | Android | iOS |
|---|---|---|
| Fetch | forced through JobScheduler (`run -f`) | forced through the lab hook on the simulator; LLDB on a device |
| Custom tasks | forced through JobScheduler | lab hook (simulator); device |
| Timeout | the SDK's 60 s, or `cmd jobscheduler timeout` | lab hook (simulator); device |
| Headless, `stopOnTerminate`, boot, package-replaced, AlarmManager | first-class lab targets | N/A: iOS has no headless mode, and `stopOnTerminate` is a no-op (`TSBackgroundFetch.m:388-393`) |
| iOS-only scenarios | | lifecycle and registration (UIScene, `+load`), subscriber persistence |

**Not yet confirmed (NOT CHECKED):**
- whether a real BGTask can be fired on the simulator by any means other than the lab hook;
- `cmd jobscheduler timeout` on the lab's android-36 image.

## Test plan

- **Selftest:** the harness's offline selftest (vendored core checks plus fetch-pack checks) passes with no devices. Every scenario and variant loads.
- **Termination calibration:** `termination-calibration` runs on both Lab AVDs, and its result is recorded in the Log before any headless scenario is trusted.
- **First board:** tier 1 on 5 platforms × 2 OSes, its captures' `summary.txt` and `run.json` committed (the scored result, as in bg-forge); the full-window capture set copied to `labs/baselines/<date>-<label>/` for `summarize.py --replay-end` / `--trace-diff` (gitignored and local, as in bg-forge). Each mutation variant fails as intended (`must_fail`).
- **Device sharing:** a bg-forge sweep and a fetch sweep started back to back on the same machine queue on the shared claims and neither capture is contaminated. This needs the bg-forge eviction work order.
- **Manual tier:** `ios-device-bgtask` on one physical iPhone, documented step by step.

## Out of scope

- Fixing any of the suspected defects. Each confirmed one gets its own work order.
- Phase B shared-core extraction: its own work orders in both repos.
- Changes to bg-forge's lab beyond the prefix-eviction work order.
- CI or cloud runners: the lab is a local Mac with emulators and simulators, as in bg-forge.
- Android physical devices.

## Not an acceptable shortcut

- **(C) Fetch as a second product inside bg-forge's labs.** Mechanically the cheapest (devices, claims and eviction are already unified, and TSBackgroundFetch is already linked into every background-geolocation lab app), but:
  - the harness is single-product by construction: one vocabulary singleton (`labkit.py:188-197`); a closed `platforms()` world that every API scenario must list or excuse (README `:379-386`), so 4–5 fetch platforms would force edits to all 79 background-geolocation scenarios and the reverse; a single `native:` label per variant;
  - `lab_setup` assumes `TSLocationManager.xcframework` (`lab_setup.py:229`);
  - the fetch sources, work orders and releases live in fetch-forge, so the lab's evidence would sit in a different repo from the code it judges.

  Doing C properly needs the same core/pack split as B anyway.
- **A plain fork of the harness** (copy, then edit freely). It recreates exactly the copy-adapted forks bg-forge WO-026 was written to end, and every hard-won rule (claims, Grace, preflight/App Nap, contamination, transport limits) would then be maintained twice. Vendored files stay byte-identical with a recorded commit.
- **Waiting for the OS to schedule real fetches.** `minimumFetchInterval` is at least 15 minutes and timing is up to the OS. Force events with JobScheduler or the lab hook.
- **Firing events inside the SDK's guards.** A `run -f` less than 1 s after scheduling, or a repeat within 5 s, is dropped on purpose (`FetchJobService.java:24-31,38-51`). A scenario that does this will show false failures. The one exception (2026-11-09) is a step that measures the guard and asserts the drop: each such `job:` carries a dated `unspaced:`, which the pack accepts only where the step states `duplicateJobs` as a number (`events-duplicate`). `events-initial-ignored` fires nothing inside a guard: the run it measures is JobScheduler's own, at configure().
- **`_simulateLaunchForTaskWithIdentifier` on the simulator.** It works on a device only; `BGTaskScheduler` is unavailable on the simulator. An iOS `scheduleTask` rejection on the simulator is expected (`BGTaskSchedulerErrorDomain` code 1), not a defect.
- **Using `am force-stop` to "terminate" in a headless scenario.** Force-stop cancels the app's fetch job (MEASURED 2026-11-05 by `termination-calibration` on both Lab AVDs: `Could not find job 999` afterwards), so a headless scenario would test nothing. Use `run-as kill -9`, which is what bg-forge's `kill` does and which keeps the job. Not yet measured: a custom task's job, and AlarmManager tasks.
- **A separate claims directory or sweep lock.** The two labs would then use Lab1/Lab2 at the same time and corrupt each other's captures.
- **Firing the AlarmManager receiver via adb.** It isn't exported. Scenarios must wait for the alarm on time.
- **Copying bg-forge's vocabulary typing** (State tree, roundtrip, `invalid_values`, declared). Fetch has no State object and 10 flat config keys. Seed from `types/background-fetch`.
- **Scoring from an SDK SQLite log.** Fetch has none. Use the `TSBackgroundFetch` logcat tag and the iOS `[TSBackgroundFetch]` messages.

## Verification

- [x] Decisions D1–D4 recorded in the Log
- [x] bg-forge prefix-eviction work order filed and done (bg-forge WO-191, `done` 2026-11-09: on-device eviction on Android and iOS, and D4)
- [x] `labs/VENDORED.md` names the bg-forge commit, and the vendored files are byte-identical with it
- [ ] `./labkit doctor` / `up` / `status` / `down` work against the shared Lab devices
- [x] Offline selftest passes
- [x] `termination-calibration` result recorded
- [ ] Tier 1 board green, or each failure linked to a work order; board captures (`summary.txt` + `run.json`) committed; full-window replay set saved under `labs/baselines/`
- [ ] Mutation variants fail as intended
- [x] Back-to-back bg-forge and fetch sweeps: no contamination (Android Lab1, api-smoke, 2026-11-08/09: bg-forge `20261108-133031`, fetch `20261108-133328` and `20261108-155938`, bg-forge `20261109-083338`, each PASS; iOS waits for phase 2's app)
- [ ] Manual iOS device tier run once and documented
- [ ] Each confirmed suspected defect has its own work order

## Log

- 2026-11-05 — created at Chris's request ("A LabKit implementation for background-fetch like bg-forge/labs").
  - Facts come from two read-only research agents (LabKit architecture with coupling classification; the fetch test surface, event triggers and regression history).
  - The only device evidence is from WO-001's session: an LLDB-driven `performFetchWithCompletionHandler:` on the iOS simulator DemoApp delivered fetch → finish.
  - Nothing else has been run.
- 2026-11-05 — **approved** by Chris ("approved") with the recommended decisions, and moved to in-progress on branch `feat/WO-002-fetch-labkit`.
  - **D1:** vendor bg-forge's harness core byte-identical, with the commit recorded (`49d38d47f`, bg-forge `master`), plus fetch packs; extract a shared core later.
  - **D2:** `fetch-forge/labs/` plus a root `./labkit`.
  - **D3:** iOS events through a lab hook triggered by `simctl openurl` (to be verified), with a launch-payload `at_s` slot as the fallback.
  - **D4:** a `channel: pods | spm` variant axis.
  - **First slice:**
    - the vendored core with `./labkit doctor` / `up` / `status` / `down`;
    - native pinning;
    - React Native on Android;
    - the scenarios `termination-calibration`, `api-smoke` and `events-fetch-android`.
- 2026-11-05 — **first slice built** on `feat/WO-002-fetch-labkit` (not committed when this entry was written).
  - **Vendored core (D1).** 16 files byte-identical with bg-forge `49d38d47f` (`labs/VENDORED.md`, sha256 and mode per file). `labs/harness/vendor_check.sh` re-extracts each from bg-forge and passes.
  - **The pack.** Every fetch difference is in `labs/harness/fetch_pack.py`. It rebinds names in vendored modules after import, each with a seam row naming the vendored line (`./labkit pack`). `./labkit` activates it in every process the harness starts (`labs/harness/pack/sitecustomize.py`); a process that asked for it and cannot apply it exits 2.
  - **Native pin.** `com.transistorsoft:tsbackgroundfetch:4.1.1-master-5de5242` in `labs/m2`, from a detached worktree of native at `5de52423e` (dirty no; AAR sha256 `74b20be4…`), by `labs/harness/publish_native_fetch.sh`. Android only; no iOS pin yet.
  - **Platform.** React Native on Android: `labs/platforms/react-native`, package `com.transistorsoft.fetchlab.react`, RN 0.88.0. Its build resolves `tsbackgroundfetch` only from `labs/m2` and refuses an APK whose resolved version or AAR sha256 is not the label's. The plugin checkout is staged as a copy, so Gradle never writes into the submodule.
  - **Plugin under test.** The react-native checkout was on `fix/WO-001-spm-first` at `2acdc21` while the parent pins `75d3ed0` (4.4.2). The difference touches only iOS, podspec, changelog and `package.json` `swiftpmConfig`, so the Android plugin built is 4.4.2's either way (`labs/harness/variants/FIX.yaml`).
  - **Scenarios.** `termination-calibration`, `api-smoke`, `events-fetch-android`.
  - **Selftest.** `./labkit selftest` runs `fetch_selftest.py` (132 offline checks: activation through sweep's own launcher, the vocabulary against `types/background-fetch`, the loader, the metrics, the host actions, the drift guard, every shipped scenario and variant), the vendored `slots_selftest.js`, and `vendor_check.sh`. bg-forge's 727-check `_selftest` is bg-geo data and is not run.
  - **CLI.** `./labkit up --os android` and `down --os android` started and stopped both Lab AVDs twice (14:16 and 13:41 runs); `status` and `doctor` work. iOS Simulators: not exercised (no iOS host yet).
- 2026-11-05 — **decisions by Chris** on the first review's two open questions:
  - **The `run_host` copy is kept, with a drift guard.** bg-forge's host-action dispatch is closed (`run.py:581-601`), so the pack runs `job:` / `force_stop:` on a ~30-line copy of `run.run_host`'s wait-then-act loop. This is a deliberate exception to "never fork in place" (Approach) and "A plain fork" (Not an acceptable shortcut). Condition: `fetch_pack.PINNED` holds the source sha256 of `run.run_host` and of every other vendored function the pack copies or re-implements, and the pack refuses to run (exit 2) when a re-vendor changes one. A bg-forge work order for an `ACTION_HANDLERS` seam deletes the copy.
  - **`termination-calibration`'s force-stop step keeps the measurement:** `job: 999` with `expect: absent`, scored green when JobScheduler no longer holds the job. Rejected: dropping the `job:` action (the step would no longer measure) and leaving the step red by design.
- 2026-11-05 — **`termination-calibration` result (MEASURED), on both Lab AVDs.** React Native, Android 16 / API 36, Play services 263635035; native `4.1.1-master-5de5242`, plugin `2acdc21`.
  - **`run-as kill -9` keeps the fetch job; `am force-stop` cancels it.** So every headless scenario terminates with `kill: true`, never `force_stop`.
  - **Kill, Lab1** (sweep 20261105-141801): `registerTask … (jobId: 999)` 14:18:03.303 → pid 4103 died 06.466 → `run -f … 999` 09.725, mark `said=Running_job_FORCED` → `Start proc 4162 … for service … FetchJobService` → `- Background Fetch event received` 09.940 → RN `point=headless event=fetch` 10.010 → `- finish:` and `- jobFinished` 11.025 → job 999 still registered at the step's end.
  - **Kill, Lab2** (14:16, kept in `labs/baselines/2026-11-05-termination-calibration-lab2-141801`, gitignored): the same sequence — pid 3223 died 14:16:30.535, run 33.833, received 34.043 (new pid 3939), headless 34.118, finish and jobFinished 35.123, job 999 still registered.
  - **Force-stop, Lab1:** `registerTask` 14:18:50.779; JobScheduler held 999 just before (mark `held=999`, read from `dumpsys jobscheduler`); `Force stopping com.transistorsoft.fetchlab.react` 53.941; `run -f … 999` 57.317 answered `Could not find job 999 in package com.transistorsoft.fetchla…` (`said=`); no process started, no SDK or headless line; no job of the package at the step's end.
  - **Force-stop, Lab2:** `registerTask` 14:17:14.657, `held=999`, force-stop 17.804, run 21.183 answered `Could not find job 999…`.
  - **Run 1** (Lab1, 13:04-13:05, with the step as first specified, force-stop then a plain `job: 999`): kill green, force-stop red `actionsDone action1=missed(no-job)`. That is the same finding scored as a failure; its capture is `labs/baselines/2026-11-05-termination-calibration-run1` (gitignored).
  - All green with the current file: sweeps 20261105-134121 and -141801 (Lab1) and the Lab2 runs at 13:44 and 14:16. Committed captures are sweep 141801's (Lab1). A capture path has no device in it, so a later run of the same cell overwrites the earlier one; Lab2's raw logs survive only in the gitignored baseline, and their decisive lines are quoted here.
  - **Consequences.**
    - run.py force-stops the app before and after every step (`backends.Adb.terminate`), which cancels its jobs. Every step configures again (the RN driver does at boot), and no job carries across a step boundary except through a `process: live` step.
    - Not yet measured: whether force-stop also cancels a custom task's job or an AlarmManager task. The finding is worded for job 999 only.
- 2026-11-05 — **other observations from the first slice (MEASURED unless marked)**.
  - **`api-smoke`:** `stop(taskId)` then `stop()` leaves the app holding no job (jobIds `-` at the step's end, read before run.py's end-of-step force-stop). Android `status()` and `start()` answer 2 whatever happens, which is why the scenario also asserts jobs and configureOutcome.
  - **`events-fetch-android`:** two forced runs 6.4 s apart give two deliveries to `configure@0` with the bare taskId `react-native-background-fetch`; no duplicate-job drop. At configure, JobScheduler starts the periodic job immediately and the SDK ignores it (`onStartJob returned false`, < 1 s after scheduling) — `events-initial-ignored` (tier 1) should pin this.
  - **`- registerTask:` proves only that the SDK asked.** It is logged before `BGTask.schedule()`, whose result is discarded (`BackgroundFetch.java:259-261`, `BGTask.java:169`). JobScheduler's side is read from `dumpsys` (`jobIds`, `jobsAtForceStop`).
  - **BOOT_COMPLETED at launch.** On Android 16, a launch after an `am force-stop` of an app already launched delivers BOOT_COMPLETED to the SDK's BootReceiver, and run.py force-stops before every step. It does not arrive after a fresh install: Lab1's kill step had none, Lab2's, where the install was skipped, had one.
    - SUSPECTED, not reproduced: the SDK's `onBoot` clears a `startOnBoot:false` config off the UI thread about 0.3 s before configure() saves it (`BackgroundFetch.java:101-121`). If the clear lands after the save, a later headless start finds no config.
    - The `bootBroadcasts` metric records it in every step.
    - Boot scenarios (tier 2) must key on a real `adb reboot` mark, not on BootReceiver lines.
  - **Foreign apps wake on the Lab devices.** In Lab2's kill step, `com.transistorsoft.backgroundgeolocation.react` (bg-geo's RN example, not a lab app) was started for a boot broadcast. It logged nothing of TSBackgroundFetch's (`foreignLines` 0).
- 2026-11-05 — **two review rounds**, read-only, before commit. Round 1: one reviewer after the build. Round 2: three independent reviewers (pack code, device evidence, docs and this work order), each should-fix finding then checked by a refuter. Applied:
  - The drift guard: `_rebind` refuses a vanished seam, `_extend` refuses a name the vendored side grew, and any patch-time failure exits 2. `CALLBACK_ARG` and `FGS_PROMOTED` are derived from the vendored patterns.
  - `Capture.sdk` counts only processes tied to the capture's package (`point=appCreate process=<pkg>` or `Start proc <pid>:<pkg>/`; on a `process: live` step also the driver's `app=boot` pid). `foreignLines` (implicit 0) counts every other process's SDK, `point=headless` and `point=task-` lines, which bg-forge's headless metrics would otherwise count as the app's.
  - New metrics:
    - `configureOutcome` (implicit `ok`; `-` on a `process: live` step; `unsettled=<n>` for a configure() that never settled). It is an early metric, and the evidence end waits for a pending configure.
    - `jobsRegistered`, `jobsAtForceStop`, `bootBroadcasts`.
  - `job:` gained `expect: present | absent` and records JobScheduler's answer as `said=`. `force_stop:` records `held=`.
  - `api-smoke` asserts jobs and `measure_from: launch`. The selftest now loads every shipped scenario and variant.
  - Docs: `labs/README.md` (a Pinning section, the driver's configure lines, process scoping, the drift guard's limits); `labs/VENDORED.md` (the scan's scope); `labs/.gitignore` (`platforms/*/.stage/`). This work order's stale labels were corrected (the device-run note, "INFERRED" device commands, force-stop "NOT CHECKED", 14 → 10 config keys, the prefix-eviction reasoning, the host-action table, "baselines committed").
  - **Declined:**
    - `npm ci` in the RN build: bg-forge's RN lab runs `npm install`, and `npm ci` would wipe node_modules on every build.
    - Recording the linked AAR's sha256 in each `run.json`: a follow-up. Today the build-time check plus the source stamp tie the APK to the label.
- 2026-11-05 — **upstream seams the pack works around** (for bg-forge work orders; Phase 0's prefix eviction first):
  - prefix eviction of every Transistor lab app (`run.py:708-714`), plus process scoping in bg-forge's headless metrics and `devices.contamination` for `point=headless` / `point=task-` lines;
  - `ACTION_HANDLERS` for host actions (`run.py:581-601`, `labkit._validate_moves` `ACTION_DOES` `:1565`), which deletes the `run_host` copy;
  - a pack hook imported by `labkit.py` itself (so a bare `python3 run.py` cannot run bg-geo's seams in this repo), plus hooks for headless names (`labkit.py:3383-3411`), implicit expectations, evidence, the SDK log adapter and per-capture extras;
  - `lab_devices up/down --name Lab1|Lab2` (today both start together);
  - a per-product native publish descriptor for `publish_native.sh` and `lab_setup` (`published`, `ensure_native`).
- 2026-11-05 — **state.** Verification: D1–D4, VENDORED, the selftest and the calibration are ticked.
  - Not ticked:
    - `doctor` / `up` / `status` / `down` (Android only so far);
    - the bg-forge prefix-eviction work order (not filed);
    - the tier 1 board (3 of 15 scenarios, 1 of 10 cells);
    - mutation variants;
    - back-to-back sweeps;
    - the manual iOS tier;
    - suspected-defect work orders.
  - Next slices: the remaining tier 1 scenarios on RN Android; the iOS pin and hook (D3, D4); the other four platforms.
- 2026-11-07 — **Re-vendored onto bg-forge WO-191, and the pack moved onto its hooks** (uncommitted, awaiting Chris's diff review). Branch `feat/WO-002-revendor-wo191`, from `master` `05b5959`.
  - **The upstream work order:** the seams listed on 2026-11-05 became bg-forge WO-191, Parts 1–5. Its status there is `verify`: the code is in, its device proof is Chris's.
  - **Vendor commit:** `23f448c42`, the WO-191 branch tip. bg-forge `master` (`dc13cac37`) held Parts 1–4 then. `23f448c42` adds Part 5, and only Part 5's four files differ. The commit stays in `master`'s history once merged, so `vendor_check` holds either way.
    - Eight of the 16 vendored files changed: `labkit`, `backends`, `run`, `devices`, `summarize`, `lab_devices`, `lab_setup`, `inventory`. `VENDORED.md` records every sha256, mode and line count.
  - **The new `labs/harness/lab_product.py`:** fetch-owned, at a path bg-forge does not have. The vendored `labkit.py` loads it and calls `register(labkit)`, which calls `fetch_pack.register()`.
    - It refuses (`labkit` exits 2) without `FETCH_LABKIT_PACK=1`, without the activation, or with another checkout's pack: WO-191 Part 3 was narrowed to labkit's seams, so a process without the activation would mix fetch's labkit seams with bg-geo's backends and run.
  - **Moved onto hooks:**
    - `Capture.sdk` replacement → `SDK_LOG`;
    - the `IMPLICIT`/`EARLY_METRICS` rebinds → in-place additions;
    - the `evidence_state` wrap → `EVIDENCE`;
    - the `Adb.end_log` and `run_step` wraps → `CAPTURE_EXTRAS`;
    - the vocabulary's `enums.Event` convention → `HEADLESS_NAMES`, and the entry is removed;
    - the `_validate_moves` wrap (a `kill` stand-in) and the `run_host` companion runner → `ACTION_HANDLERS` `job` (`keys=("expect",)`; a repeat found by identity) and `force_stop`;
    - the `lab_setup.published`/`ensure_native` copies and the `inventory` rebind → `NATIVE`. Fetch's own Android slot calls `publish_native_fetch.sh` in a worktree named `native`, as before; the iOS slot stops at "WO-002 phase 2".
    - `CALLBACK_ARG`, `FGS_PROMOTED` and the `_validate_hooks` and `expectations` wraps moved into `register()` verbatim, so `labkit` gets no post-import patch.
  - **Deleted:** those copies, their four PINNED rows, `_fetch_runner`, the `GRADLE_PLACEHOLDERS` rebind (no reader on fetch's slot) and `threading`.
  - **Kept, for Phase B:** the activation and the `backends`/`run`/`sweep`/`lab_setup` rows, including `SUBMODULES` (WO-191 D4 option A).
  - **Every seam row re-cited** against the new lines (`./labkit pack`).
  - **Behaviour:**
    - All 10 captured steps (4 board captures, 6 `termination-calibration` baselines, from the WO-002 worktree) re-score byte-identical: every metric row, every verdict.
    - The review's old/new differential found no difference in what loads or refuses, the resolved steps, runner calls and marks, SDK lines, evidence, expectations, jobscheduler.txt or the native setup commands, beyond the ones below.
  - **Intended differences** (each in README or here):
    - The metric names `headlessEvents.locationerror` and `headlessFired.locationerror` are gone: a bg-geo name that fetch never used.
    - A bare process (no `FETCH_LABKIT_PACK`) now exits 2, where it ran "inert" with bg-geo's seams.
    - **Actions share one loop:** `job`/`force_stop` now share `run_host`'s single loop, so a slow one delays an action due at the same time, and a failing one stops the rest. No shipped scenario shows it: `termination-calibration`'s kill→job chain moves by one 0.25 s poll.
    - **iOS stop:** it no longer makes a `native/background-fetch` worktree before stopping.
    - **Capture timing:** `jobscheduler.txt` is written after `logcat.txt`, so a failed read keeps the log. run.json `timings` show a `capture_extras` phase and a shorter `end_log`.
    - **Wording:** some refusal texts differ for the same inputs (vendored checks run first).
    - **Key order:** a resolved item keeps its author's key order.
  - **Fetch selftest:** 110 → 140 checks, all PASS with `types/background-fetch` checked out. `slots_selftest` and `vendor_check` pass.
    - Checks that pinned the old rebinds now check the hooks.
    - The runner test drives the vendored `run_host`.
    - New: `test_hooks` (`CAPTURE_EXTRAS`; `NATIVE`'s `published()` and both slots through the real `ensure_native`; `inventory`; the `check_repo` exit-2 pass-through), the repeat budget with equal items, each half of the guard, a symlinked harness path, and another checkout's pack.
  - **Mutants:** 17, each failing a named check.
  - **Review:** read-only; a Sonnet-high behaviour differential, an Opus-max guard and activation lens, a Sonnet-medium docs/scope/tests lens, and verifiers on the other model. Fixed:
    - **Medium, verified real:** the README still described the pinned `run_host` copy.
    - **Medium, verified real:** the guard passed when the active pack's hook would not patch this harness (a symlinked path, which the pack compared by `abspath` while Python uses the realpath; or another checkout's pack). The pack now compares real paths, and the guard requires this harness's own pack.
    - **Lows:**
      - the pack's `check_repo` wrapper swallowed labkit's exit 2 as "missing variant";
      - the selftest run on its own exited 2;
      - stale citations in `VENDORED.md`, `FIX.yaml` and the selftest docstring;
      - no `inventory` check.
    - **Not fixed (pre-existing, out of scope):** two pack dirs on `PYTHONPATH` make the sitecustomize shims chain each other to `RecursionError`. It needs a realpath compare and a sentinel in `pack/sitecustomize.py`; worth its own small change.
  - **Verification:** "bg-forge prefix-eviction work order filed and done" stays unticked. It was filed and its code is in (WO-191 Part 1), but its on-device proof is Chris's. "Back-to-back sweeps" still waits for those runs.
- 2026-11-08 — **Committed** (`80a0b26` labs, `b6450b2` Log) after Chris's diff review.
- 2026-11-08 — **The pack's sitecustomize recursion, fixed** (uncommitted, awaiting Chris's diff review), on Chris's approval of the plan.
  - **Diagnosis** (pre-existing; found by the re-vendor review): `pack/sitecustomize.py` chained the first other `sitecustomize.py` on `sys.path`, by `abspath`. A second fetch pack on `PYTHONPATH` counted as "other" and chained the first one back, until `RecursionError`, and every harness process exited 2.
    - Reproduced both ways: another checkout's pack (`eval "$(./labkit env)"` in the main checkout, then `./labkit selftest` in a worktree), and this pack spelled through a symlink.
  - **Fix:** the shim skips any fetch pack's shim (a pack dir beside a `fetch_pack.py`) and chains the first other one, the interpreter's own (Homebrew's here), comparing real paths. `./labkit` puts its own checkout's pack first, so that one loads. A hand-run shell whose first pack is another checkout's now gets lab_product's "another checkout's" refusal instead of a recursion.
  - **Proof:**
    - The selftest's new `test_shim` covers two packs, and a symlinked spelling first. Each loads the first pack and chains exactly the sitecustomize a bare interpreter imports. 140 → 141 checks, PASS.
    - Mutants: no skip of fetch shims, and never chaining anything, each fail it. Reverting the candidate compare to `abspath` passes, as an equivalent mutant: the skip rule alone already catches a symlinked spelling, so the realpath compare is defence in depth.
    - The documented case, run for real: the main checkout's env, then `./labkit selftest` here, PASS. With `HEAD`'s shim in the same shell, `RecursionError`.
- 2026-11-08 — **Committed** (`78b9ba7` labs, `b704bc1` Log) after Chris's diff review. Both of this branch's commits are merged in `master`: the re-vendor in `f03a087`, the sitecustomize fix in `e744105`.
- 2026-11-09 — **Back-to-back sweeps with bg-forge: no contamination** (Chris's runs on Android Lab1 `emulator-5580`, checked from their captures). The full account is in bg-forge WO-191's Log, 2026-11-09.
  - **fetch's side first:** `./labkit setup --os android` (`setup-20261108-133239`) published `4.1.1-master-5de5242` into the main checkout's `labs/m2` (commit `5de52423e1c5`, `dirty=no`) and built the react-native app.
    - The fetch sweep before it (`20261108-133134`) stopped at its build: the label was not in `labs/m2` yet, and the build's guard refused. Nothing reached the device.
  - **The sweeps,** api-smoke, each with `--devices 1 --build stale --no-uninstall`. A sweep uninstalls its own Android app after its last cell, so without `--no-uninstall` the next lab's sweep finds nothing to evict.
    - bg-forge `20261108-133031`, PASS: the baseline. No fetch app was installed yet.
    - fetch `20261108-133328` and `20261108-155938`, PASS, 43 s each. `evicted`: `com.transistorsoft.backgroundgeolocation.react` and `com.transistorsoft.labs.react`. `foreignTransistorApps`: the first. The only `Start proc` line is `fetchlab.react`'s.
    - bg-forge `20261109-083338`, PASS. fetch's app had been launched just before it, with job 999 run and scheduled. The step evicted it, and the force-stop cancelled the job (`get-job-state … 999` → `Could not find job 999`).
    - `summarize` scores every capture PASS, with no `CONTAMINATED`.
  - **Still open in this box's neighbours:**
    - "bg-forge prefix-eviction work order filed and done": WO-191 is filed but still `verify`, with iOS eviction, parity and D4's `setup --os ios` open.
    - The fetch captures from these sweeps are not committed. That belongs to the Tier 1 box.
- 2026-11-09 — **bg-forge WO-191 is `done`**, so the prefix-eviction box is ticked. The details are in WO-191's Log, 2026-11-09.
  - **iOS:** bg-forge's react-native api-smoke on the Lab1 Simulator uninstalled a `com.transistorsoft.fetchlab.` app at its first step. The app was a stand-in, bg-forge's `NativeLab.app` re-identified as `com.transistorsoft.fetchlab.standin`, because fetch has no iOS app yet. Eviction matches on the prefix alone, so phase 2's real app takes the same path; its first iOS run's `evicted` will show it.
  - **D4:** bg-geo's `./labkit setup --os ios` rebuilt and published its label through Part 5's default iOS slot. fetch's own iOS slot (`fetch_pack._native_ios`) still stops at "WO-002 phase 2".
- 2026-11-09 — **Tier 1 on react-native Android: eight scenarios and one measurement** (uncommitted, awaiting Chris's diff review), on Chris's approval of the plan. Branch `feat/WO-002-tier1-android`, from `master` `efe59ce`.
  - **How it was designed.**
    - Five read-only research agents covered: the native SDK and RN bridge at the pin, the harness, the RN driver, the regression history, and bg-forge's precedents. They read the native pin `5de5242`, the RN plugin `c6dff5f` (now `a3c6996`, which changes only npm publishing, not `android/`, `index.js` or `src/`), and the measured captures.
    - Three adversarial reviewers then read the draft: one for harness validity, one for device behaviour, one for regression coverage.
    - Their 4 blockers were load refusals: a hook id, missing `status` hooks, and a configure declaration without `args`/`returns`.
    - Their 4 majors, all fixed in what was built:
      - a timeout metric that floored at the 60 s boundary;
      - a warm headless job with only ~0.3 s of margin past the SDK's duplicate window;
      - RN 4.4.2 left unpinned by a latency recorded as `any`;
      - a control job inside the initial run's 5 s window, which hid a revert of c88f7d8.
  - **Scenarios** (`labs/harness/scenarios/`, all in SUITE):
    - `api-constants`: the eight constants probed against the vocabulary.
    - `events-initial-ignored`: configure()'s immediate run ignored; a control run 6 s later delivered.
    - `events-duplicate`: job 999, then a custom task, then 999 `unspaced` at +3 s (dropped), then 999 `unspaced` at +6.5 s (delivered). This pins the 5 s window, the queue (not 9f75ea1's single slot), and that a dropped run does not restart the window.
    - `events-task-android`: scheduleTask, the task fired by its hashCode, then `expect: absent`.
    - `events-timeout-sdk`: finish withheld, the SDK's 60 s timeout, about 95 s a cell.
    - `stop-on-terminate`: a kill step and a finishNow step, each with job present, then absent. It pins both halves of 786b557.
    - `headless-kill`: cold then warm job. It pins 4.2.6, 4.2.7's start/end lines, and 4.4.2 (latency `<500` ms). 4.2.7's isTaskRunning guard behaviour cannot be pinned on RN 0.88.
    - `headless-activity-destroyed`: RN's control for suspected defect 4.
    - `recreate-foreground-android`: the measurement for suspected defect 10, PREDICTED RED.
  - **Pack** (`fetch_pack.py`):
    - **`job:` gains a dated `unspaced:`.** It skips the host's 5.5 s repeat wait and is refused unless an earlier action fires the same job and the step states `duplicateJobs` as a number. The mark carries `unspaced since_ms=`.
    - **New metrics:**
      - `jobStops`: JobScheduler's `Job history:` STOP reasons, windowed by the host clock now written in jobscheduler.txt's header (`host_ms=`), else run.json's capture_extras phase;
      - `jobsCancelled`;
      - `rnHeadlessInit` / `rnHeadlessStarts` / `rnHeadlessEnds` / `rnHeadlessErrors`;
      - `headlessNoTask`;
      - `headlessLatencyMs`;
      - `driverEvents` / `driverFinishes`;
      - `appProcesses`;
      - `fetchTimeoutSkewMs`;
      - `lifecycleTally`.
    - No driver change. No vendored file touched: `vendor_check` passes.
  - **Offline half of api-constants:** the selftest now holds the types package's JSDoc `Value: N` and its types.ts tables to the vocabulary. That is where the NETWORK_TYPE transposition shipped.
  - **Selftest:** 141 → 164 checks, PASS. Re-scored on today's real captures (main checkout, gitignored):
    - events-fetch-android: `jobStops` `999:app_called_jobFinished=2,999:onStartJob_returned_false=1`, the pattern events-initial-ignored asserts;
    - termination-calibration's kill step: `appProcesses` 2, `rnHeadlessInit`/`Starts`/`Ends` 1/1/1, `headlessLatencyMs` 71.
  - **Corrections to this work order** (amended in place above, each marked 2026-11-09):
    - **`onStartJob returned false`** is JobScheduler's job-history stop reason, not an SDK line. The SDK logs nothing on that path (`FetchJobService.java:26-31`). This corrects the 2026-11-05 Log's wording.
    - **The Activity-destroyed cause is `finishNow`,** not `KEYCODE_BACK`.
    - **Suspected defect 3** does not repeat every 60 s.
    - **RN 4.2.7's isTaskRunning guard** cannot be pinned on RN 0.88. A revert of b640726 still shows, because its start/end lines go missing.
    - **The NETWORK_TYPE transposition** was in documentation only.
    - **786b557** has two halves.
    - **New suspected defect 10:** an RN recreation leaves the SDK headless.
    - **"Not an acceptable shortcut"** now names its one exception, `unspaced:`.
  - **The docs site** (checked at Chris's request): fetch.transistorsoft.com shows the correct NETWORK_TYPE values on every page and in its search index (`Last-Modified` 2026-06-29 16:09:58 GMT, 13 minutes after 37af5f2). The local `deploy/gh-pages` ref is behind its remote (`8a2d8aa`). fetch-forge's own `origin/gh-pages` (`ac1bb1b`, April) still carries the wrong values but is not what the site serves.
  - **PREDICTED, for the first board to settle:**
    - the dropped duplicate's stop reason, `onStartJob returned false`;
    - the one-shot task's job gone after its finish;
    - whether RN 0.88's JS timers run after its Activity is destroyed (headless-activity-destroyed's 1 s finish);
    - the first `[BGTask] timeout` line in any capture;
    - recreate-foreground-android red. If it is, file a work order for suspected defect 10.
  - **Not pinned:**
    - the behaviour of RN 4.2.7's isTaskRunning guard (its log lines are pinned);
    - HeadlessTask's old-architecture branch (the lab app is bridgeless);
    - the R8 keeps (Tier 2's `release-build-headless`);
    - the 1000 ms threshold's exact value, because no run between ~0.2 and 0.9 s after scheduling can be forced.
  - **Review and mutation, after the build.**
    - **Two read-only reviewers** read the built change: one on the pack code, run against the real captures; one on the scenarios, against the source. They found no blocker or major issue. Eight minor findings, all applied:
      - the action handler never ran with `unspaced`;
      - taskId pairing and the header's `host_ms` were not really exercised by the fixture;
      - `headlessLatencyMs` counted a headless timeout as a hand-over (now `event=fetch` only);
      - 4.2.7, not 4.2.6, added the start/end lines;
      - api-smoke's history shows a cancelled second run, not a second ignored one;
      - `jobsRegistered` is a set, so it cannot show "asked once";
      - the README's `lifecycleTally` example was in the wrong order;
      - the check count.
    - **Mutation testing:** 30 mutants of the new pack code, each in its own scratch copy of the harness, and each fails the selftest. They cover every `unspaced` rule, the no-wait primitive and the handler, the `host_ms` header, `jobStops`' window, row, package, block, mark and clock rules, the age parser, and each other new metric's scoping, pairing and arithmetic. The baseline passes.
  - **Next:** Chris's device run, `./labkit sweep --os android --devices 1 --build stale` (twelve scenarios, about 12 minutes; the first three took 184 s on 2026-11-09).
- 2026-11-09 — **The first board column: React Native on Android, Lab1.** Chris ran the sweep from the main checkout, 11:33–11:43, after the Tier 1 merge (`303a51f`).
  - **Inputs:** Lab1 `emulator-5580`, Android 16 / API 36, Play services 263635035. Native `4.1.1-master-5de5242` (`5de52423e1c5`, clean); react-native plugin `a3c6996` (clean).
  - **Result: 13 of 14 steps PASS** (12 scenarios). `recreate-foreground-android` FAILs, as predicted.
  - **The Log's PREDICTED items, now MEASURED:**
    - **The dropped duplicate's stop reason** is `onStartJob returned false`. `events-duplicate` reads `jobStops 999:onStartJob_returned_false=2` (configure's immediate run and the +3 s repeat), with `duplicateJobs 1` and three deliveries.
    - **The one-shot task's job is gone after its finish.** `events-task-android` ends holding only `999`. The task (`-1681675378`) was finished 1.005 s after delivery.
    - **RN 0.88's JS timers run after its Activity is destroyed.** In `headless-activity-destroyed`, the headless task's finish came at `after_ms=1000` (delivery 11:42:09.733, finish 10.751), on the warm context (`rnHeadlessInit 0`).
    - **The first `[BGTask] timeout` line:** `events-timeout-sdk`, 11:38:57.682, 60.001 s after delivery (`fetchTimeoutSkewMs 1`). The driver finished on the timeout (`timeout:ok=1`).
    - **`recreate-foreground-android` is red: suspected defect 10 is confirmed.**
      - Bold text recreates the Activity (`isChangingConfigurations=true`) and logs `☯️  HeadlessMode? true`, with no `☯️` line after it.
      - The job 4 s later is stopped with the app on screen: `Stopping on terminate`, `cancel … jobId=999`, `configure@0=never-fired`.
      - Its work order is **WO-003** (draft).
  - **Also measured:** `headless-kill` `headlessLatencyMs 69` (< 500, so 4.4.2 is pinned) over a cold and a warm job: `rnHeadlessInit 1`, `Starts`/`Ends` 2/2. `stop-on-terminate`: both steps stopped the fetch and cancelled job 999.
  - **Kept:**
    - The 14 steps' `summary.txt` + `run.json` are committed. The four steps captured before replace their 2026-11-05 run.json files; their summary.txt files are unchanged.
    - The full-window set is in `labs/baselines/2026-11-09-tier1-react-native-android-lab1` (main checkout, gitignored, 98 files). The WO-003 fix run will overwrite these capture paths.
  - **Not run:** Lab2 for these scenarios; the board's other nine cells.
  - **Verification:**
    - The Tier 1 box stays unticked (1 of 10 cells).
    - Defect 10 has its work order (WO-003). "Each confirmed suspected defect" stays open while the board grows.
  - **Next:** the iOS host (phase 2: the iOS native pin, the D3 hook, the D4 channel), planned for Chris's approval.
- 2026-11-10 — **Phase 2 planned: React Native on iOS. Chris approved the plan ("Approve the plan").** One read-only research agent covered the harness's iOS paths, bg-forge's RN iOS host and native iOS publish, and the fetch plugin's iOS bridge. Nothing is built yet.
  - **What exists (source):**
    - the harness's Simulator backend (install, launch with argv, the log stream, prefix eviction);
    - the pack's retag of `[TSB…` NSLog lines to `SDK_TAG` (`fetch_pack.py:50-52,200-214`).
  - **What is missing:**
    - the iOS pin (`fetch_pack._native_ios` stops at "WO-002 phase 2", `:575-580`);
    - an iOS app;
    - a host action that fires an event in the running app;
    - every iOS scenario (all twelve excuse iOS).
  - **Not usable as the template:** fetch's RN example is RN 0.85, an orphan gitlink, with no SceneDelegate.
  - **Slice 2a, CocoaPods channel:**
    1. **The iOS pin.** `_native_ios` builds a Simulator-only `TSBackgroundFetch.xcframework` from a `native/background-fetch` worktree at the variant's commit: a Release archive with `BUILD_LIBRARY_FOR_DISTRIBUTION=YES`, `CODE_SIGNING_ALLOWED=NO`, then an ad-hoc signature, as bg-forge's `build_native_ios.sh:37-59`. It publishes to `labs/xcframeworks/<label>/` with:
       - a podspec stamped with the iOS marketing version;
       - a `Package.swift` `binaryTarget(path:)` for 2b;
       - a provenance file: marketing, build, `sha256_sim`, commit, dirty.
    2. **The app.** `labs/platforms/react-native/app/ios`, from RN 0.88's template, with module `FetchLab` and bundle id `com.transistorsoft.fetchlab.react`.
       - `native-ios/`: AppDelegate, SceneDelegate, and a `LabModule` holding the argv parse, the probe and the hook.
       - Podfile: `TSBackgroundFetch` by `:path` to the label, and `LabModule`.
       - Info.plist: UIBackgroundModes `fetch`/`processing`, `BGTaskSchedulerPermittedIdentifiers`, and a `fetchlab` URL scheme.
       - An iOS branch in `build.sh`, after bg-forge RN's `build.sh:74-264`: the `pod install` guards, then post-build proofs (the linked slice's sha256, the `PROJECT:TSBackgroundFetch-<build>` string, the bundle id, a production JS bundle).
       - An `ios:` block in `lab.yaml`.
    3. **The fire action (D3).** A pack `ACTION_HANDLERS` `fire` with `oses=("ios",)` runs `xcrun simctl openurl <udid> fetchlab://fire?kind=fetch|timeout|task&id=…`. SceneDelegate hands the URL to `LabModule`, which calls the SDK's entry points (the Simulator rows of "How events can be forced") and logs `LAB point=fire`.
       - **INFERRED until the spike below:** that `openurl` reaches a running app, and what it does to the scene and app state.
       - Fallback: the 2026-11-05 launch-payload `at_s` slot (a step's `extras:` reach launch argv, `run.py:809-813`).
    4. **The pack.**
       - An optional `CAPTURE_EXTRAS` read of the SDK's prefs plist.
       - iOS metrics: `finish <id> (n of m)` and the completion result.
       - A gap to note: the plugin's own NSLogs do not open with `[TSB` (`RNBackgroundFetch.mm:57,80`, "`- … failed to start`"), so the log tap misses them.
    5. **Scenarios.**
       - `api-smoke` and `api-constants` stop excusing iOS.
       - New: `ios-fetch-sim`, `ios-timeout-sim`, `ios-task-sim`; then `ios-registration` (UIScene, `+load`).
    6. **Proof.** The selftest grows over the iOS pieces, then a sweep on the Lab1 and Lab2 iPhone 17 Pro Simulators (iOS 27.2).
  - **Slice 2b, SwiftPM channel (D4), after 2a.**
    - The channel reaches neither `build.sh` (whose placeholders are `{native}` and `{plugin}` only, `run.py:51-52`) nor the source stamp (`native|plugin`, `:214`). Vendored files change upstream first, so this needs a small bg-forge work order.
    - Then:
      - the SPM host gets its own build directory (RN's SwiftPM setup refuses a Pods xcconfig);
      - the staged copy of the plugin's `ios/Package.swift` points `transistor-background-fetch` at the label's local package.
    - This is the variant that exercises the plugins' `from: "4.2.0"` (WO-001).
  - **Decisions (Chris, with the plan):**
    - **D5:** commit an RN 0.88 template `app/ios`, as `app/android` is, rather than generating it on every build.
    - **D6:** one label per variant, named as today (`4.1.1-master-5de5242`). Each OS's provenance records its real version. At `5de5242` that is iOS 4.2.0 (build 4200) and Android 4.1.1, and the plugin podspec's `~> 4.2` needs the iOS one.
    - **D4 order:** CocoaPods first (2a), SwiftPM second (2b).
  - **First:** the `openurl` spike, with a throwaway app on a scratch Simulator, not the shared Lab devices.
- 2026-11-10 — **The D3 spike: `simctl openurl` is unusable, and `simctl push` delivers a real legacy background fetch (MEASURED).**
  - **Setup:**
    - A throwaway Swift app with a UIScene, UIBackgroundModes `fetch`/`processing`, and no SDK.
    - A scratch iPhone 17 Pro Simulator on iOS 27.2 (`FetchSpike`), created for the spike and deleted after it. The shared Lab devices were not touched.
    - The app's source and logs stayed in the session scratchpad and were not kept.
  - **`simctl openurl <udid> fetchlabspike://…` is rejected.**
    - iOS asks "Open in “Spike”?" for every open of a custom scheme, even with the app in front, and waits for a tap. Nothing reached `scene:openURLContexts:` in any state: foreground, suspended or not running.
    - While the alert is up, the app is held `inactive` (SpringBoard's deactivation reason `systemModalAlert`). The alert outlived an uninstall and reinstall of the app and went only when the Simulator was rebooted, so any later step would start contaminated.
  - **A Darwin notification** (`simctl spawn <udid> notifyutil -p <name>`) reaches a foreground app in 0.19 s (one try). A suspended app gets it only at its next resume: posted 09:42:11, delivered 09:42:15.791, when a push woke the app.
  - **Suspension:** a backgrounded app is suspended within 1 s of `sceneDidEnterBackground` (0.26 s where runningboard logged it) (runningboard `Suspending task`; its 1 s timer stops).
  - **`simctl push <udid> <bundle>` with `{"aps":{"content-available":1}}`:**
    - **The sequence:**
      - CoreSimulatorBridge asks SpringBoard to launch the app with `event_type = background_fetch`.
      - SpringBoard sends `UIFetchContentInBackgroundAction` and runs an `SBBackgroundFetchTask`.
      - UIKit calls `application:performFetchWithCompletionHandler:` on the app delegate, in a UIScene app too.
      - Only then does simctl try to post the notification. It fails with exit 121, `Notification has no user visible content`, after the fetch has been delivered.
    - **Foreground:** the delegate is called 0.10 s after the command, with `state=active` and `backgroundTimeRemaining` 30.0 s.
    - **Suspended:** the app resumes in the background (`BG-Active`). The delegate is called after 0.11 s with `state=background` and 30.0 s, and the app is suspended again after its completion.
    - **Not running** (after `simctl terminate`): the app is launched in the background, with no `sceneWillEnterForeground`. The delegate is called 0.31 s after the command, with 29.8 s.
    - **Two pushes 1 s apart:** two deliveries, the second in the same wake with 28.8 s.
    - **SpringBoard logs the completion result:** `Finished <SBBackgroundFetchTask …> with result: RetrievedNewData` (`.newData`), `NoNewDataAvailable`, or `Error` (`.failed`). That is an OS-side witness of what the app passed to the completion handler.
    - **Requirements:** UIBackgroundModes `fetch`, and a minimum fetch interval other than Never.
      - With Never (a fresh install that never set an interval), UIKit answered `NoNewDataAvailable` in 2 ms without calling the delegate.
      - An interval of 900 s works as well as the minimum.
      - Not needed, each tried both ways: `remote-notification`, an `aps-environment` entitlement, push registration.
  - **The legacy timeout:** the completion was withheld, and a UIBackgroundTask was begun as `TSBackgroundFetch.m:186-195` does. Its expiration handler ran 26.0 s after delivery, with 4.0 s left (runningboard: `Assertions for process will expire soon`). Completing then gave `with result: Error`, and the app was suspended 5 s later.
  - **What the SDK does with a legacy fetch** (source, at HEAD; no commit since `5de5242` touches these files):
    - On the Simulator, `scheduleBGAppRefresh` gets `BGTaskSchedulerErrorCodeUnavailable` and calls `setMinimumFetchInterval` with configure's interval (`TSBackgroundFetch.m:117-122,154-158`). So configure itself meets the interval requirement.
    - `performFetchWithCompletionHandler:applicationState:` (`:179-197`) reschedules, keeps the handler, and begins the UIBackgroundTask whose expiration calls `[TSBGAppRefreshSubscriber onTimeout]`.
    - **Nothing in an RN 0.88 app forwards UIKit's delegate call to it.** The plugin's `RNBackgroundFetch+AppDelegate.m`, which does, is left out by both the podspec (`source_files`, `:22`) and `ios/Package.swift:35`, and the install docs no longer mention it. So the lab app's AppDelegate must forward the call. SUSPECTED, not measured with the plugin: a stock RN app on the Simulator never receives a fetch event.
  - **Custom tasks still need the in-app hook.**
    - `scheduleTask` cannot submit on the Simulator, but `TSBGTask`'s initializer has already added the task to its list (`TSBGTask.m:119-133`). So `[[TSBGTask get:id] execute]` should reach its callback (INFERRED, source).
    - Its trigger would be the Darwin notification, with the app in the foreground.
  - **Proposed for Chris: D3 revised.** Fetch and timeout events on the Simulator come from SpringBoard through `simctl push`, not from a lab hook.
    - A pack `fire` action with `oses=("ios",)` runs `simctl push` with `content-available`. It accepts exit 121 only with that message.
    - The lab AppDelegate forwards `application:performFetchWithCompletionHandler:` to the SDK, as the plugin's excluded category did.
    - The `fetchlab://` URL scheme is dropped.
    - SpringBoard's `SBBackgroundFetchTask` lines join the iOS log tap, so the completion result is scored from the OS's side.
    - `ios-timeout-sim` becomes OS-driven (about 26 s).
    - Only `ios-task-sim` keeps a hook, fired by a Darwin notification.
    - What the Simulator still cannot exercise: the BGTaskScheduler path (`handleBGAppRefreshTask`, its expiration handler, and processing tasks). That stays the manual device tier.
- 2026-11-10 — **D3 revised, by Chris ("Yes, use simctl push").** It replaces 2026-11-05's D3 (an `openurl`-triggered lab hook, with a launch-payload fallback).
  - **Fetch and timeout events:** on the Simulator, they come from SpringBoard through `simctl push` with `content-available`, which the lab AppDelegate forwards to the SDK.
  - **Custom tasks:** they keep an in-app hook, fired by a Darwin notification with the app in the foreground.
  - Phase 2's plan (2026-11-10, above) changes to match:
    - step 2's Info.plist gets no URL scheme;
    - step 3's `fire` action runs `simctl push`;
    - the SpringBoard `SBBackgroundFetchTask` lines join the iOS log tap.
- 2026-11-10 — **Phase 2a, step 1: the iOS pin is built** (uncommitted, awaiting Chris's diff review).
  - **`labs/harness/publish_native_fetch_ios.sh LABEL [NATIVE_DIR]`** is fetch's own. It follows bg-forge's `build_native_ios.sh` + `publish_native_ios.sh` (`0fe0eb14c`): the same layout and provenance keys, and an ad-hoc signature. Unlike bg-forge's publish, it builds from the checkout itself, as the Android script does, so `commit` is the build's.
    - The build is the release `scripts/build-ios.sh`'s Simulator archive alone (`:131-139` at `5de5242`) with `CODE_SIGNING_ALLOWED=NO`, its derived data in a scratch directory that is removed on exit. The checkout's committed `TSBackgroundFetch.xcframework` (the last release's binary) is never read.
    - **Checks before anything is published:**
      - exactly one `ios-*-simulator` slice, with arm64;
      - the slice's version and build equal `versioning/TSBackgroundFetch.properties`;
      - a `PROJECT:TSBackgroundFetch-<build>` string;
      - `TSBackgroundFetch.h`, `TSBGTask.h` and `TSBGAppRefreshSubscriber.h` public.
    - **Published:**
      - the xcframework;
      - the checkout's podspec, its version stamped;
      - the checkout's `Package.swift`, its binary target pointed at the label's xcframework by `path:`, with `ios/TSBackgroundFetch/SwiftInterface` copied to the path it names;
      - the provenance, written last: commit, dirty, `built`, `slice`, marketing, build, `sha256_sim`, `swift_api`.
  - **The pack:** `_native_ios` runs it from the `native` worktree the Android slot uses, with `LANG=en_US.UTF-8`. `assert_native`'s refusal now names the script. `FIX.yaml` gains `native_source.ios` (the same commit), and README's Pinning section covers iOS and D6.
  - **Proof (MEASURED, in this worktree's `labs/`):**
    - **The label:** `4.1.1-master-5de5242` from `labs/checkouts/native-5de52423e` (dirty no): slice `ios-arm64_x86_64-simulator`, 4.2.0 (4200), `sha256_sim` `cfe07435…89d`, `swift_api` 4 files. Three seconds; the checkout is still clean after it.
    - **Rebuilt through setup's real `ensure_native`:** the label removed, then rebuilt by `_native_ios` with the same `sha256_sim`. A second call printed `ok: ios … is published` and built nothing. `TSL_TAG` (the build date) is in no `.m`/`.h`, so a rebuild on another day should give the same binary (INFERRED).
    - **SwiftPM:** a scratch package depending on the label by `path:` built for the Simulator, compiling the `BackgroundFetch` overlay and a target that calls `TSBackgroundFetch.sharedInstance().perform(completionHandler:applicationState:)` and `TSBGTask.get(_:)`. Xcode writes a `.swiftpm/` into a path dependency's directory, which slice 2b must expect.
    - **CocoaPods:** `pod ipc spec` reads `TSBackgroundFetch 4.2.0`, `vendored_frameworks` `TSBackgroundFetch.xcframework`, iOS 15.0, static.
    - **`./labkit doctor --os ios`:** `native ios … (published)` and `native ios Swift API … beside the xcframework`; both Lab Simulators booted.
    - **Selftest:** 164 checks PASS with `types/background-fetch` checked out. One expectation changed: the iOS slot now builds instead of stopping. `slots_selftest` and `vendor_check` pass.
  - **Not covered offline:** the script itself, which needs Xcode (the Android script isn't selftested either). The run above is its proof.
  - **Next (2a step 2):** the RN iOS app (`app/ios` from RN 0.88's template, AppDelegate forwarding the legacy fetch, Podfile `:path` pin, the `build.sh` iOS branch).
- 2026-11-10 — **Phase 2a, step 2: the React Native iOS app builds and receives fetches from `simctl push` (MEASURED)** (uncommitted, awaiting Chris's diff review). Branch `feat/WO-002-ios-react-native`, from `master` `273af07`.
  - **`app/ios` is committed (D5):** `@react-native-community/template@0.88.0` (from the local npm cache), renamed FetchLab as `init` does; `ios/.lab-template` records it. It is staged as its own first commit, so the lab's edits read as a diff against it:
    - `AppDelegate.swift`:
      - a `LAB point=didFinishLaunching` line;
      - `TSBackgroundFetch.sharedInstance().didFinishLaunching()`, as the plugin's install docs say (the SDK's `launched` flag makes its `+load` path and this one call);
      - the legacy-fetch forward (D3), with `LAB point=legacyFetch` and `legacyFetchComplete` lines.
    - `SceneDelegate.swift`: a `LAB point=sceneWillConnect` line, no dev-loading view, and the embedded bundle always, as bg-forge's host does.
    - `Info.plist`: UIBackgroundModes `fetch` and `processing`; BGTask ids `com.transistorsoft.fetch` and `com.transistorsoft.fetchlab.task`; display name "Fetch RN Lab".
    - The bundle id `com.transistorsoft.fetchlab.react`.
    - `Podfile`: `pod 'TSBackgroundFetch', :path => ENV.fetch('LAB_TSBF')`, `pod 'LabModule'`, and bg-forge's fmt-as-C++17 fix (MEASURED there on Xcode 26.4+, not re-measured here).
    - `LabModule/`: argv args with Android's keys, `knobs()`, the probe, the verdict, and the fatal-JS-error abort.
  - **`build.sh`'s iOS branch** checks the committed host and edits no tracked file:
    - the template's React Native version against `node_modules`;
    - the plist's modes and ids, and the bundle id;
    - the Podfile's pin;
    - `pod install` when the lock, sandbox, Podfile or development pods differ (bg-forge's rule);
    - `xcodebuild` with `FORCE_BUNDLING`;
    - then the proofs: the linked slice's sha256 and version against the provenance; the lock's EXTERNAL SOURCES; `PROJECT:TSBackgroundFetch-<build>` and the legacy-fetch forward in `FetchLab.debug.dylib`; the bundle id; a `--dev false` `main.jsbundle`.
    - `lab.yaml` gains the `ios:` block; `labkit.hosts("react-native")` is now `[android, ios]`.
  - **Found on the way (MEASURED):**
    - **The version string was missing.** TSBackgroundFetch's slice is a static archive, and nothing in the SDK references its own version string. So the app linked 72 TSBackgroundFetch symbols and no `PROJECT:TSBackgroundFetch-4200`, which the harness's `assert_native` (via `fetch_pack.Simctl.native_resolved`) reads for every iOS run. `LabModule.m` now references `TSBackgroundFetchVersionString` and reports it as the probe's `sdkBuild=`.
    - **An apostrophe in `"${VAR:?…}"` is a quote in bash.** Two of them in `build.sh`'s messages paired up and swallowed an assignment, and `bash -n` passed. The messages were reworded.
  - **The build:** 71 s for the first one (`pod install` and a clean `xcodebuild`), 8 s for an incremental one.
  - **On a scratch iPhone 17 Pro Simulator (iOS 27.2, deleted after; the shared Lab devices untouched),** launched with `--scenario --nativeLabel --finishDelayMs=1000`:
    - **Boot:** `configure … outcome=ok status=2`. The probe reads `sdkBuild=4200 configured=true subscribers=react-native-background-fetch`.
    - **Foreground push:** `SBBackgroundFetchTask` Executing, then `legacyFetch state=active remaining=30.0`, then the SDK's `performFetchWithCompletionHandler`, then `LAB app=event=fetch n=1`. `finish … after_ms=1000 outcome=ok` came 1.0 s later, then `legacyFetchComplete result=newData`, `[TSBackgroundFetch finish] … (1 of 1)` and SpringBoard `with result: RetrievedNewData`.
    - **Suspended push:** the same, with `state=background` and the probe's `pendingFetch=true` during the second.
    - **Not running** (after `simctl terminate`): a background launch (`didFinishLaunching state=background`, `argv=[]`). `legacyFetch` came at +0.03 s, before JavaScript booted at +0.05 s. The fetch was still delivered and finished 1.0 s later: the subscriber the SDK reloads at `+load` (`TSBGAppRefreshSubscriber load`) held it until the driver configured.
  - **`pod install` writes into the committed host (MEASURED, found after the build).** React Native's `react_native_post_install` rewrote the tracked `project.pbxproj` (a `PrivacyInfo.xcprivacy` resource, `OTHER_CFLAGS -DRCT_REMOVE_LEGACY_ARCH=1`, `REACT_NATIVE_PATH`, `USE_HERMES`, `PODFILE_DIR`, the Debug compilation condition) and reordered `PrivacyInfo.xcprivacy`'s entries. bg-forge never sees this because its `ios/` is gitignored. Done:
    - That output is committed as part of the host.
    - A second `pod install` changed neither file (MEASURED).
    - `build.sh` now hashes the tracked `ios/` files around `pod install` and refuses one that rewrites any, naming the file. Tested both ways: with the privacy manifest put back to the template's order it refused (`pod install rewrote tracked files under app/: ios/FetchLab/PrivacyInfo.xcprivacy`); on the committed state it built and passed every proof.
  - **Noise, not a defect:** a Debug React Native app asks `localhost:8081/status` once at launch and logs `-1004 Could not connect`, although the bundle is embedded.
  - **Not done yet (2a steps 3–6):** the pack's `fire` action and the SpringBoard lines in the log tap; the scenarios; a sweep on the Lab Simulators.
- 2026-11-10 — **D4's route chosen by Chris ("Variant axis"):** the SwiftPM channel is a variant key (`channel: spm`, e.g. a `FIX-SPM` variant), not a separate `react-native-spm` platform.
  - **Why it needs bg-forge first.** The vendored `run.build` hands a build only `{native}` and `{plugin}` (`run.py:51-52`). Its env expands only `{labs}`/`{repo}`, and `source_stamp` hashes only `native|plugin` (`:214`). So a variant's `channel:` reaches neither `build.sh` nor the staleness check. (It already reaches the native slot: `lab_setup.ensure_native` hands it the whole variant, `:426`; and `labkit.variant()` accepts any key.) Vendored files change upstream first, so this is a bg-forge work order. Its WO-189 already lists "a SwiftPM variant of the RN lab" as out of scope there, so both labs need it.
  - **Captures:** keyed by variant name (`run.py:618`), so `FIX-SPM` gets its own rows on the board without further change.
  - **Rejected: a separate SwiftPM platform.** No upstream change, but every scenario would have to list or excuse it (the closed `platforms()` world), and the board would show SwiftPM as another platform rather than as another way of installing one.
- 2026-11-10 — **Re-vendored onto bg-forge `76f6536ac`** (bg-forge WO-192 merged and pushed: a variant's `channel:` reaches builds and source stamps) at Chris's request ("Yes, re-vendor"). Branch `feat/WO-002-revendor-wo192`, from `master` `31eed1e` (uncommitted, awaiting Chris's diff review).
  - **Only WO-192 changed vendored files** since `23f448c42` (`git log 23f448c42..76f6536ac` over the 16 rows: one commit, `741b5bde5`): `labkit.py`, `run.py`, `sweep.py`, `lab_setup.py`, extracted with `git show 76f6536ac:<path>`. `labs/VENDORED.md` names the new commit, sha256 and line counts.
  - **25 citations of vendored lines in fetch-owned files re-cited** (the pack's seam rows, the README, the selftest's docstring, `pack/sitecustomize.py`, the React Native `lab.yaml`). Each was mapped from the old line through `git diff -U0`'s hunks, and none fell inside a changed hunk. Three more (`ios_grace :4650`, `(:307 wording)`, `(:382 prints …)`) sat after text and were found by a second scan. 15 were checked by hand to land on the code they name, and the three by their own lines. The Log's dated entries keep the lines of their own day.
  - **Proof:**
    - `vendor_check` PASS against `76f6536ac`.
    - Fetch selftest 164 PASS, `slots_selftest` PASS. The pack's drift guard passes: no function it pins changed.
    - **Re-score:** 43 full-window steps (the main checkout's 14 board captures, its 2026-11-09 baseline, and this worktree's 15-step WO-003 baseline) re-score byte-identical under the old and new harness: 41 PASS, and 2 FAIL, the pre-fix `recreate-foreground-android` capture and its copy.
  - **What fetch gains:** a variant's `channel:` now reaches `build.sh` (`LAB_CHANNEL`, `{channel}`) and the source stamp, `run.json` records it, and a platform view lists the channels its build implements in `build.channels`. None of fetch's views lists one yet, and `FIX` has no `channel:`, so nothing runs differently. That is slice 2b's first step.
- 2026-11-10 — **Slice 2b: the React Native SwiftPM channel builds, and `FIX-SPM` runs through it (MEASURED)**, at Chris's request ("Yes, start 2b"). Branch `feat/WO-002-ios-react-native-spm`, from `master` `077cddc` (uncommitted, awaiting Chris's diff review).
  - **The SwiftPM host, `app/ios-spm`** (committed beside `app/ios`):
    - **How it was made:** the template at `b94e95e`, converted by React Native 0.88.0's own `setup-apple-spm.js add --deintegrate --config-command '["node","ios-spm/lab-config.js"]'`. 0.88.0 ships the tool; WO-001 had proved the mode on 0.88.1. Its core, dependency and Hermes tarballs were already in `~/Library/Caches/ReactNative`, so nothing was downloaded.
    - **What gets committed:** the injected `project.pbxproj`, the injection record (`.spm-injected.json`, which remembers the config command), the scheme with its "Sync SPM Autolinking" pre-action, `.gitignore` and `.lab-template`. `FetchLab/` and `.xcode.env` are symlinks to `app/ios`'s, so both hosts run one AppDelegate and SceneDelegate.
    - **`lab-config.js`:** React Native's SwiftPM setup asks the CLI's `config` where the iOS project is, and the CLI always answers `ios/`. Without this, autolinking wrote to `app/ios/build` and found no plugin (MEASURED: "Using only built-in modules"). The script prints the config with `ios-spm` as `sourceDir`.
    - **LabModule:** an app module (`app/package.json` `swiftpmConfig.modules`, path `ios/LabModule`), so one `LabModule.m` serves both hosts with no project-file edit. React Native's wrapper for an app module gives it React's headers and nothing else. So `LabModule.m` no longer imports the SDK's headers: it reads the SDK's state through the Objective-C runtime and KVC, and keeps its `extern` of the version string, which the app's link resolves in both hosts. The CocoaPods host was rebuilt with it and passes every proof.
    - **Rejected:** editing the project to add LabModule. CocoaPods' `xcodeproj` gem would not load outside `pod`'s own environment here, and a module of the app's own is React Native's documented route.
  - **The native SDK through SwiftPM, with the plugin's manifest untouched.** A SwiftPM mirror (`ios-spm`'s `project.xcworkspace/xcshareddata/swiftpm/configuration/mirrors.json`, written by `build.sh`, gitignored) maps the plugin's `github.com/transistorsoft/transistor-background-fetch.git` to a local git repo of the label, tagged with its iOS version (`build/lab-mirror/transistor-background-fetch`, remade when the label changes). The plugin's own `from: "4.2.0"` is what resolves (MEASURED: `Checking out 4.2.0`).
    - The repo must carry the URL's name: `Package.resolved` keys the pin by the mirror directory's name (MEASURED: `identity: tbf-mirror` in the spike).
    - **Rejected:** rewriting the staged plugin's `Package.swift` to a path dependency. It would fail the stage's byte-for-byte check, and it would never test the plugin's version requirement.
  - **`build.sh`:** the iOS branch splits on `LAB_CHANNEL` (empty or `pods`, or `spm`), and the proofs are shared.
    - **SwiftPM:**
      - the checks: the symlink, the template version, the bundle id, the injection record's config command, the plugin's `ios/Package.swift`;
      - the mirror and `mirrors.json`;
      - `setup-apple-spm.js update` only when `build/xcframeworks` or `build/generated/autolinking` is missing (`sync` alone does not make `build/xcframeworks`; MEASURED);
      - `xcodebuild -project … -clonedSourcePackagesDirPath`;
      - a refusal if `update` or the build rewrote a tracked file under `ios-spm` or `ios` (MEASURED: `update` on the committed host changes none);
      - `Package.resolved`'s pin for the plugin's URL: identity `transistor-background-fetch`, version = the label's iOS version, revision = the mirror's commit.
    - **Both channels:** the slice the build linked (CocoaPods' `XCFrameworkIntermediates/…`, SwiftPM's `Build/Products/…/TSBackgroundFetch.framework`) against `sha256_sim` and the marketing version; the `PROJECT:` string and the legacy-fetch forward in `FetchLab.debug.dylib`; the bundle id; a `--dev false` bundle. The proven app is then copied to `app/build/lab-ios/FetchLab.app`, `lab.yaml`'s one iOS artifact (bg-forge WO-192 D2).
  - **`lab.yaml`:** the `ios:` view gains `build.channels: [pods, spm]` and the shared `app:` path.
  - **`variants/FIX-SPM.yaml`:** FIX plus `channel: spm`.
  - **A SwiftPM app's bundle (MEASURED, for the record):** the app embeds `Frameworks/TSBackgroundFetch.framework`, a 33 KB dylib with no symbols that nothing links, which carries the SDK's privacy manifest. The SDK itself is linked statically into `FetchLab.debug.dylib`, which defines its three classes. So there is no second copy of TSBackgroundFetch (WO-001's §6 crash).
  - **Proof:**
    - **Through the harness** (`run.py --build-only`, so the vendored `run.build` sets `LAB_CHANNEL`):
      - `FIX-SPM` from a clean `ios-spm/build` in 20.5 s: mirror, `update`, build, every proof, with `Package.resolved` pinning `transistor-background-fetch 4.2.0` at the mirror's commit and slice `cfe07435…`;
      - then `FIX` (CocoaPods) into the same artifact in 6.4 s, its stamp different from `FIX-SPM`'s;
      - `FIX-SPM` on Android refused: `react-native (android) builds no channel spm (build.channels: none)`.
    - **On a scratch Simulator** (deleted after), the harness-built SwiftPM app: `simctl push` in the foreground, suspended and not running each reached `legacyFetch`, then the SDK, the JavaScript event and `finish` 1.0 s later, then `newData` and SpringBoard `RetrievedNewData`. The probe read `sdkBuild=4200 configured=true` through the header-free LabModule.
    - **Selftest** 165 PASS (the new variant loads), `vendor_check` PASS. The plugin checkout stays clean (`git status` empty).
  - **Not done:** iOS scenarios (phase 2a step 4). Every scenario still excuses iOS, so neither channel has a scored run yet. A sweep can't run `FIX` or `FIX-SPM` on the Lab Simulators until one does.
  - **bg-forge WO-192:** its last box ("fetch re-vendors, and its `FIX-SPM` React Native iOS build runs through SwiftPM") is now MEASURED. Its status change is a bg-forge commit, waiting on Chris.
- 2026-11-10 — **A bug in slice 2b's `build.sh`, found by Chris's `./labkit setup --os ios --variant FIX-SPM`, and fixed** on Chris's approval of the plan ("Yes, fix it"). Branch `fix/WO-002-tracked-sums-symlink`, from `master` `2d40109` (uncommitted, awaiting Chris's diff review).
  - **The bug (MEASURED):** `tracked_sums` hashed every tracked path with `shasum`. Once `ios-spm` was committed, `git ls-files` listed `ios-spm/FetchLab`, a symlink to a directory. `shasum` followed it and exited 1 (`Is a directory`), and `set -euo pipefail` ended the build. That stopped every iOS build, CocoaPods too, since both guards pass `ios`.
    - Reproduced in this worktree through `run.py --build-only` (exit 2, the same two lines as Chris's run).
    - My earlier proofs missed it because every SwiftPM build ran while `ios-spm` was still untracked, and none ran after the commit.
  - **The fix:**
    - `tracked_sums` reads a symlink as `link:<target>` (never followed), a missing tracked file as `missing`, and a regular file by its sha1, as before.
    - `refuse_rewrites` takes the path after the state's two spaces whatever the state, so a retargeted symlink or a deleted file is caught and named, not silently passed.
    - Its callers are unchanged: the `pod install` guard and the SwiftPM guard.
  - **Proof (MEASURED):**
    - `FIX-SPM` then `FIX` through `run.py --build-only`: both build and pass every proof.
    - Three refusals, each naming the path:
      - a `pod install` forced over the template-order privacy manifest (`ios/FetchLab/PrivacyInfo.xcprivacy`), through the real CocoaPods path;
      - `ios-spm/FetchLab` retargeted;
      - `ios-spm/lab-config.js` moved away.
      The last two used `build.sh`'s own two functions, extracted verbatim, around each change, restored by git. An unchanged tree passes.
    - Selftest 165 PASS.
- 2026-11-10 — **Phase 2a steps 3–5: the first scored iOS runs, CocoaPods and SwiftPM, both Lab Simulators (MEASURED)**, at Chris's request ("Yes, start them"). Branch `feat/WO-002-ios-scenarios`, from `master` `17e1fc3` (uncommitted, awaiting Chris's diff review). A read-only research agent first mapped the harness's iOS run path.
  - **The pack:**
    - **Actions:** three iOS-only kinds (`oses=("ios",)`):
      - `fire: fetch`, `simctl push` with `content-available`, where exit 121 counts as a fired fetch only with simctl's "no user visible content" message;
      - `background: true`, which launches Settings over the app;
      - `terminate: true`, `simctl terminate`, after which the next `fire` launches the app in the background.
    - **Log filter:** the iOS filter's splice also keeps SpringBoard's `SBBackgroundFetchTask` lines (`I/SpringBoard`).
    - **Counts:** `fetchReceived`/`fetchFinished` count the iOS SDK's lines on iOS (`performFetchWithCompletionHandler`, and `finish … (n of m)`, not "Called without a task"). Android's other counts read `-` there.
    - **New metrics:** `legacyFetchStates`, `legacyResults`, `springboardResults` and `legacyTimeoutAfterMs`.
    - **A wrap of the vendored `devices.contamination`.** On iOS it counts any second `app=boot` as another lab's app (`devices.py:225-228`, "no trigger relaunches it"), so the first cold run read CONTAMINATED although it was the same app (pid 30305). The wrap excuses one boot after each of the step's own `action=terminate` marks. The rule is right for bg-forge, which never relaunches on iOS, so it is fetch's own exception, not an upstream change.
  - **Scenarios:**
    - **`ios-fetch-sim`:** steps foreground, background and cold.
    - **`ios-timeout-sim`:** the finish withheld in the background.
    - **`api-constants`:** no longer excuses iOS.
    - Both new scenarios are in `SUITE` and excuse android.
    - **`api-smoke` still excuses iOS.** Two of its expectations (`jobsRegistered`, `jobIds`) are JobScheduler's, and the harness has no per-OS expectation, so that needs its own design.
  - **Measured, `FIX` on Lab1 and `FIX-SPM` on Lab1 and Lab2, all PASS:**
    - **`ios-fetch-sim`:** each step gives `fetchReceived 1`, `fetchFinished 1`, `driverEvents fetch=1`, `legacyResults newData=1`, `springboardResults RetrievedNewData=1`; `legacyFetchStates` reads `active=1`, `background=1` and `background=1`. In the cold step SpringBoard launched the terminated app (a new pid) for the push, and the fetch still reached the driver.
    - **`ios-timeout-sim`:** the timeout came 25.996 s after delivery on Lab1 (`FIX`), and 25.999 s and 25.990 s on `FIX-SPM` (Lab1, Lab2). The driver finished on it, the SDK logged `finish … (1 of 1)`, and UIKit's handler got `newData`, which SpringBoard records as `RetrievedNewData`.
      - **The SDK reports a timed-out fetch as a success;** a Swift spike's `.failed` gave SpringBoard `Error`. It is pinned as today's behaviour, not judged right. The source (PROVEN): once every subscriber has finished, the SDK's `finish` signals the OS with success however the fetch ended, `setTaskCompletedWithSuccess:YES` on the BGTaskScheduler path and `UIBackgroundFetchResultNewData` on the legacy one (`TSBackgroundFetch.m:368-377` at `5de5242`). The research agent had cited `RNBackgroundFetch.mm:377`, which is the wrong file. **New suspected defect 11 (SUSPECTED, device path NOT MEASURED):** on a device, a BGAppRefreshTask whose expiration reaches the plugin's onTimeout, which finishes it, is reported to iOS as completed successfully, which may skew the system's scheduling of later refreshes. It gets a work order of its own if a device run confirms it.
    - **`api-constants`:** the eight constants pass on iOS.
    - **Provenance:** `run.json` records `channel: spm` for `FIX-SPM`, and `assert_native` read build 4200 from both apps.
  - **Selftest:** 170 PASS. New checks:
    - `push_fetch`'s exits;
    - `to_background`'s command;
    - the iOS metrics on a synthetic capture (a non-finish and another lab app's SpringBoard line are not counted);
    - the contamination wrap (excused after a terminate; not without one; not a second boot after one).
    - The action-kind, predicate and patched-module checks were updated.
  - **Kept:** the 10 steps' `summary.txt` + `run.json` under `labs/captures/react-native-ios/` (`FIX` from Lab1, `FIX-SPM` from Lab2, the last run of each cell).
  - **Not done:**
    - `ios-task-sim`: the custom-task hook, a Darwin notification into LabModule.
    - `ios-registration`.
    - `api-smoke` on iOS.
    - An iOS sweep: the harness also counted a non-Lab booted Simulator (`iPhone 17`, C8041EF8) as free, so each run here named its device with `--serial`.
- 2026-11-10 — **Phase 2a step 6: the first iOS sweeps, react-native `FIX` and `FIX-SPM`, both Lab Simulators (MEASURED)**, at Chris's request ("The iOS sweep"). Branch `feat/WO-002-ios-sweep`, from `master` `b0f4ab3`.
  - **A correction to the entry above.** The sweep never had the non-Lab Simulator problem. A sweep's devices come from `lab_devices.resolve`, which reads Lab1 and Lab2 by name (`lab_devices.py:620-657`). Only a plain `run` without `--serial` lists every booted iPhone Simulator (`devices.pick`, `devices.py:139-161`). There it refused, "several free emulators … pass --serial", which is that function's intended answer. No harness change was needed.
  - **The sweeps,** `./labkit sweep --platforms react-native --os ios --variant <V> --scenarios suite`, from this worktree:
    - **`20261110-202256`, `FIX`:** the build said `channel=pods`, `Podfile.lock: TSBackgroundFetch from the snapshot`.
    - **`20261110-202503`, `FIX-SPM`:** the build said `channel=spm`, `Package.resolved: transistor-background-fetch 4.2.0 from the mirror of the snapshot`.
    - Both builds linked the same slice (`sha256:cfe074357f4a`, native 4.1.1-master-5de5242 = 4.2.0/4200) and plugin `85a6bf4`, and were incremental (7.3 s, 8.8 s).
  - **Each board:** `api-constants`, `ios-fetch-sim` and `ios-timeout-sim` PASS; the other 11 suite scenarios excuse iOS (`·`). Two lanes: `ios-fetch-sim` on Lab1 (89–90 s), `api-constants` then `ios-timeout-sim` on Lab2 (72 s). Wall 97 s and 99 s. Load at most 5.2 and 7.4; memory pressure normal.
  - **The timeout** came 25.999 s (`FIX`) and 25.998 s (`FIX-SPM`) after delivery, both on Lab2.
  - **`api-smoke`'s iOS excusal** now gives today's reason. The old one said no platform had an iOS host. Now: `jobsRegistered` and `jobIds` are JobScheduler's, and an expectation cannot differ by OS yet.
  - **Kept:** the sweeps' captures replace the committed ones. A capture path has no device in it, so `api-constants` and `ios-timeout-sim` are now Lab2's and `ios-fetch-sim` Lab1's, for both variants. Only device, serial, timings and timeout values changed. `ios-fetch-sim/FIX-SPM/foreground` no longer lists an eviction: bg-forge's `nativeapi` app was gone from Lab1 by then.
  - **Selftest:** 170 PASS; `vendor_check` PASS.
  - **Still not done:** `ios-task-sim`, `ios-registration`, `api-smoke` on iOS (per-OS expectations), back-to-back bg-forge and fetch sweeps on iOS (Verification), and the other platforms' iOS hosts.
