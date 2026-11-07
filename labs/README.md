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
- A vendored script run as `__main__` (`run.py` started by `sweep.py:644`, `lab_setup.py`'s re-exec) has
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
| Suite | `sweep.SUITE`: `termination-calibration`, `api-smoke`, `events-fetch-android` (the first slice) |
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
```

- **`job:`** must wait `delay_s: 1.5` or more after its conditions, because FetchJobService ignores a
  run less than 1 s after scheduling. A repeat of the same job on one device waits out the SDK's 5 s
  duplicate guard, and the step's window is budgeted for that wait.
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
  the host reads `dumpsys jobscheduler <pkg>` into the capture. The `jobIds` metric lists the job ids
  registered there.
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

**Which process is the app's.** Logcat is device-wide, and bg-forge's lab apps link TSBackgroundFetch
too. An SDK line counts only from a process tied to the capture's package: a `point=appCreate
process=<pkg>` line or an ActivityManager `Start proc <pid>:<pkg>/`. On a `process: live` step (its
process started before the step's log) the driver's `app=boot` process also counts. `foreignLines`
counts the SDK, `point=headless` and `point=task-` lines of every other process; it is implicit 0,
because bg-forge's headless metrics read those lines from every process and its contamination check
does not see them.

## Pinning

The native SDK a variant tests is a label in `labs/m2` (Android), built from a recorded commit:

```
labs/harness/publish_native_fetch.sh 4.1.1-master-5de5242 labs/checkouts/native-5de52423e
```

- The checkout is a detached worktree of `native/background-fetch`
  (`git -C native/background-fetch worktree add --detach "$PWD/labs/checkouts/native-<sha>" <sha>`,
  with an absolute path). `labs/checkouts/` and `labs/m2/` are gitignored.
- The script publishes `com.transistorsoft:tsbackgroundfetch:<label>` with bg-forge's
  `lab-publish.init.gradle`, checks the POM and AAR carry the label, and writes
  `labs/m2/.provenance/<label>.txt` (native commit, dirty flag), which every capture's `run.json` records.
- A variant names the label (`native:`) and its commit (`native_source.android:`); `./labkit setup`
  rebuilds a missing label from that commit.
- The React Native app resolves `tsbackgroundfetch` only from `labs/m2` and fails its build unless the
  resolved version is the label and the AAR's sha256 equals the one in `labs/m2`.
- iOS has no pin yet (WO-002 phase 2).

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

Each lab evicts only its own `platforms()` (run.py:708-714). Evicting every Transistor lab app by
package prefix needs a bg-forge work order (WO-002, "Lab devices").

A capture's path has no device in it (`captures/<platform>/<scenario>/<variant>/<step>`, bg-forge's
layout), so the last run of a cell overwrites the one before it. To keep a run, copy its step
directories under `labs/baselines/<date>-<what>/` (gitignored) before running the cell again.
