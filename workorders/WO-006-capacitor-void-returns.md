---
id: WO-006
title: Capacitor's scheduleTask() and stop() resolve undefined, where the shared types declare, and React Native resolves, true
status: draft
created: 2026-11-12
platforms: [android, ios]
submodules: [capacitor/background-fetch]
branch: fix/WO-006-capacitor-void-returns
issues: []
supersedes: []
---

> **Found by** WO-002's new Capacitor Android host, on its first full Tier 1 sweep: `20261112-113743`, variant `FIX`.
> - React Native passed 12 of 12. Capacitor passed 9 and failed 3; this is one of the 3.
> - The Tier 1 design predicted this red from source before the run. `labs/harness/scenarios/api-smoke.yaml:25-28` records that prediction.
>
> **Evidence markers:** **MEASURED** = seen in a capture, a log or a compiler run; **PROVEN** = read in source; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at. Android is MEASURED. iOS is PROVEN only: there is no Capacitor iOS host yet.
>
> **Citation bases:**
> - **Bridges and types**, at their checked-out commits: Capacitor `9dd204a` (8.1.4 plus Unreleased), React Native `85a6bf4`, Cordova `38ad0fe`, Flutter `11d53b8`, types `5c9685d` (1.0.0).
> - **Native:** `399765c`, TSBackgroundFetch 4.2.1 (lab label `4.1.1-master-399765c`). Its `android/` is identical to `5de5242`'s: `git diff` is empty.
> - **Capacitor's runtime:** 8.5.3, the lab host's (`labs/platforms/capacitor/app/package.json:6-8`). Its files are cited under that host's `node_modules/@capacitor/`.
> - **Lab files:** cited at the working tree. `api-smoke.yaml` and the Capacitor captures were not yet committed when this was written. `run.json` and `summary.txt` are the tracked capture files; `logcat.txt` exists on disk only.

## Diagnosis

**MEASURED on Capacitor Android** (Lab1 emulator-5580, Android 16). api-smoke fails on `apiAssertions` alone. Every other row is `ok`. The failing row reads `scheduleTask#4.$=want-boolean-got-undefined, stop#5.$=want-boolean-got-undefined, stop#7.$=want-boolean-got-undefined` (`labs/captures/capacitor/api-smoke/FIX/run/summary.txt`). From that capture's `logcat.txt`:

```
:422  11:44:16.050 I/Capacitor/Console(12341): ... LAB app=ret id=scheduleTask#4 path=$ type=undefined parent=1
:430  11:44:16.057 I/Capacitor/Console(12341): ... LAB app=ret id=stop#5 path=$ type=undefined parent=1
:445  11:44:16.063 I/Capacitor/Console(12341): ... LAB app=ret id=stop#7 path=$ type=undefined parent=1
```

- **The SDK did the work. Only the value handed back is wrong.**
  - `- registerTask: com.transistorsoft.fetchlab.smoke (jobId: -592898938)` (`:404`).
  - `- cancel taskId=com.transistorsoft.fetchlab.smoke, jobId=-592898938` (`:427`).
  - `- cancel taskId=capacitor-background-fetch, jobId=999` (`:442`).
  - The `jobsRegistered` and `jobIds -` rows pass.
- **Native sent no data.** Capacitor's bridge echoes each native result to the console:
  - configure, status and start echo `Msg: {"status":2}` (`:365`, `:374`, `:398`);
  - scheduleTask, stop and finish echo `Msg: undefined` (`:420`, `:428`, `:435`, `:443`).

  That this echo comes from `native-bridge.js:329` (`c.dir(JSON.stringify(result.data))`) is INFERRED from the console's line number. The page's copy of the bridge is offset by four lines.
- **React Native, in the same sweep on the same native label,** reads `type=boolean value=true` for all three calls (`labs/captures/react-native/api-smoke/FIX/run/logcat.txt:614, :618, :625`). On the iOS Simulator, `stop#5` and `stop#7` read `true` too (`labs/captures/react-native-ios/api-smoke-ios/FIX/run/logcat.txt:39, :46`).

**PROVEN in source: two layers drop the value, and the outer one alone is enough.**
1. **The JS wrapper, which both OSes share** (`capacitor/background-fetch/src/index.ts`):
   - `:245` declares `scheduleTask(config:TaskConfig):Promise<void>`, and `:255-256` resolve nothing when the native call resolves;
   - `:328` declares `stop(taskId?:string):Promise<void>`, and `:330-331` do the same;
   - so anything native sends is discarded. By contrast, configure, start and status forward `result.status` (`:202`, `:298`, `:386`).
2. **The native plugins resolve with no data.**
   - **Android:** `BackgroundFetchPlugin.java:84` (stop) and `:93` (scheduleTask) call a bare `call.resolve()`. That sends no `data` (`PluginCall.java:58-60` → `MessageHandler.java:116-118`), so `native-bridge.js:965` resolves `result.data`, which is undefined.
   - **iOS:** `BackgroundFetchPlugin.swift:93` (stop) and `:119` (scheduleTask) call `call.resolve()`, which is `CAPPluginCallResult(nil)` (`CAPPluginCall.swift:37-39`).
3. **A Capacitor plugin can resolve only an object, never a bare boolean** (`PluginCall.java:53` `resolve(JSObject)`; `CAPPluginCall.swift:41-43`). So whichever side supplies `true`, the JS wrapper has to produce it.

**What the types declare (PROVEN)**, in `types/background-fetch/src/BackgroundFetch.ts`:
- `:167` `scheduleTask(config: TaskConfig): Promise<boolean>`, "resolving to `true` if the task was successfully scheduled" (`:148`);
- `:203` `stop(taskId?: string): Promise<boolean>`, "resolving to `true` on success" (`:192`).

The lab's vocabulary uses the same signatures (`labs/harness/vocabulary.yaml:43, :46`), and so does the docs site (`docforge/db/BackgroundFetch.scheduleTask.yaml:83`, `docforge/db/BackgroundFetch.stop.yaml:41`). api-smoke holds every bridge to the types on purpose: "That red is a finding for its own work order, never relaxed here" (`api-smoke.yaml:27-28`).

**Why nothing caught it (PROVEN, and MEASURED with a compiler):**
- **Capacitor's `BackgroundFetch` class is its own.** `src/definitions.ts:1-10` re-exports only the types package's interfaces and aliases, not its class (`BackgroundFetch.ts:41`).
- **React Native is typed by the types' class itself.** Its `index.d.ts:1-8` re-exports that class.
- **Capacitor's `.d.ts` is compiled from `index.ts`,** so it says `Promise<void>` (`dist/esm/index.d.ts:178, :235`, from a local build).
- **A compiler check shows exactly which members differ.** I assigned Capacitor's class to a `Pick` of the types' class over `configure`, `scheduleTask`, `start`, `stop`, `finish` and `status`. It fails, and tsc names only `scheduleTask`, the first mismatch (it reports one). A `Pick` over `stop` alone fails the same way, and one over the other four members compiles (MEASURED: TypeScript 4.1.6, the checkout's own `~4.1.5`, run on scratch files; no repo file touched).

**The native SDK has nothing to report beyond success or error (PROVEN).** Native at `399765c`:
- **Android:** the SDK's `stop(String)` and `scheduleTask(BackgroundFetchConfig)` are void (`BackgroundFetch.java:140, :178`). A stop for an unknown id silently does nothing (`:162-173`). JobScheduler's `schedule()` result is discarded (`BGTask.java:169`).
- **iOS:** `-stop:` is void (`ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.h:45`). `-scheduleProcessingTaskWithIdentifier:…` returns an `NSError` (`:37`), and Capacitor iOS already turns that into a reject (`BackgroundFetchPlugin.swift:115-117`).

So on every bridge that resolves `true`, React Native's included, it means only that the call returned without error.

**Not a regression (PROVEN).** `Promise<void>` dates from the plugin's first working commit, `53aa1c5` (2021-06-01). Release 8.1.0 moved the interfaces to the shared types package but kept Capacitor's own class (`CHANGELOG.md:21-22`).

## Proposed fix

Change `src/index.ts`:
- Declare `scheduleTask` as `Promise<boolean>` and resolve `true` when the native call resolves (`:245`, `:255-256`).
- Do the same for `stop` (`:328`, `:330-331`).
- Add the types' `@returns` lines to both JSDoc blocks (`BackgroundFetch.ts:148`, `:192`).
- Leave the rejections as they are.

Under D1, no native file changes. The anchor `// (WO-006)` goes at each change. The CHANGELOG entry carries `(WO-006)`, for example:

> * [Changed] `scheduleTask()` and `stop()` resolve `true`, as `@transistorsoft/background-fetch-types` declares; their return type is now `Promise<boolean>` (was `Promise<void>`). (WO-006)

**Decisions:**

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Where the `true` comes from | (a) The JS wrapper resolves `true` after the native call resolves. (b) Each native plugin resolves `{…: true}` and the wrapper forwards it, as it does `status`. | (a). The wrapper must change under either option (point 3 above), and native has nothing to report. One file fixes both OSes, including iOS, with no native rebuild and without waiting for the iOS host. |
| D2 | What `stop(taskId)` resolves when nothing was stopped | (a) `true` whenever the call returns, as React Native does on both OSes (`RNBackgroundFetchModule.java:69-73`; `RNBackgroundFetch.mm:86-97`). (b) `false`. | (a). Option (b) needs the native SDK to report what it stopped on both OSes (both are void today) and every bridge to follow. That is its own work order. |
| D3 | The `.d.ts` change, and which release carries it | (a) The next release, which already carries WO-001 (`CHANGELOG.md:3-7`), with a `[Changed]` line. (b) Hold it for the next major. | (a). The types and the docs site already promise `boolean`. The break is narrow: at compile time for TypeScript callers that typed the result as `void`, and at runtime only for an untyped-JavaScript truthiness test on the result (see the Scope check). 8.1.0 changed the type surface in a minor release (`CHANGELOG.md:21-22`). |
| D4 | A guard against future drift | (a) preflight's consumer (`scripts/preflight.sh:88-114`) assigns the class to a `Pick` of the types' class over the members Capacitor has, and reads both results as `boolean`. (b) Only the two `boolean` reads. (c) No guard. | (a). It fails today on exactly these two members (MEASURED above), and preflight runs on every release (`preflight.sh:2-3`). Leave `registerHeadlessTask` out of the `Pick`: Capacitor has none, by design (`api-smoke.yaml:13-16`). That the consumer can import the types package, which the plugin's tarball brings in as a dependency, is INFERRED. |

## Scope check

- **Callers in the repo all ignore the value, so they compile either way** (INFERRED):
  - `example/src/app/home/home.page.ts:86` (`await BackgroundFetch.stop()`) and `:122`;
  - `scripts/preflight.sh:104`;
  - `help/INSTALL-IOS.md:41`.
- **The lab:**
  - `labs/platforms/_shared/labapi.js:405` awaits the value.
  - events-duplicate and events-task-android assert scheduleTask only as `outcome: resolved` (`events-duplicate.yaml:40-47`, `events-task-android.yaml:32-40`), so they pass either way.
- **Capacitor TypeScript users.** `Promise<boolean>` is not assignable to `Promise<void>`, so these stop compiling (MEASURED with tsc 4.1.6 on a scratch file; TypeScript 5 NOT CHECKED):
  - a `Promise<void>` annotation on the result;
  - passing `BackgroundFetch.stop` where a `() => Promise<void>` is expected;
  - a test double typed against the class that resolves `undefined` (under `strictNullChecks` only; without it `undefined` is assignable to `boolean`);
  - `.then((v: void) => …)`.

  A bare `await` or a fire-and-forget call is unaffected.
- **Plain JavaScript users.** A truthiness test on the result changes from `undefined` to `true`. TypeScript refuses that test on a `void` value with or without `strict` (TS1345, MEASURED with tsc 4.1.6), so only untyped JavaScript, or code that cast the result, could have written one (INFERRED).
- **Unchanged:**
  - **`finish()`** (`index.ts:356-357`) already conforms: its `Promise<void>` is assignable where the types declare `void` (MEASURED in the same check).
  - **Native callers and the native SDK** (under D1).
  - **The docs site,** which already says `Promise<boolean>`. Whether its Capacitor page shows the TypeScript signature is NOT CHECKED.
  - **The checked-in typedoc `docs/`,** which shows `Promise<void>` (`docs/classes/backgroundfetch.html:513, :664`; last regenerated `06cf615`, 2025-07-31, before 8.1.0). Regenerating it, or leaving it stale, is a decision for the release.

## Platform parity

- **Android:** affected, MEASURED (see the Diagnosis). The plugin is `BackgroundFetchPlugin.java:78-85, :87-98`, under the shared wrapper (`index.ts:245-260, :328-336`).
- **iOS:** affected, PROVEN in source, not measured. The same wrapper discards whatever Swift sends, so `index.ts` alone makes the JS result undefined on iOS. Swift sends nothing anyway (`BackgroundFetchPlugin.swift:86-94, :96-121`).
- **The other bridges, for the record. None of them is in this order's scope.**
  - **React Native conforms:** MEASURED on Android for all three calls and on the iOS Simulator for `stop` (see the Diagnosis). Its iOS `scheduleTask` resolve is PROVEN only (`RNBackgroundFetch.mm:119`), because it rejects on the Simulator.
    - Its native modules resolve `true` / `@(YES)` (`RNBackgroundFetchModule.java:58, :73`; `RNBackgroundFetch.mm:96, :119`).
    - Its JS passes the promise straight through (`index.js:61-63, :73-75`).
    - Its TurboModule spec declares `Promise<boolean>` (`src/NativeBackgroundFetch.ts:7, :10`).
  - **Cordova has a callback API,** so the question does not arise in the same form.
    - `stop(success?, failure?)`, `scheduleTask(config, success?, failure?)` and `stopTask(taskId, …)` all return `void` (`src/typescript/index.ts:79-81, :90-95, :97-102`).
    - Its native success callbacks carry no value on either OS (Android `CDVBackgroundFetch.java:88, :94`; iOS `CDVBackgroundFetch.m:106, :129`).
    - It needs its own api-smoke policy, which WO-002 has not settled.
  - **Flutter's `stop` is `Future<int>` and resolves the status.**
    - The Dart signature is `lib/background_fetch.dart:524-527`. Android resolves the status at `BackgroundFetchModule.java:169`, and iOS at `BackgroundFetchPlugin.m:139`.
    - Its `scheduleTask` is `Future<bool>` and resolves `true` (Dart `:575-577`, Android `:188`, iOS `:172`).
    - docforge records Dart's `stop` as `Future<int>` (`BackgroundFetch.stop.yaml:42`). The Dart surface is not held to the TypeScript types.

## Test plan

- **Compile-time red/green (D4).** Add `(WO-006)` assertions to preflight's consumer:
  - `const scheduled: boolean = await BackgroundFetch.scheduleTask(task)`;
  - the same `boolean` read for `stop(task.taskId)` and for `stop()`;
  - D4's `Pick` assignment, if taken.

  Run them at `9dd204a` first and record the red, then run them green on the fix. preflight compiles with TypeScript 5 (`preflight.sh:86`), so the red and the green are measured on 5.x. Whether `Promise<void>`-typed callers break on 5.x (the Scope check's list) stays NOT CHECKED unless those patterns are also compiled with TypeScript 5 on a scratch file. `npm run build` must stay clean.
- **Lab, Android.** Run `api-smoke` on Capacitor, variant `FIX`, with the plugin label at the fix commit.
  - `scheduleTask#4`, `stop#5` and `stop#7` must read `type=boolean value=true`.
  - `jobsRegistered -592898938,999` and `jobIds -` must be unchanged.
  - Also run Capacitor's events-duplicate and events-task-android, which call scheduleTask.
  - Run React Native's api-smoke as a guard. React Native is untouched.
- **Lab, iOS, once WO-002's Capacitor iOS host exists.**
  - `api-smoke-ios` takes Capacitor: add it to `platforms` (`api-smoke-ios.yaml:33`), drop its excusal (`:35-36`), and give registerHeadlessTask an `absent_on: capacitor` (`:45-47`), as `api-smoke.yaml:65-67` has. Then `stop#5` and `stop#7` must read `true`.
  - `scheduleTask#4` rejects on the Simulator (`:52-54`), so scheduleTask's iOS resolve can't be measured there. It goes through the same wrapper line as Android's (INFERRED). A device check is optional.
- **No JS unit tests.** The plugin has no JS test runner (`package.json` scripts), and adding one is out of scope.

## Out of scope

- **Capacitor iOS `start()`** is declared `CAPPluginReturnCallback` (`BackgroundFetchPlugin.swift:13`; WO-002 suspected defect 1). What JS receives from it is NOT CHECKED. Measure it on the iOS host first, and do not change `start`'s row in `pluginMethods` in passing.
- **The sweep's other two Capacitor reds,** `stop-on-terminate/finish` and `headless-activity-destroyed`, are separate defects.
- **`index.ts:249`'s `delay === NaN` check is always false** (PROVEN). So a non-numeric `delay` is not rejected and is sent on as NaN. Android's bridge serializes it to `null` (`native-bridge.js:843`, `JSON.stringify`). What each native plugin then does with it is NOT CHECKED.
- **The wrapper rejects with `error.message`,** a string rather than an Error (`:258`, `:333`).
- **Capacitor Android's scheduleTask rejects on `JSONException`** (`BackgroundFetchPlugin.java:94-97`), where React Native Android's never rejects (`RNBackgroundFetchModule.java:55-59`).
- **Making the SDK report what `stop` actually stopped** (D2's option b). Also left out: a sentence in the types' JSDoc saying `true` does not mean a task existed, which would be a `types/background-fetch` change.
- **Cordova's callback API and Flutter's `int`-returning `stop`.**

## Not an acceptable shortcut

- **Relaxing api-smoke for Capacitor,** whether by an `absent_on`, a per-platform `returns` or `outcome: resolved`. api-smoke exists to hold every bridge to the types (`api-smoke.yaml:25-28`). The other scenarios were loosened on purpose; this one never is.
- **Changing the types to `Promise<void>` or `Promise<boolean | void>`.** React Native is typed by them and resolves `true`, and the docs say `boolean`.
- **Resolving `true` at runtime while the `.d.ts` stays `Promise<void>`.** api-smoke would go green, but TypeScript users still could not use the value as a boolean (TS2322 on a `boolean` assignment, TS1345 on a truthiness test), and the drift would stay invisible.
- **Fixing it in native only.** The wrapper discards whatever native sends (`:256`, `:331`), so api-smoke stays red.
- **Resolving the native object (`{value: true}`) or the status (as Flutter's `int` does).** The types say `boolean`, and api-smoke asserts `equals: true`.
- **Fixing Android now and leaving iOS "until the host exists".** Under D1 (a), the fix is in the one wrapper file that both OSes share (`src/index.ts`). Under D1 (b), the Java and the Swift change in the same commit.
- **Resolving `false` for "nothing stopped" on Capacitor alone.** That diverges from React Native and needs the SDK to report it (D2).

## Verification

- [ ] D1–D4 decided
- [ ] preflight's `(WO-006)` consumer assertions red at `9dd204a`, green on the fix; `npm run build` clean
- [ ] Capacitor Android `api-smoke` green on the fix's label: the three hooks read boolean `true`, the job rows are unchanged
- [ ] Capacitor events-duplicate and events-task-android still green; React Native `api-smoke` still green
- [ ] Capacitor iOS `api-smoke-ios`: `stop#5` and `stop#7` read `true`, once the host exists (WO-002)
- [ ] `api-smoke.yaml:25-28` updated from PREDICTED to MEASURED, naming WO-006 and its red and green sweeps
- [ ] `events-duplicate.yaml:40-41` and `events-task-android.yaml:32-33` updated: scheduleTask resolves `true` on both bridges (WO-006); their `outcome: resolved` hooks stay as they are
- [ ] CHANGELOG `[Changed]` entry carries `(WO-006)`, in the release D3 picks
- [ ] Platform parity: both OSes fixed through the one wrapper, as cited; React Native conforms (MEASURED on Android; on iOS, stop MEASURED, scheduleTask PROVEN); Cordova and Flutter recorded, not changed

## Log

- 2026-11-12 — **Created (draft)** from WO-002's Capacitor Android Tier 1 sweep `20261112-113743` (Capacitor 9 PASS, 3 FAIL; React Native 12/12 PASS). api-smoke read red exactly as the Tier 1 design had predicted from source.
  - **Checked by hand while writing:**
    - the Capacitor, React Native, Cordova and Flutter lines, at their checked-out commits;
    - the native lines at `399765c`, and the types at `5c9685d`;
    - Capacitor 8.5.3's bridge, in the lab host's `node_modules`.
  - **Measured while writing:** the TypeScript assignability results above, with tsc 4.1.6 on scratch files. No repo file was touched.
  - **Not done yet:** no branch, no code, nothing run on iOS.
  - **Corrected after two skeptics' review** (16 corrections; 0 declined). The `Pick` over `stop` alone, and the user patterns without `strict` and with `strictNullChecks` alone, were compiled then, with tsc 4.1.6 on scratch files.
