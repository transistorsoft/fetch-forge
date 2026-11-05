---
id: WO-002
title: background-fetch has no device lab. Nothing calls a bridge's API or fires a fetch or headless event on a real emulator or simulator. Build a LabKit for fetch on bg-forge's harness
status: draft
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
> Nothing in this work order has been run on a device.

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

### How events can be forced (PROVEN from source; the device commands are INFERRED until a lab runs them)

Fetch differs from background-geolocation in one fundamental way: **the OS schedules events; no API call causes them.** A lab therefore needs host actions that fire them.

**Android (emulator and device)**

| Event | How to force it |
|---|---|
| Periodic fetch | `adb shell cmd jobscheduler run -f <pkg> 999`, the fetch job id (`BackgroundFetchConfig.java:265,302-308`). Wait ≥ 1.5 s after `configure`: runs < 1 s after `scheduled_at` are ignored (`FetchJobService.java:24-31`). Space repeats ≥ 5 s apart: duplicates are dropped (`:38-51`). |
| Custom task | `run -f <pkg> <Java String.hashCode(taskId)>`. The log line `registerTask: <id> (jobId: N)` gives the id. |
| Timeout | Withhold `finish` for the SDK's 60 s (`BGTask.java:28,80-85`), or `cmd jobscheduler timeout <pkg> <jobId>` (API 28+; NOT CHECKED on the lab image) → `onStopJob` (`FetchJobService.java:63-76`). |
| Headless | `stopOnTerminate:false`, `enableHeadless:true`. Kill with `run-as <pkg> kill -9` (bg-forge's `AndroidBackend.kill`), then `run -f 999`. |
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
- 7 methods, about 14 flat config keys, no State object.
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
- Both labs must evict *every* Transistor lab app by package prefix, not just their own `platforms()`.
- Otherwise a fetch lab app left installed fires JobScheduler jobs during a background-geolocation capture, and that capture fails the contamination check (`run.py:708-714`; INFERRED).
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

**4. Host actions.** Each is new unless marked reused:

| Action | What it does |
|---|---|
| `job:` | `cmd jobscheduler run -f <pkg> <jobId>`, honouring the ≥ 1.5 s and ≥ 5 s guards |
| `job_timeout:` | `cmd jobscheduler timeout` |
| `kill:` | Reused from bg-forge: `run-as kill -9` |
| `back` | `KEYCODE_BACK` |
| `reboot` | `adb reboot`, then wait for boot |
| `reinstall` | `adb install -r` |
| iOS `fire:` | Sends the lab hook a command. **DECISION D3** picks the transport: `xcrun simctl openurl <udid> fetchlab://fire?...` handled by the app (INFERRED to work; unverified), or an `at_s` slot in the launch payload. |

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
| 3 | Tier 1 scenarios, `termination-calibration` first; first board; baselines; README |
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
- **First board:** tier 1 on 5 platforms × 2 OSes, with baselines committed. Each mutation variant fails as intended (`must_fail`).
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
- **Using `am force-stop` to "terminate" before `termination-calibration` has run.** Force-stop may cancel the app's jobs (NOT CHECKED), which would make every headless scenario test the wrong thing. Use `run-as kill -9`, which is what bg-forge's `kill` does.
- **A separate claims directory or sweep lock.** The two labs would then use Lab1/Lab2 at the same time and corrupt each other's captures.
- **Firing the AlarmManager receiver via adb.** It isn't exported. Scenarios must wait for the alarm on time.
- **Copying bg-forge's vocabulary typing** (State tree, roundtrip, `invalid_values`, declared). Fetch has no State object and 14 flat config keys. Seed from `types/background-fetch`.
- **Scoring from an SDK SQLite log.** Fetch has none. Use the `TSBackgroundFetch` logcat tag and the iOS `[TSBackgroundFetch]` messages.

## Verification

- [ ] Decisions D1–D4 recorded in the Log
- [ ] bg-forge prefix-eviction work order filed and done
- [ ] `labs/VENDORED.md` names the bg-forge commit, and the vendored files are byte-identical with it
- [ ] `./labkit doctor` / `up` / `status` / `down` work against the shared Lab devices
- [ ] Offline selftest passes
- [ ] `termination-calibration` result recorded
- [ ] Tier 1 board green, or each failure linked to a work order; baselines committed
- [ ] Mutation variants fail as intended
- [ ] Back-to-back bg-forge and fetch sweeps: no contamination
- [ ] Manual iOS device tier run once and documented
- [ ] Each confirmed suspected defect has its own work order

## Log

- 2026-11-05 — created at Chris's request ("A LabKit implementation for background-fetch like bg-forge/labs").
  - Facts come from two read-only research agents (LabKit architecture with coupling classification; the fetch test surface, event triggers and regression history).
  - The only device evidence is from WO-001's session: an LLDB-driven `performFetchWithCompletionHandler:` on the iOS simulator DemoApp delivered fetch → finish.
  - Nothing else has been run.
