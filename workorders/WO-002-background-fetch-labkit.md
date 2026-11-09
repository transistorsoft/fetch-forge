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

- **RN 4.4.2:** a cold-start race in headless mode (`HeadlessTask.java:135,158`). Also bridgeless headless (4.2.6) and the `isTaskRunning` guard (4.2.7).
- **Flutter:**
  - 1.6.2: `MissingPluginException` after a relaunch while a foreground service kept the process alive.
  - 1.1.3: "FlutterJNI was detached" on the back button.
  - 1.2.0: a launch with the screen off was detected as headless.
  - 1.1.2: the release-build entry point `@pragma('vm:entry-point')`.
- **Capacitor 8.1.4:** `proguard-rules.pro` was missing from the npm package, so R8 stripped `BackgroundFetchHeadlessTask` in release builds.
- **Cordova:** 7.1.3, `onDestroy` → `setHeadless(true)`; 7.2.3, a missing `scheduleProcessingTask` argument.
- **Native Android:**
  - `stopOnTerminate` didn't cancel jobs (2020);
  - an NPE after upgrading (2020);
  - `onBoot` didn't re-arm AlarmManager tasks (2020);
  - headless detection moved to `LifecycleManager` (2022);
  - Android 14 exact alarms (2023);
  - the 5 s duplicate-job guard (2023).
- **Native iOS 4.1.0:** BGTask handlers auto-register from `+load` (UIScene hosts depend on it), `TSBGTask` delay and type persistence, and subscriber counting.
- **Types / docforge:** the `NETWORK_TYPE_UNMETERED`, `NOT_ROAMING` and `CELLULAR` constants were transposed (types `7be684d`, docforge `37af5f2`).

### Suspected defects for the lab to confirm or rule out (SUSPECTED; none reproduced)

Each one that a scenario confirms becomes its own work order.

1. **Capacitor iOS `start()`** is declared `CAPPluginReturnCallback` (`capacitor/background-fetch/ios/Sources/BackgroundFetchPlugin/BackgroundFetchPlugin.swift:13`), so it probably resolves a callbackId, not a status.
2. **Android `start()` after `stop()` does nothing.** `stop` destroys the config (`BackgroundFetch.java:166-173`), so `registerTask` logs "failed to find BackgroundFetchConfig" (`:249-251`). Every bridge still resolves `start()`.
3. **A headless class that can't be loaded strands the BGTask.** This happens with a missing Cordova class, a Capacitor `applicationIdSuffix`, or an R8 strip. The BGTask is never finished, and the error repeats every 60 s (`BackgroundFetch.java:280-284`, `BGTask.java:215-219`).
4. **Capacitor Android has no `onDestroy`/`setHeadless` hook.** With the Activity finished but the process alive, events go to a dead bridge and are never finished. `ProcessLifecycleOwner` never dispatches `ON_DESTROY` (`LifecycleManager.java:161-166`).
5. **Flutter headless burst:** a second event during isolate start-up is stranded until it times out (`flutter/background_fetch/android/.../HeadlessTask.java:114-123`).
6. **iOS stale subscriber:** a subscriber persisted from a previous run, now without a callback, blocks completion until the task expires (`TSBGAppRefreshSubscriber.m:27-43,133-134`, `TSBackgroundFetch.m:358-368`).
7. **iOS reconfigure is ignored:** a new `minimumFetchInterval` is dropped by the `fetchScheduled` guard (`TSBackgroundFetch.m:104`). Flutter's Dart `configure` also binds its callbacks only once (`lib/background_fetch.dart:442`).
8. **The Swift API's `@discardableResult` `onFetch`/`scheduleTask`** remove the listener or stop the task at once if the caller doesn't keep the result (`SwiftInterface/BackgroundFetch.swift:132,147-149,189,210-212`).
9. **A `delay` above 2³¹−1 ms** breaks the `getInt`/`getInteger`/`Integer` casts in RN, Capacitor and Flutter.

### How events can be forced (PROVEN from source; each device command is INFERRED until a lab has run it — those marked MEASURED have, see the Log)

Fetch differs from background-geolocation in one fundamental way: **the OS schedules events; no API call causes them.** A lab therefore needs host actions that fire them.

**Android (emulator and device)**

| Event | How to force it |
|---|---|
| Periodic fetch | `adb shell cmd jobscheduler run -f <pkg> 999`, the fetch job id (`BackgroundFetchConfig.java:265,302-308`). Wait ≥ 1.5 s after `configure`: runs < 1 s after `scheduled_at` are ignored (`FetchJobService.java:24-31`). Space repeats ≥ 5 s apart: duplicates are dropped (`:38-51`). **MEASURED** (RN, Android 16): two forced runs 6.4 s apart, two deliveries (`events-fetch-android`). |
| Custom task | `run -f <pkg> <Java String.hashCode(taskId)>`. The log line `registerTask: <id> (jobId: N)` gives the id. |
| Timeout | Withhold `finish` for the SDK's 60 s (`BGTask.java:28,80-85`), or `cmd jobscheduler timeout <pkg> <jobId>` (API 28+; NOT CHECKED on the lab image) → `onStopJob` (`FetchJobService.java:63-76`). |
| Headless | `stopOnTerminate:false`, `enableHeadless:true`. Kill with `run-as <pkg> kill -9` (bg-forge's `AndroidBackend.kill`), then `run -f 999`. **MEASURED** (RN, Android 16, both Lab AVDs): a new process, the RN headless task, finish, `jobFinished` (`termination-calibration`). `am force-stop` instead cancels job 999. |
| Activity destroyed, process alive | `KEYCODE_BACK` or bg-forge's `finishNow`, then `run -f 999`. |
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
| `job:` | `cmd jobscheduler run -f <pkg> <jobId>`, honouring the ≥ 1.5 s and ≥ 5 s guards; `expect: present \| absent` says whether JobScheduler should hold the job, and its answer rides in the mark (`said=`) | built |
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
- **Firing events inside the SDK's guards.** A `run -f` less than 1 s after scheduling, or a repeat within 5 s, is dropped on purpose (`FetchJobService.java:24-31,38-51`). A scenario that does this will show false failures.
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
