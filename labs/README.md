# Background-fetch device lab (WO-002)

A lab is a minimal app plus a harness. The harness drives the app on a device and scores the run itself.
This lab is bg-forge's LabKit, pointed at TSBackgroundFetch.

**The protocol is bg-forge's** and is not repeated here: launch payloads, the `LAB app=` / `LAB point=`
log vocabulary, api hooks, event assertions, host moves and actions, steps that end on evidence,
devices and sweeps. Read it in bg-forge's `labs/README.md`. That protocol and the `./labkit` device,
run, sweep and board commands work here unchanged. bg-forge's native publish scripts
(`publish_native.sh`, `publish_native_ios.sh`, `build_native_ios.sh`), its platforms and its Pinning
section do not: see [Pinning](#pinning) below. This file covers only where fetch differs.

## What is vendored

`labs/harness/{labkit,backends,run,sweep,summarize,devices,lab_devices,lab_setup,inventory}.py`,
`slots_selftest.js`, `avd/`, `lab-publish.init.gradle`, and `labs/platforms/_shared/{labapi,labevents,logo}.js`.
They are copied **byte-identical** from bg-forge commit `23f448c42` (decision D1; bg-forge WO-191 grew the
seams this lab had worked around). They are never edited here. A change goes into bg-forge first, and the file
is then re-vendored.
[`VENDORED.md`](VENDORED.md) lists every file with its sha256, and `./labkit vendor-check` re-extracts
each one from bg-forge to compare.

bg-geo's data is not vendored:
- its vocabulary, scenarios and variants;
- the typed-config, member and roundtrip/invalid tables;
- the native publish scripts and `labstate.js`.

bg-forge's 727-check `_selftest` is vendored inside `labkit.py` but never run here: its checks are
bg-geo data. `./labkit selftest` runs fetch's own instead.

## The pack

`labs/harness/fetch_pack.py` is the one fetch-owned module that holds every difference. It never edits
a vendored file. Each override names the vendored line it stands in for; `./labkit pack` prints the table.

**labkit's seams are hooks (bg-forge WO-191).** The vendored `labkit.py` loads
[`harness/lab_product.py`](harness/lab_product.py) itself, at the end of its own definitions, and that calls
`fetch_pack.register(labkit)`. It sets labkit's hooks: `SDK_LOG`, `EVIDENCE`, `CAPTURE_EXTRAS` (jobscheduler.txt),
`HEADLESS_NAMES`, `NATIVE`, the implicit expectations, `job:` and `force_stop:` as `ACTION_HANDLERS` kinds, and fetch's
metrics. labkit.py gets no post-import patch at all. `lab_product.py` is fetch's own, at a path bg-forge does not have.

**The other vendored modules are patched (until Phase B).** `backends`, `run`, `sweep` and `lab_setup` have no
such hook yet, so the pack still rebinds names in them after they are imported. `./labkit` exports two
variables:
- `PYTHONPATH=labs/harness/pack`, whose `sitecustomize.py` first chains the interpreter's own
  sitecustomize, then loads the pack;
- `FETCH_LABKIT_PACK=1`.

From there:
- Each vendored module is patched as soon as its import finishes.
- A vendored script run as `__main__` (`run.py` started by `sweep.py:647`, `lab_setup.py`'s re-exec) has
  its globals patched when its `main()` is entered. `lab_devices.py` has no patch of its own; it sees the
  pack through the modules it imports.
- Children inherit the environment, so the pack is active in every process the harness starts.
  `./labkit selftest` proves this through `sweep.capped` (sweep's own launcher) and one level deeper.
- A process that asked for the pack and cannot load or apply it exits 2 ("could not be judged").
- A process WITHOUT the flag and the activation is refused too: `register()` runs while `backends.py` is still
  loading (bg-forge WO-191 Part 3), so `lab_product.py` will not give fetch's labkit seams to a process whose
  backends, run and sweep would be bg-geo's. labkit says "lab_product.py did not load" and exits 2.
- To run a harness script by hand, first run `eval "$(./labkit env)"`.

**Drift guard.** `vendor_check` pins the vendored files' bytes. The pack also refuses to run on a
re-vendor that moved what it depends on:
- Every rebind of a vendored name goes through `_rebind`, which refuses a name the vendored module no
  longer has; every name the pack adds goes through `_extend`, which refuses one the vendored module
  already has. Any other failure while patching (a wrapper reading a renamed name) also exits 2.
- `PINNED` holds the source sha256 of every vendored function the pack copies or re-implements rather
  than wraps (`Simctl.native_resolved`, `Simctl.permissions`, the two `sdk_db`, `lab_setup`'s
  `swift_api_published` and Capacitor pair). If a re-vendor changes one, the pack refuses until its copy has
  been reviewed against the new source and the hash updated. WO-191's hooks deleted four copies: the
  `run_host` companion runner, `Capture.sdk`, `lab_setup.published` and `ensure_native`.
- Wraps are not pinned. A wrapper that leans on vendored internals (`to_logcat`'s line shape, the text
  `assert_native` and `Report.line` rewrite, `_validate_hooks`' stand-in `$`) still needs the
  manual seam re-check in [`VENDORED.md`](VENDORED.md) at every re-vendor; the selftest catches most of
  it.

What it changes:

| Area | Fetch |
|---|---|
| SDK log | Android: the logcat tag `TSBackgroundFetch`. iOS: `NSLog` lines starting `[TSB…` (no os_log subsystem), retagged to that tag. `SDK_LOG` (what `Capture.sdk()` reads) reads the log on both OSes, never a database, and only lines from the app under test's own processes |
| Native | `tsbackgroundfetch` in `labs/m2`; `TSBackgroundFetch.xcframework` and `PROJECT:TSBackgroundFetch-<build>` on iOS |
| Permissions | None granted or revoked; there are no SDK dialogs |
| Vocabulary | [`harness/vocabulary.yaml`](harness/vocabulary.yaml) is seeded from `types/background-fetch` and has no State tree. `configure(config, onEvent, onTimeout?)` is an `events:` declaration (`subscribe: configure`, `args:` the config, `errors: true` for onTimeout); it can never be an `api:` hook. The `STATUS_*` and `NETWORK_TYPE_*` constants are probed: `{api: STATUS_AVAILABLE, probe: true, returns: {$: {equals: 2}}}` |
| Headless names | `fetch` and `timeout` (`fetch_pack.HEADLESS_NAMES`, labkit's `HEADLESS_NAMES` hook). A lab driver logs `LAB point=headless event=fetch` for a HeadlessEvent with `timeout: false`, and `event=timeout` otherwise: fetch's HeadlessEvent has no name |
| Implicit expectations | Every step that expects anything also expects `configureOutcome: ok` and `foreignLines: 0` (below). Both are early metrics, so a step can still end on its evidence |
| Platforms | `labs/platforms/<name>/`: a `lab.yaml`, a `build.sh <label> <plugin>` and an `app/` (bg-forge's shape). `react-native`: Android, and iOS on CocoaPods and SwiftPM. `capacitor` (2026-11-12): Android, Tier 1 (every Android scenario; the shared ones are bridge-neutral, conformance stays in `api-smoke`). The iOS scenarios excuse it until its iOS host (both channels) exists. Flutter and Cordova follow (WO-002) |
| Suite | `sweep.SUITE`: the first slice (`termination-calibration`, `api-smoke`, `events-fetch-android`), then Tier 1 on react-native Android (`api-constants`, `events-initial-ignored`, `events-duplicate`, `events-task-android`, `events-timeout-sdk`, `stop-on-terminate`, `headless-kill`, `headless-activity-destroyed`) and the measurement `recreate-foreground-android` (WO-002, suspected defect 10); then iOS's (`ios-fetch-sim`, `ios-timeout-sim`, `ios-task-sim`, `ios-registration`, `api-smoke-ios`, and Tier 2's `ios-stop-start`) |
| Setup / doctor | The fetch submodules, `capacitor/background-fetch`, the `tsbackgroundfetch` pin through `publish_native_fetch.sh`. The iOS pin is not built yet, so `./labkit setup` without `--os android` stops at the iOS native step even when doctor counts it as a to-do |

**What a fetch lab driver logs, beyond bg-forge's protocol.** `LAB app=config json=<config>` before
each `configure()` call, and `LAB app=configure source=<boot|event-arm|…> outcome=<ok|rejected|skipped>`
when it settles (or the boot decision not to make one). `configureOutcome` and the evidence end both read
them. The app's `Application` logs `LAB point=appCreate pid=<pid> process=<process>` in every process,
headless ones included, which ties a process to the app (below).

## Host actions (Android)

These sit beside bg-forge's `kill: true` (`run-as <pkg> kill -9`), with the same `after:` / `delay_s`
grammar and the same `host i=<i> action=…` marks, scored by `actionsDone`:

```yaml
actions:
  - {after: {armed: configure@0}, delay_s: 2, job: 999}             # cmd jobscheduler run -f <pkg> 999 (the fetch job)
  - {after: {api: "scheduleTask#0"}, delay_s: 2, job: com.foo.task}  # a custom task: its jobId is the taskId's String.hashCode
  - {after: {point: host}, force_stop: true}                          # am force-stop <pkg>, then waits until no process is left
  - {after: {point: host}, delay_s: 3, job: 999, expect: absent}      # JobScheduler must answer "Could not find job"
  - {after: {event: configure@0}, delay_s: 3, job: 999,
     unspaced: "2026-11-09 inside the SDK's 5 s window: dropped"}      # a repeat fired inside the duplicate guard, on purpose
```

- **`job:`** must wait `delay_s: 1.5` or more after its conditions, because FetchJobService ignores a
  run less than 1 s after scheduling. A repeat of the same job on one device waits out the SDK's 5 s
  duplicate guard, and the step's window is budgeted for that wait.
- **`unspaced:`** (WO-002 Tier 1) fires a repeat without that wait, inside the SDK's duplicate guard, on
  purpose: firing inside the guards to get a delivery is a shortcut WO-002 rejects (the drop reads as an SDK
  defect), so it is a measurement only. It is a dated reason (`"YYYY-MM-DD <why>"`), only on a repeat of a
  job fired earlier in the step, never with `expect: absent`, and only in a step whose expect states
  `duplicateJobs` as a number. Its mark carries `unspaced since_ms=<ms since the same job last ran>`.
  `events-duplicate` is the one user.
- **`expect:`** says what the step expects of the job: `present` (the default: it runs) or `absent`
  (JobScheduler holds no such job). The other answer is a scored miss, `actionsDone:
  action<i>=missed(no-job)` or `missed(present)`; the step still runs. JobScheduler's answer rides in
  the mark as `said=` (non-word runs as `_`, 60 characters).
- **`force_stop:`** first reads `dumpsys jobscheduler <pkg>`; its mark carries the jobs JobScheduler held
  just before (`held=999`), which `jobsAtForceStop` reads.
- **`force_stop` versus `kill`.** Measured by `termination-calibration` on 2026-11-05, on both Lab
  emulators (Android 16): `am force-stop` cancels the app's fetch job (job 999), `run-as kill -9` keeps
  it. A headless scenario terminates with `kill: true`. run.py itself force-stops the app before and
  after every step, so each step configures again, and no job carries across a step boundary except
  through a `process: live` step.
- **`jobscheduler.txt`.** At the end of every Android step, before run.py's own end-of-step force-stop,
  the host reads `dumpsys jobscheduler <pkg>` into the capture, its header carrying the host's clock
  (`host_ms=`). The `jobIds` metric lists the job ids registered there; `jobStops` reads its `Job history:`.
- **How it is wired.** `job` and `force_stop` are two of labkit's own action kinds (`ACTION_HANDLERS`, bg-forge
  WO-191), registered by the pack: the vendored `_validate_moves` checks them (fetch's rules in the kind's
  `validate`) and the vendored `run_host` runs them. One loop runs every action and move of a step, so a slow
  `job:` (a repeat waiting out the duplicate guard, up to 5.5 s) or `force_stop:` (up to 2 s) delays an action or
  move due at the same time, and one that fails stops the step's remaining actions (unscored). Until WO-191 they
  ran on a companion thread; no shipped scenario has a step where the difference shows.

**Fetch metrics.** The Android SDK's own lines, counted in the window: `fetchReceived`, `fetchFinished`,
`fetchTimeouts`, `jobFinished`, `jobStopped`, `duplicateJobs`, `stoppingOnTerminate`,
`headlessTaskErrors`, `fetchTally`, and `jobsRegistered` (the jobIds of the SDK's `- registerTask:`
lines: what the SDK asked JobScheduler for, logged before the call, whose result it discards). From
JobScheduler itself: `jobIds` (step end) and `jobsAtForceStop` (the `force_stop` mark). Over the whole
log: `configureOutcome` (`ok`, `-` when no configure() was made, else `<source>=<outcome>` or
`unsettled=<n>`), `bootBroadcasts` (the SDK BootReceiver's broadcasts: on Android 16 every launch after an
`am force-stop` delivers BOOT_COMPLETED, and run.py force-stops before every step), and `foreignLines`.
bg-forge's generic metrics (`crashes`, `apiAssertions`, `eventAssertions`, `actionsDone`, …) apply as
they are.

Added for Tier 1 (WO-002, react-native Android), each from the app's own processes in the window:

| Metric | Reads |
|---|---|
| `jobStops` | JobScheduler's `Job history:` STOP rows for the app, `<jobId>:<reason>=n` (`999:onStartJob_returned_false=1`: a run the SDK ignored, which it logs nothing about; `app_called_jobFinished`: a delivered one). Kept from the step's `measure_from` mark (launch or trigger) on; `no-epoch` where a capture cannot place them |
| `jobsCancelled` | the jobIds of the SDK's `- cancel taskId=…, jobId=N` (stop(), stopOnTerminate's lazy stop, a reschedule) |
| `rnHeadlessInit`, `rnHeadlessStarts`, `rnHeadlessEnds`, `rnHeadlessErrors` | React Native's HeadlessTask: a ReactContext built (the cold path), each JS task started and ended, and its error lines |
| `headlessNoTask` | React Native's `No task registered for key BackgroundFetch` (the 4.4.2 cold-start race) |
| `headlessLatencyMs` | the slowest hand-over, ms from the SDK's receipt to the headless task's `point=headless event=fetch` (a headless timeout is not one) |
| `driverEvents`, `driverFinishes` | the foreground driver's own deliveries (`fetch=1,timeout=1`) and how it finished them (`fetch:withheld=1,timeout:ok=1`) |
| `appProcesses` | the app's processes started in the window (`point=appCreate`) |
| `fetchTimeoutSkewMs` | how far `[BGTask] timeout:` landed from 60 s after its receipt |
| `lifecycleTally` | the SDK's LifecycleManager lines, sorted (`headless:true=2,onStart=1`) |

**Which process is the app's.** Logcat is device-wide, and bg-forge's lab apps link TSBackgroundFetch
too. An SDK line counts only from a process tied to the capture's package: a `point=appCreate
process=<pkg>` line or an ActivityManager `Start proc <pid>:<pkg>/`. On a `process: live` step (its
process started before the step's log) the driver's `app=boot` process also counts. `foreignLines`
counts the SDK, `point=headless` and `point=task-` lines of every other process; it is implicit 0,
because bg-forge's headless metrics read those lines from every process and its contamination check
does not see them.

## Host actions and metrics (iOS)

(WO-002 phase 2a, D3) The Simulator has no JobScheduler and no usable BGTaskScheduler.  A fetch is fired the way
SpringBoard sends one: `xcrun simctl push <udid> <bundle>` with `{"aps":{"content-available":1}}` makes it deliver
UIKit's legacy background fetch, which the app's AppDelegate forwards to the SDK.  It works in the foreground,
suspended, and not running.  Three action kinds, iOS only, so a scenario using one excuses android:

```yaml
actions:
  - {after: {api: "status#0"}, delay_s: 3, fire: fetch}          # simctl push: exit 0, or 121 "no user visible content"
  - {after: {api: "status#0"}, delay_s: 2, background: true}     # Settings launched over the app (suspended within 1 s)
  - {after: {api: "status#0"}, delay_s: 2, terminate: true}      # simctl terminate: the next `fire` launches it
```

- **Relaunches:** a boot that follows the step's own `terminate` is the app relaunched in the background, not
  another lab's app.  The pack's wrap of `devices.contamination` excuses one boot per terminate, and every other
  finding stands.
- **Counts:** `fetchReceived` and `fetchFinished` count the iOS SDK's own lines on iOS
  (`[TSBackgroundFetch performFetchWithCompletionHandler]`, `[TSBackgroundFetch finish] … (n of m)`).  The Android-only
  counts read `-` there.
- **New metrics, iOS only:**
  - `legacyFetchStates`: the app state at each delivery, e.g. `background=1`.
  - `legacyResults`: what UIKit's completion handler was given, e.g. `newData=1`.
  - `springboardResults`: SpringBoard's own record of the result, e.g. `RetrievedNewData=1`. The log filter keeps
    SpringBoard's `SBBackgroundFetchTask` lines for this.
  - `legacyTimeoutAfterMs`: ms from delivery to the driver's `event=timeout`.
- **Custom tasks (`ios-task-sim`):** BGTaskScheduler can't launch one on the Simulator, so the lab app has a hook.
  `scheduleTask` rejects there, but the SDK has already listed the task with its callback.
  `fire: task` / `fire: task-timeout` with a `taskId:` posts a Darwin notification, `<bundle>/<kind>/<taskId>`
  (`simctl spawn <udid> notifyutil -p`). LabModule observes one for each task identifier the app's Info.plist
  permits, and calls that task's `execute` or `onTimeout`, as BGTaskScheduler and the expiration handler do on a
  device. A suspended app gets the notification only when it next resumes, so these steps keep the app in front.

  ```yaml
  - {after: {api: "scheduleTask#1"}, delay_s: 3, fire: task, taskId: com.transistorsoft.fetchlab.task}
  - {after: {event: configure@0}, delay_s: 5, fire: task-timeout, taskId: com.transistorsoft.fetchlab.task}
  ```

  - `taskFires`: what the hook did with each fire, e.g. `task:ran=1,task:missing=1` (`missing`: no such task, e.g.
    its finish destroyed it; `noCallback`: listed with no callback).
  - `taskSubmitErrors`: why the SDK couldn't submit, e.g. `BGTaskSchedulerErrorDomain:1=1` (the Simulator's
    Unavailable). `TSBackgroundFetch:0=1` would mean an identifier the Info.plist doesn't permit.
  - `sdkTasks`: the task identifiers the SDK lists at the step's end (the last tick's probe), `-` for none.
  - `driverTally` (both OSes): the driver's deliveries by kind and taskId, e.g.
    `fetch:com.transistorsoft.fetchlab.task=1`.
- **Launch-handler registration (`ios-registration`):** BGTaskScheduler logs each registration in the app's own
  process, under the `com.apple.BackgroundTasks` subsystem, which the log filter keeps. It logs
  `registerForTaskWithIdentifier: <id>`, then `Application finished launching` and `Handling app launch` for the
  launch. A step's `extras: {sdkLaunchCall: delegate|scene|none}` tells the host where to call the SDK's
  `didFinishLaunching()`:
  - `delegate` (the default, and what an OS launch with no arguments gets): in the app delegate, as the install
    docs say;
  - `scene`: in SceneDelegate, after the launch;
  - `none`: not at all, leaving only the SDK's `+load` path.

  The host logs `LAB point=didFinishLaunching … sdkLaunchCall=<value>` at each launch.
  - `bgRegistrations`: each registration in the app's processes, placed against those two lines, e.g.
    `com.transistorsoft.fetch:launching=1`. The places are:
    - `launching`: before `Application finished launching`, so from the app delegate;
    - `finished`: after it, before `Handling app launch` (the SDK's `+load` path);
    - `late`: after `Handling app launch`;
    - `unplaced`: a line the placing needs is missing;
    - `rejected`: the identifier isn't in the Info.plist.

    A second registration of one identifier crashes the app (BGTaskScheduler raises), so `crashes` catches it.
  - `sdkLaunchCalls`: where the host made its call, e.g. `scene=1`; `-` for none.
- **API smoke (`api-smoke-ios`):** `api-smoke`'s hooks on iOS, in a scenario of its own, as bg-forge splits an API
  scenario per OS. On the Simulator `scheduleTask` rejects (`outcome: rejected`, with the task identifier the iOS
  host permits), and BGTaskScheduler's own lines replace JobScheduler's reads:
  - `bgSubmits`: the requests the SDK handed BGTaskScheduler, by kind, e.g.
    `com.transistorsoft.fetch:appRefresh=1,com.transistorsoft.fetchlab.task:processing=1`. It is the counterpart of
    `jobsRegistered`, and like it, shows the SDK asked, not that the OS took the request.
  - `bgCancels`: the requests the SDK asked it to cancel, e.g. `com.transistorsoft.fetch=1`.
  - `sdkSubscribers`: the fetch listeners the SDK holds at the step's end (the last tick's probe), `-` for none.
    With `sdkTasks`, it is the counterpart of `jobIds`.

## Pinning

The native SDK a variant tests is a label in `labs/m2` (Android) and `labs/xcframeworks` (iOS), built from a recorded commit:

```
labs/harness/publish_native_fetch.sh 4.1.1-master-5de5242 labs/checkouts/native-5de52423e
```

- The checkout is a detached worktree of `native/background-fetch`
  (`git -C native/background-fetch worktree add --detach "$PWD/labs/checkouts/native-<sha>" <sha>`,
  with an absolute path). `labs/checkouts/` and `labs/m2/` are gitignored.
- The script publishes `com.transistorsoft:tsbackgroundfetch:<label>` with bg-forge's
  `lab-publish.init.gradle`, checks the POM and AAR carry the label, and writes
  `labs/m2/.provenance/<label>.txt` (native commit, dirty flag), which every capture's `run.json` records.
- A variant names the label (`native:`) and its commit (`native_source.android:` / `.ios:`); `./labkit setup`
  rebuilds a missing label from that commit.
- The React Native app resolves `tsbackgroundfetch` only from `labs/m2` and fails its build unless the
  resolved version is the label and the AAR's sha256 equals the one in `labs/m2`.
- **iOS** (WO-002 phase 2) is a label in `labs/xcframeworks`, built from the same worktree:

  ```
  labs/harness/publish_native_fetch_ios.sh 4.1.1-master-5de5242 labs/checkouts/native-5de52423e
  ```

  The script archives the Simulator slice alone, with the release build's Simulator flags, then signs it
  ad hoc. It never uses the checkout's committed `TSBackgroundFetch.xcframework`, which is the last
  release's binary. It checks that the slice's version and build match `versioning/TSBackgroundFetch.properties`,
  that the slice carries `PROJECT:TSBackgroundFetch-<build>`, and that its three headers are public. Then it
  publishes, beside the xcframework:
  - the checkout's podspec, stamped with the iOS version, which a Podfile pins by `:path`;
  - the checkout's `Package.swift`, its binary target pointed at the label's xcframework, with the Swift
    overlay's sources at the path that manifest names;
  - `labs/xcframeworks/.provenance/<label>.txt`: commit, dirty, marketing, build and `sha256_sim`.
- **One label, one native commit, two versions** (D6). The label is named after Android's version, and each
  OS's provenance records its own. At `5de5242` that is iOS 4.2.0 (4200) and Android 4.1.1, so a plugin
  podspec's `~> 4.2` resolves.

## Commands

```
./labkit doctor | setup [--os …]     tools, then the repo; doctor changes nothing
./labkit status | up | down          the Lab devices, shared with bg-forge's lab (same names, same claims dir)
./labkit sweep | run [switches]      the suite / one scenario
./labkit board | summarize           results
./labkit selftest                    fetch_selftest.py, the vendored slots selftest, vendor_check.sh — offline
./labkit vendor-check | pack | env   the vendored bytes; the pack's seams; the pack for a hand-run shell
```

## Devices

The four Lab devices are bg-forge's:
- the Android emulators Lab1 and Lab2, on emulator-5580 and emulator-5582;
- the iOS Simulators Lab1 and Lab2.

Both labs use the same machine-wide claims directory and sweep lock
(`~/Library/Caches/transistorsoft-lab/claims`). A fetch sweep and a bg-forge sweep therefore queue; they
never run side by side. Never set `LAB_CLAIMS_DIR` and never use a separate device.

Since bg-forge WO-191 each lab's first step evicts every other lab app on the device by package prefix
(`com.transistorsoft.labs.*`, `com.transistorsoft.fetchlab.*`), and on Android force-stops every other
Transistor app too (`labkit.evictions`, run.py:719-725): verified on both OSes and by back-to-back sweeps
(WO-002 Log, 2026-11-09).

A capture's path has no device in it (`captures/<platform>/<scenario>/<variant>/<step>`, bg-forge's
layout), so the last run of a cell overwrites the one before it. To keep a run, copy its step
directories under `labs/baselines/<date>-<what>/` (gitignored) before running the cell again.
