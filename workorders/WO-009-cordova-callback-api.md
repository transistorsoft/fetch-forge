---
id: WO-009
title: Cordova's start(), stop(), status() and scheduleTask() return nothing where the shared types declare Promises; stop(taskId) throws on iOS and cancels every task on Android; on iOS start's native half is unordered against a later stop
status: done
created: 2026-11-16
platforms: [android, ios]
submodules: [cordova/background-fetch]
branch: fix/WO-009-cordova-callback-api
issues: []
supersedes: []
---

> **Found by** WO-002's Cordova hosts, on their first sweeps (Android and iOS, 2026-11-15). Both of WO-002's Log entries list it under "Open, for work orders" (`WO-002-background-fetch-labkit.md:1271`, `:1312`), and its 2026-11-16 entry points both to this order (`:1329-1330`). Chris's decision (`:1258`, `:1278`): the reds are findings; api-smoke and api-smoke-ios keep scoring the types' contract; the `status` anchors in the other scenarios assert `outcome: resolved`.
>
> **Evidence markers:** **MEASURED** = seen in a lab capture or a sweep record; **PROVEN** = read in source; **INFERRED** = reasoning, not run; **SUSPECTED** = a defect inferred from source that no device run reproduced; **NOT CHECKED** = not looked at.
>
> **Citation bases.** Source read on 2026-11-16. Nothing was checked out, built, compiled or run.
> - **Cordova plugin:** `cordova/background-fetch` `368eb85` (7.3.1 plus Unreleased). Its paths are relative to that submodule.
> - **Other bridges and types:** React Native `8d9ab19`, Capacitor `3fa169a`, types `dc4f5b8`.
> - **Native:** TSBackgroundFetch 4.2.1 (iOS) / tsbackgroundfetch 4.1.1 (Android), both at `native/background-fetch` origin/master `399765c` (lab label `4.1.1-master-399765c`, whose number is Android's: WO-002 Log `:1043`), read with `git show 399765c:<path>`; the checkout sits on `0670713`. Lines are cited `native/background-fetch/<path>:<line> (399765c)`. In the tables, `TSBackgroundFetch.m` stands for `native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.m` and `BackgroundFetch.java` for `native/background-fetch/android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/BackgroundFetch.java`, always at `399765c`.
> - **Cordova runtimes:** cordova-android 15.0.0 and cordova-ios 8.1.1, the lab host's copies under `labs/platforms/cordova/app/node_modules/` (installed by `npm ci` from the committed lock, `labs/platforms/cordova/app/package-lock.json:180-182, :223-225`; not tracked).
> - **Captures:** Android sweep `20261115-215127` (Lab2 `emulator-5582`, Android 16). iOS sweeps `20261115-223641` (FIX) and `20261115-224122` (FIX-SPM), Simulator iOS 27.2, cordova-ios 8.1.1, Swift Package Manager on both (WO-002 Log `:1277`). These are the captures committed at `0c596d2`.
>   - `run.json` carries no sweep id. Each capture's sweep is the one whose record has its `RESULT` line (`labs/harness/.claims/sweep-<id>/stdout.log`), and its `when` stamp falls inside that sweep.
>   - Git keeps only `run.json` and `summary.txt` (`labs/.gitignore:6-10`). `logcat.txt` line numbers were the on-disk files until WO-009's own sweeps overwrote them on 2026-11-16 (Log): those logcats survive nowhere. The sweep records are gitignored too (`:14`) and on disk in this worktree.

## Diagnosis

**MEASURED on Cordova Android** (`labs/captures/cordova/api-smoke/FIX/run`, sweep `20261115-215127`). api-smoke fails on `apiAssertions` alone. jobsRegistered `-592898938,999`, jobIds `-` and every other row are `ok`. The failing row (`summary.txt`): `status#2.$=want-number-got-undefined, start#3.$=want-number-got-undefined, scheduleTask#4.$=want-boolean-got-undefined, stop#5.$=want-boolean-got-undefined, stop#7.$=want-boolean-got-undefined`. From `logcat.txt`:

| Lines | What |
|---|---|
| `:328-330` | `[CDVBackgroundFetch] status()`, then `status#2 … type=undefined` |
| `:331-351` | `[CDVBackgroundFetch] start()`, `- registerTask: cordova-background-fetch (jobId: 999)`, then `start#3 … type=undefined` |
| `:352-372` | scheduleTask registers job `-592898938`; then `W/CordovaPlugin: Attempted to send a second callback for ID: BackgroundFetch505706847` / `Result was: "Invalid action"`; then `scheduleTask#4 … type=undefined` |
| `:373-378` | stop#5: `[CDVBackgroundFetch] stop()`, `- stop` with no taskId, then `- cancel taskId=cordova-background-fetch, jobId=999` AND `- cancel taskId=com.transistorsoft.fetchlab.smoke, jobId=-592898938`; `stop#5 … type=undefined` |
| `:383-386` | stop#7: `- stop`, nothing left to cancel; `type=undefined` |
| `:391` | `"Uncaught (in promise) TypeError: callback.success.apply is not a function", source: https://localhost/cordova.js (295)` |

- **status, start and scheduleTask did their native work.** Only the values are missing.
- **stop#5 did the wrong work.** stop(taskId) stopped every task, the fetch job included. jobIds `-` cannot tell (`api-smoke.yaml:63`): stop#7 would have cancelled the fetch job anyway.

**MEASURED on Cordova iOS, both channels** (sweeps `20261115-223641` FIX, `-224122` FIX-SPM).
- **api-smoke-ios `run`** (`labs/captures/cordova-ios/api-smoke-ios/{FIX,FIX-SPM}/run/summary.txt`): `status#2.$=want-number-got-undefined, start#3.$=want-number-got-undefined, scheduleTask#4=want-rejected-got-resolved, stop#5=threw, stop#7.$=want-boolean-got-undefined`.
  - **scheduleTask#4:** native failed (`[TSBackgroundFetch scheduleTask] ERROR:  Failed to submit task request: … Code=1`, FIX `logcat.txt:32`). The failure went to the plugin's empty function, and the call returned undefined, which the driver reads as resolved (see "How the lab reads" below).
  - **stop#5:** `outcome=threw` (FIX and FIX-SPM `:35`), and no `[TSBackgroundFetch stop] com.transistorsoft.fetchlab.task` follows: nothing reached native. React Native and Capacitor log that line at stop#5 (`labs/captures/react-native-ios/api-smoke-ios/FIX/run/logcat.txt:35`; `labs/captures/capacitor-ios/api-smoke-ios/FIX/run/logcat.txt:37`).
- **FIX-SPM reads two more rows red:** `bgSubmits com.transistorsoft.fetch:appRefresh=2,…` and `sdkSubscribers cordova-background-fetch`. Its `logcat.txt`:

  ```
  :40  22:44:13.427 [TSBackgroundFetch stop] (null)        stop#7's native half
  :43  22:44:13.428 app=api id=stop#7 outcome=ok
  :45  22:44:13.428 app=api-done n=8
  :47  22:44:13.428 [TSBackgroundFetch start] (null)       start#3's native half
  :49  22:44:13.428 submitTaskRequest: <BGAppRefreshTaskRequest: com.transistorsoft.fetch …>
  ```

  start#3's native half ran after stop#7's. It re-added the listener and submitted the fetch request again: the tick at `:59` reads `subscribers=cordova-background-fetch`.
- **FIX reads those two rows green.** There start's native half ran between stop#5 and stop#7 (`:36`, before `[TSBackgroundFetch stop] (null)` at `:41`). The SDK's `fetchScheduled` guard (`native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.m:104 (399765c)`) made it submit nothing, and stop#7 then removed the listener.
- **So the ordering is a race, and each channel has read it both ways.** Six Cordova iOS sweeps ran api-smoke-ios at `368eb85` (each sweep's `stdout.log` `RED` line and its `cordova-ios-api-smoke-ios.log`):
  - **bgSubmits red (`appRefresh=2`) in four:** `20261115-212810` (FIX, Lab1), `-220040` (FIX), `-220507` (FIX-SPM) and `-224122` (FIX-SPM).
  - **Green (`appRefresh=1`) in two:** `-213250` (FIX-SPM, Lab1) and `-223641` (FIX). FIX and FIX-SPM build the same SwiftPM app (WO-002 Log `:1277`).
  - **bgSubmits counts native lines alone,** in the processes whose host AppDelegate logged `didFinishLaunching` (`labs/harness/fetch_pack.py:922-925, :970-980`). So it scored even in `-212810` and `-213250`, whose driver lines never reached the capture (WO-002 Log `:1291`); there sdkSubscribers reads `no-probe`. In the other four it agrees with bgSubmits. The logcats of `-212810` and `-213250` were overwritten, so the ordering behind their rows is INFERRED: start's native half after stop#7's in `-212810`, before it in `-213250`.
  - **The `.647`/`.648` times** WO-002 quoted (`:1303`; `api-smoke-ios.yaml:52-53` at `0c596d2`) are in no committed capture and no sweep record. That they are `-220040`'s is INFERRED from `api-smoke-ios.yaml:43, :52-53` at `0c596d2`, which named `-220040` as the FIX sweep and marked the times "MEASURED, FIX". `-212810`, also FIX, also `appRefresh=2` and with its native lines captured (WO-002 Log `:1291`), is the other candidate. `-223641` overwrote that capture either way, so the times are NOT CHECKED here.
  - **Corrected in WO-002's 2026-11-16 entry** (`:1347`): it calls bgSubmits and sdkSubscribers a race and gives the `.647`/`.648` to `-220040`. The scenario now cites `-223641`/`-224122` and quotes the committed times (`api-smoke-ios.yaml:43, :52-54`).
- **ios-stop-start** (`labs/captures/cordova-ios/ios-stop-start/{FIX,FIX-SPM}/{stopped,restarted}`): `stopped` reads `stop#1.$=want-boolean-got-undefined`; `restarted` reads that and `start#2.$=want-number-got-undefined`. Every other row passes, bgSubmits `appRefresh=3` and sdkSubscribers included.
  - The call returns before native has started: `restarted/logcat.txt:30`, `ret id=start#2 … type=undefined` at .457; `:33`, `[TSBackgroundFetch start]` at .458 (FIX).
  - The fetch fires 3 s after start#2 (`ios-stop-start.yaml:76` at `0c596d2`), so the race cannot show in this scenario.

**PROVEN: the JavaScript, which both OSes share.**
- **What ships.** `src/typescript/index.ts` is the source. Cordova loads `www/index.js` (`plugin.xml:20-22`; `package.json:5`), and TypeScript reads `src/typescript/index.d.ts` (`package.json:9`). Both are compiled from `index.ts` and committed (`src/typescript/tsconfig.json:4-5, :11`). preflight refuses a release where either differs from a fresh compile (`scripts/preflight.sh:49-55`, run by `.github/workflows/release.yml:55`). Read side by side, every method body in `www/index.js:12-65` is `index.ts:44-106`'s. That the files are byte-equal to a compile is INFERRED: preflight was not run.
- **The methods:**

| Method | Declared (`index.d.ts`) | Passes to `exec` (`index.ts`) | Returns |
|---|---|---|---|
| configure | `:19` `(config, onEvent, onTimeout): Promise<BackgroundFetchStatus>` | `:70` `exec(onEvent, onTimeout, …, 'configure', [config])`, then `:71` `exec(onStatus, onStatus, …, 'status', [])` | a Promise settled by the status call: resolves 2, rejects with the bare status number (`:63-69`) |
| start | `:20` `(success?, failure?): void` | `:76` `exec(success \|\| EMPTY_FN, failure \|\| EMPTY_FN, …, 'start', [])` | undefined |
| stop | `:21` `(success?, failure?): void` | `:80` the same, `'stop', []` | undefined |
| finish | `:22` `(taskId, success?, failure?): void` | `:87` `'finish', [taskId]` | undefined |
| scheduleTask | `:23` `(config, success?, failure?): void` | `:94` `'scheduleTask', [config]` | undefined |
| stopTask | `:24` `(taskId, success?, failure?): void` | `:101` `'stop', [taskId]` | undefined |
| status | `:25` `(callback): void` | `:105` `exec(callback \|\| EMPTY_FN, EMPTY_FN, …, 'status', [])` | undefined |

- **stop has no taskId parameter.** `stop(taskId)` passes the string as `success`, and sends `[]` (`:80`). Cordova's own form is `stopTask(taskId)` (`:97-102`), which the types do not have.
- **What `exec` does with a string as its first argument:**
  - **Android** (cordova-android 15.0.0, `templates/project/assets/www/cordova.js`): `androidExec` registers it as the success callback (`:989`). The native reply calls `callback.success.apply` (`:295`), which throws on a string. MEASURED: `logcat.txt:391`.
  - **iOS** (cordova-ios 8.1.1, `templates/project/www/cordova.js`): `iOSExec` throws "The old format of this exec call has been removed" when its first argument is a string (`:929-947`), before anything is sent. MEASURED: `stop#5 outcome=threw`.

**PROVEN: the native halves.** start and status already send the status the types declare. Where the types declare `true` (stop, scheduleTask), neither OS sends it: Android's bare `success()` sends the string `"OK"`, and iOS sends null. Each replies to start after its native work.

| | Android `src/android/CDVBackgroundFetch.java` | iOS `src/ios/CDVBackgroundFetch.m` |
|---|---|---|
| configure | `:53-72`: no reply of its own. onFetch sends OK and onTimeout ERROR on configure's callbackId, both kept | `:51-71`: the same, through `:156-174`. A status other than available is only logged (`:67-69`) |
| start | `:74-79`: `adapter.start(…)`, synchronous (`BackgroundFetch.java:128-137, :246-262`), then `success(adapter.status())` | `:73-93`: all of it inside the SDK's status block, which is `dispatch_async` on the main queue (`TSBackgroundFetch.m:244-249`): `addListener`, `start:nil`, then OK with the status as an int, or ERROR with the status (`:89`) or the start error's code (`:85`) |
| stop | `:81-89`: `args[0]` or null to `adapter.stop(taskId)`. Null stops every task (`BackgroundFetch.java:147-160`). Then `success()`, whose message is the string `"OK"`: it builds `new PluginResult(Status.OK)` (cordova-android `framework/src/org/apache/cordova/CallbackContext.java:121-123`), which takes `StatusMessages[1]` (`PluginResult.java:36-38, :174`) | `:95-108`: no argument: `removeListener`, then `stop:nil`, every task (`TSBackgroundFetch.m:355-366`). A taskId: that task alone. Then OK with a nil message, which reaches JavaScript as null (cordova-ios `CordovaLib/Classes/Public/CDVPluginResult.m:114-117, :183`) |
| status | `:41-43`: `success(adapter.status())`, always 2 (`BackgroundFetch.java:215-217`) | `:148-154`: OK with the status as an int, from the status block |
| scheduleTask | `:91-95`: `success()`, the string `"OK"` as for stop. But `execute()` never sets `result = true` for it (`:47-49`), so it returns false (`:50`) | `:110-134`: OK with a null message, or ERROR with the error's `localizedDescription` |

- **Android's `execute()` answers scheduleTask twice.** cordova-android sends `INVALID_ACTION` on the same callbackId when `execute()` returns false (`framework/src/org/apache/cordova/PluginManager.java:148, :154-157`). The plugin's `success()` went first, so `CallbackContext` drops the second answer with a warning (`CallbackContext.java:55`). MEASURED: `logcat.txt:369-370`. Only that order keeps "Invalid action" (`PluginResult.java:180`) from reaching the failure callback.
- **Why start lands late on iOS (INFERRED).** cordova-ios calls each command's method on the main thread and sends nothing after it returns (`CordovaLib/Classes/Public/CDVCommandQueue.m:186-193`). start's work is queued behind it on the main queue (`TSBackgroundFetch.m:246`). The commands after it may run first or later: the queue runs them in one go, yielding after 8 ms (`CDVCommandQueue.m:32, :155-156`), and each JavaScript batch arrives on its own. Both orders are MEASURED above.

**PROVEN: the contract the lab scores.** In `types/background-fetch/src/BackgroundFetch.ts`:
- `start(): Promise<BackgroundFetchStatus>` (`:183`);
- `stop(taskId?: string): Promise<boolean>` (`:203`): "If a `taskId` is provided, only that specific task is cancelled" (`:188`);
- `scheduleTask(config): Promise<boolean>` (`:167`);
- `status(callback?): Promise<BackgroundFetchStatus>` (`:247`);
- `finish(taskId): void` (`:224`).

The lab's vocabulary says the same (`labs/harness/vocabulary.yaml:40-46`). What the other two JavaScript bridges do:
- **React Native.** start resolves the status after its native start (`RNBackgroundFetchModule.java:61-66`; `RNBackgroundFetch.mm:65-84`, inside the same status block). stop and scheduleTask resolve `true` (`:68-74`, `:54-59`; `.mm:86-97`, `:99-123`). status resolves the status (`:76-80`; `.mm:141-147`). `index.js` passes each promise through (`:61-63`, `:69-75`); its `status(callback)` calls the callback and returns undefined (`:81-87`).
- **Capacitor.** The wrapper resolves `result.status` for start and status (`src/index.ts:299-307`, `:391-399`) and `true` for stop and scheduleTask (`:334-344`, `:257-260`; WO-006). Its iOS start is a promise again (WO-008), resolved inside the status block after the native start (`BackgroundFetchPlugin.swift:69-86`).
- **MEASURED:** both pass api-smoke (sweep `20261113-175459`) and api-smoke-ios (`20261113-175553` FIX, `-175657` FIX-SPM). On iOS their native start precedes the `ret` (`react-native-ios/api-smoke-ios/FIX/run/logcat.txt:27-29`; `capacitor-ios/api-smoke-ios/FIX/run/logcat.txt:29-31`).

**PROVEN: how the lab reads a callback-style call.** `labs/platforms/_shared/labapi.js` calls the member as a customer would (`:397`), records a throw as `threw` (`:398-400`), then awaits what the call returned (`:405`). Awaiting undefined resolves at once, with undefined. So:
- start, status and stop read `type=undefined`;
- a failure that only the failure callback hears reads as resolved (scheduleTask#4 on iOS);
- the next hook runs before an asynchronous native half has run (start on iOS).

The driver calls the published surface, `window.BackgroundFetch` (`labs/platforms/cordova/app/www/index.html:70-71, :343`), never `cordova.exec`.

**Why nothing caught it (PROVEN).**
- **Cordova declares its own class** (`index.d.ts:4-26`), importing only the types' interfaces (`:1`). Its signatures say callbacks and `void`. React Native's `index.d.ts:1-8` re-exports the types' class instead.
- **preflight's consumer uses the callback form** (`preflight.sh:113`). At runtime it checks only the constants and `finish` (`:137-142`).
- **The docs disagree.** The README documents the callback form, with `stopTask` for a single task (`README.md:442-447`). The docs site renders the TypeScript signatures on its Cordova pages (`docforge/scripts/generate_mkdocs.py:37, :301`): `stop(taskId?: string): Promise<boolean>` and `await BackgroundFetch.stop('com.foo.customtask')` (`docforge/db/BackgroundFetch.stop.yaml:21, :41`). Its Cordova examples page calls `BackgroundFetch.stop("com.example.my-custom-task")` (`docforge/src/static/cordova/examples.md:132`). A customer who follows the docs site gets the throw on iOS and the stop-everything on Android.

**Not a regression (PROVEN).** 7.2.x's `www/BackgroundFetch.js` has the same callback `start`, `stop`, `stopTask` and `status` (`git show 09c93d8^:www/BackgroundFetch.js:92-122`). 7.3.0 (`09c93d8`) ported them to `index.ts` unchanged.

**SUSPECTED, and not this order's: configure's failure path.** configure's failure callback is the caller's `onTimeout` (`index.ts:70`). Native never sends configure an error on purpose. But cordova-android answers a `JSONException` thrown in `execute()` with `JSON_EXCEPTION` on configure's callbackId (`PluginManager.java:158-160`), and `buildConfig`'s getters throw it (`CDVBackgroundFetch.java:107-150`). A bad option would then reach the page as a timeout whose taskId is `"JSON error"` (`PluginResult.java:181`), while configure's Promise, settled by the separate status call, still resolves 2 (INFERRED; not run). See Out of scope.

## Proposed fix

**`src/typescript/index.ts`**, rebuilt with `npm run build`. Commit `www/index.js` and `src/typescript/index.d.ts` with it (`RELEASING.md`; preflight step 2).
- **start, stop, status, scheduleTask and stopTask return a Promise, settled by the native reply.**
  - start and status resolve the status native sends.
  - stop, stopTask and scheduleTask resolve `true`. Neither native sends it (Android `"OK"`, iOS null: the table above), so the wrapper supplies it, as Capacitor's has since WO-006 (its D1). It resolves `true` itself and never passes native's value through: a `value ?? true` would resolve `"OK"` on Android.
  - A native failure rejects (D4).
  - Callbacks a caller passes are still called, with the values they get today (D1).
- **stop takes the types' form (D2).** A string first argument is the taskId, sent as `[taskId]`, as stopTask sends it today. A function first argument is the old success callback. Nothing, `undefined` or `null` is `stop()`.
  - Decide on the type of the first argument, never on `arguments.length`. A caller that passes an explicit `undefined` must get `stop()`. The lab's runner records how a length test misreads that call (`labapi.js:392-393`).
- **configure and finish do not change.** finish is `void` in the types (`BackgroundFetch.ts:224`) and green in both api-smoke scenarios.
- **`index.d.ts`** declares the Promises, the status argument of start's and status's success callbacks, stop's two forms (D2) and stopTask's deprecation (D3).
- **status's `callback` becomes optional** (`callback?`) in `index.ts`, and so in `index.d.ts`, as `BackgroundFetch.ts:247` declares. Today it is required (`index.ts:104`, `index.d.ts:25`), so `await BackgroundFetch.status()` would not compile (TS2554; INFERRED, not compiled). D5's `Pick` would not catch it, because TypeScript accepts a required parameter where an optional one is expected (INFERRED); the Test plan's `status()` read does.

**`src/android/CDVBackgroundFetch.java`:** `execute()` returns true for scheduleTask (`:47-49`). This one line removes the second "Invalid action" answer (`logcat.txt:369-370`). Today only its order keeps that answer from rejecting the new Promise.

**No other native change.**
- Both natives already send the status that start and status declare, and the wrapper supplies the `true` (above). Both reply to start after the native start (the table above).
- So a caller who awaits start() has it ordered before a later stop(), as on React Native and Capacitor.
- On iOS a caller who does not await is unordered there too. INFERRED: React Native iOS and Capacitor iOS run start in the same status block.

**`README.md`:** the Methods table (`:441-447`) and the scheduleTask examples (`:99-110`, `:277-281`, `:336-341`) show the Promise form, and document the callback form.

**`scripts/preflight.sh`:** the `(WO-009)` assertions (Test plan; D5).

**Lab text, in fetch-forge on a branch of the same name** (the Test plan says which).

The anchor `// (WO-009)` goes at each change in `index.ts` and at `CDVBackgroundFetch.java:47-49`, and `# (WO-009)` in `preflight.sh`. The CHANGELOG entries go under `## Unreleased`, for example:

> * [Changed] `start()`, `stop()`, `status()`, `scheduleTask()` and `stopTask()` return a `Promise`, as `@transistorsoft/background-fetch-types` declares: `start()` and `status()` resolve the `BackgroundFetchStatus`, the others `true`, and a failure rejects. The success and failure callbacks still work. TypeScript code that types one of their results as `void`, or a test double for one of them that returns nothing (for example `() => {}`), no longer compiles. `stop(taskId)` now stops only that task: until now the taskId was taken for the success callback, so on Android every task was stopped and on iOS the call threw. `stopTask(taskId)` is deprecated in favour of `stop(taskId)`. A call with no callbacks that fails now rejects (iOS: `start()` with Background App Refresh off, `scheduleTask()` when iOS refuses the task), where it failed silently. (WO-009)
> * [Android] `scheduleTask()` no longer answers twice ("Invalid action", logged and dropped). (WO-009)

**Decisions** (each changes a public API customers already call):

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | How the Promise form and the callback form coexist | (a) Every method returns a Promise and still calls the callbacks it is given. When a caller passes any callback, the plugin attaches a no-op handler to that Promise, so a callback-only caller never sees an unhandled rejection. (b) A Promise only when no callback is passed; with callbacks, undefined as today. React Native's `status(callback)` works that way (`index.js:81-87`). (c) Promises only: the callbacks are dropped. | (a). It meets the types for every call they describe and changes nothing for a callback caller. **What each breaks.** (a): a no-callback call that fails now rejects, and if nobody awaits or catches it the WebView logs an unhandled rejection (INFERRED); TypeScript code that typed a result as `void`, and a typed test double of one of the five methods that returns nothing (INFERRED, not compiled; Scope check). (b): the same; the return type depends on the arguments; and `status(callback)` returns undefined where the types and the docs site declare a Promise. (c): every callback caller, whose callbacks are never called; the plugin's examples (`example/app/www/js/index.js:40-56`, `example/ionic/src/app/home/home.page.ts:51-80`), the README, preflight's consumer (`preflight.sh:113`) and the lab driver (`index.html:100, :278`); a major release. |
| D2 | stop's first argument | (a) A string is the taskId (the types' form), a function the old success callback; callbacks may follow either. (b) Keep `stop(success, failure)`, and customers keep using `stopTask(taskId)`. | (a). (b) leaves the form the types and the docs site give Cordova customers throwing on iOS and stopping every task on Android. **What (a) breaks:** an Android app that calls `stop(taskId)` and relies on it stopping everything now stops one task. Today that call logs an uncaught TypeError from cordova.js, but asynchronously, after native has stopped every task (`logcat.txt:374-377`, then `:391`): `androidExec` hands the native reply to `nextTick` (`cordova.js:1000-1002`). It does not interrupt the caller, so an Android app may rely on it without having noticed (INFERRED). |
| D3 | stopTask | (a) Keep it, returning the same Promise as `stop(taskId)`, and mark it `@deprecated` in `index.d.ts` in favour of `stop(taskId)`. (b) Keep it, not deprecated. (c) Remove it. | (a). There is no runtime change, and TypeScript users are pointed at the types' form. (c) breaks everyone the README taught (`README.md:443`) and needs a major release. |
| D4 | What a rejection carries | (a) What native sends, the value today's failure callback receives: for start a bare number (the status, `CDVBackgroundFetch.m:89`, or the start error's code, `:85`); for scheduleTask the error's message (`:131`). configure already rejects a bare number (`index.ts:67`). (b) An `Error` whose message is that value, nearer React Native (`RNBackgroundFetch.mm:77, :81`). | (a). Both forms of a call then fail with one value, and the types declare no rejection value. Under either option, iOS start's two failure paths send ints that can collide: a BGTaskScheduler error code 2 reads like STATUS_AVAILABLE (SUSPECTED, older than this order; Out of scope). |
| D5 | A guard against drift | (a) preflight's consumer assigns Cordova's class to a `Pick` of the types' class over the members both have: the eight constants, configure, scheduleTask, start, stop, finish and status. It also reads each result as the types declare it, compiles the callback forms, and checks what reaches `exec` at runtime. (b) The reads and the runtime checks, without the `Pick`. (c) No guard. | (a). Use a `Pick`, not an `Omit`: the types float on `^1.0.0` (`package.json:52`), and an `Omit` fails a release when the types add a member (WO-006 Log, 2026-11-13). NOT CHECKED, with no compile while writing: whether stop's two signatures and configure's required `onTimeout` (`index.d.ts:19`; optional in the types, `BackgroundFetch.ts:136`) pass the assignment. |
| D6 | Which release | (a) The next release, beside WO-001's and WO-003's lines under `## Unreleased` (`CHANGELOG.md:3-11`), with the `[Changed]` line. (b) Hold it for a major. | (a) under D1 (a): a callback caller's app runs as before, D2's Android `stop(taskId)` aside; a typed test double may need a change (Scope check). D1 (c) would need (b). |

## Scope check

- **Inside the plugin:**
  - The example apps call the callback forms (`example/app/www/js/index.js:40-56`; `example/ionic/src/app/home/home.page.ts:51-80`). They are unchanged under D1 (a) or (b).
  - preflight's consumer (`preflight.sh:113`) still compiles under D1 (a) or (b).
  - `www/exec-browser.js` forwards to `window.cordova.exec` and is not affected (INFERRED).
  - `src/typescript/index.js` is an older ES-module wrapper over `window.BackgroundFetch` (`:1-92`, last changed in 7.3.0). Nothing loads it (`package.json:5, :9`), but the tarball ships it: `.npmignore:6-7` excludes only `index.ts` and `tsconfig.json`. It drops return values (`:72-77`, no `return`). It is noted here so nobody fixes the wrong file; see Out of scope.
- **Native:** the SDK is untouched, and so is the iOS plugin. The Android plugin changes by one line.
- **The lab** (INFERRED until swept):
  - The driver's own calls use callbacks: `finish(taskId, ok, err)` (`index.html:100`) and the tick's `status(r)` (`:278`). They keep working under D1 (a) or (b), not (c).
  - The bridge-neutral anchors pass either way. The `status` anchors assert `outcome: resolved`. scheduleTask asserts `outcome: resolved` on Android (`events-duplicate.yaml:45-47`, `events-task-android.yaml:38-40`), which the Promise does. ios-task-sim's scheduleTask asserts `settled` (`ios-task-sim.yaml:38-40, :70-72`).
  - api-smoke-ios's bgCancels `com.transistorsoft.fetch=1,com.transistorsoft.fetchlab.task=1` does not change when stop#5 reaches native. stop#5 cancels the task, stop#7 the fetch, and stop#7's stop-all then finds no task (`TSBackgroundFetch.m:357-361`), as on React Native.
- **Customers:**
  - **Callback callers:** no runtime change under D1 (a), except D2's Android `stop(taskId)`. A typed test double may stop compiling (below).
  - **Callers who pass no callback:** they get a Promise, and failures now reject (D1).
  - **TypeScript callers:** results change from `void` to a `Promise` (`index.d.ts:20-25`). INFERRED to break, not compiled while writing:
    - code that annotated a result `void`;
    - a test double or stub of start, stop, status, scheduleTask or stopTask that returns nothing: jest `mockImplementation(() => {})`, Jasmine `and.callFake(() => {})`, or `and.returnValue(undefined)` under `strictNullChecks`. Each is a `void`-returning function where a Promise is declared. WO-006 MEASURED the Capacitor analogue: a double resolving `Promise.resolve()` failed with TS2322 (WO-006 Log, 2026-11-13, `WO-006-capacitor-void-returns.md:210`). The Ionic example (`example/ionic/src/app/home/home.page.ts`) shows typed TypeScript callers exist.

    To be compiled with TypeScript 5 on a scratch file, as WO-006 did.
- **Docs:**
  - The README changes in this order.
  - The docs site already shows the types' signatures and `stop(taskId)` for Cordova, and the fix makes them true.
  - The docs site's `await BackgroundFetch.status;` (`examples.md:113`, no call) is wrong under any API: Out of scope.
- **NOT CHECKED:** a Capacitor app that loads this plugin through Capacitor's Cordova layer (`docs/INSTALL_CAPACITOR.md`; `CDVBackgroundFetch.m:13-17` notes that layer).

## Platform parity

- **Android: affected, MEASURED** (sweep `20261115-215127`).
  - The shared JavaScript is `index.ts:75-106`.
  - stop(taskId) cancels every task: `CDVBackgroundFetch.java:81-88` calls `native/background-fetch/android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/BackgroundFetch.java:147-160 (399765c)` with null.
  - Android only: scheduleTask's `execute()` return (`:47-50`).
  - **start's ordering: not affected.** exec does not return until `execute()` has done the native start. PROVEN: `CDVBackgroundFetch.java:74-79` calls `adapter.start(…)` and then `success(…)`; `native/background-fetch/android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/BackgroundFetch.java:128-137, :246-262 (399765c)` registers the task inline; cordova-android's `androidExec` calls the bridge synchronously (`cordova.js:992`), which runs `CordovaBridge.java:59` and then `PluginManager.java:148` inline. MEASURED, and consistent with it: start's native lines (`labs/captures/cordova/api-smoke/FIX/run/logcat.txt:331-349`, sweep `20261115-215127`) precede `api id=start#3` (`:350`) and its `ret` (`:351`).
- **iOS: affected, MEASURED on both channels** (sweeps `-223641`, `-224122`).
  - The same JavaScript. stop(taskId) throws in iOSExec.
  - start's native half runs in an asynchronous status block (`CDVBackgroundFetch.m:77-92`; `native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.m:244-249 (399765c)`).
  - The `execute()` return: N/A, because cordova-ios calls the method's selector and sends nothing after it (`CDVCommandQueue.m:186-193`).
- **One file fixes the JavaScript on both OSes.** Android's line goes in the same commit.
- **The other bridges, for the record; none of them is in scope.**
  - React Native and Capacitor conform, and reply to start after their native start (MEASURED; see the Diagnosis).
  - Flutter's stop is `Future<int>` (WO-011).

## Test plan

- **Red before the fix:** already MEASURED (sweeps `20261115-215127`; `-223641`, `-224122`; also `-183522`, `-220040`, `-220507`).
- **preflight, `(WO-009)`.** Run the assertions at `368eb85` first and record the red; then run them green on the fix.
  - **Compile.** Strict, TypeScript 5 (`preflight.sh:96, :117-122`):
    - `BackgroundFetchStatus` reads of `start()` and `status()`;
    - `boolean` reads of `scheduleTask(task)`, `stop(task.taskId)`, `stop()` and `stopTask(id)`;
    - the callback forms still compile;
    - D5's `Pick`.
  - **Runtime.** Extend the fake `window.cordova.exec` (`:135`) to record each call's action and arguments, and to answer success or failure. A success answers what Android sends: the status for start and status, the string `"OK"` for stop, stopTask and scheduleTask, so that a wrapper passing native's value through fails here and not only on a device. Then check:
    - `stop("x")` sends `["x"]` and resolves `true`;
    - `stop(fn)` sends `[]` and calls `fn`;
    - `stop()` and `stop(undefined)` send `[]`;
    - `stopTask("x")` sends `["x"]` to `stop` and resolves `true`;
    - `scheduleTask(task)` resolves `true` on a success answer, and rejects with the fake's message on a failure answer;
    - `start()` resolves the number the fake answers, and rejects with it on a failure answer;
    - a failing call made with a callback calls that callback and leaves no unhandled rejection (node's `unhandledRejection` event);
    - `status(cb)` calls `cb` and resolves the same number.
  - `npm run build` is clean, and preflight's step 2 passes on the committed output.
- **The lab builds what the `cordova/background-fetch` checkout holds** (`labs/platforms/cordova/lab.yaml:28-32`). build.sh stages a copy and records its HEAD, dirty flag and content hash (`:71`); `run.json`'s `plugin_source` carries the commit and dirty flag. Sweep with the fix branch checked out there.
  - WO-010 changes the same file (`CDVBackgroundFetch.java`: this order at `execute()`, `:47-49`; WO-010 at `onResume` and `onDestroy`, `:152-170`) and `plugin.xml`. Either may land first. So the Log records each sweep's plugin stamp and any other order's code in the checkout during it, as WO-011's Test plan does.
- **Lab, Android:** api-smoke, Cordova, variant `FIX`, with the plugin label at the fix.
  - `apiAssertions ok`: status#2 and start#3 read `type=number value=2`; scheduleTask#4, stop#5 and stop#7 read `type=boolean value=true`. jobsRegistered and jobIds are unchanged.
  - **By hand in `logcat.txt`, since no metric reads it:**
    - stop#5 logs `- stop: com.transistorsoft.fetchlab.smoke` and cancels the smoke job alone;
    - stop#7 cancels job 999;
    - no `callback.success.apply` TypeError;
    - no "second callback" warning.
  - **Guards:** events-duplicate and events-task-android stay green. The Cordova Android board reads 12/13, with restart-foreground-android still red (WO-010), or 13/13 if WO-010's fix is in the swept code.
- **Lab, iOS:** api-smoke-ios and ios-stop-start, Cordova, `FIX` and `FIX-SPM`, all green.
  - bgSubmits read the ordering red in four sweeps of six, and green at least once on each channel (Diagnosis), so one green proves nothing: run three sweeps per channel, each green on bgSubmits `appRefresh=1` and sdkSubscribers `-`.
  - **By hand:** `[TSBackgroundFetch start]` precedes `ret id=start#3`, and `[TSBackgroundFetch stop] com.transistorsoft.fetchlab.task` falls between `ret id=scheduleTask#4` and `app=api id=stop#5 outcome=ok`, as on React Native (`react-native-ios/api-smoke-ios/FIX/run/logcat.txt:34-38`): the driver logs a hook's `api` line only after the call settles (`labapi.js:405-412`).
  - **Not reachable on the Simulator: iOS start's native reject** (`CDVBackgroundFetch.m:89`, Background App Refresh off or restricted; `:85`, an error from the SDK's start). The Simulator's status is always available, and the SDK clears BGTaskScheduler's Unavailable (`TSBackgroundFetch.m:118-122 (399765c)`). That start() then rejects with that number is INFERRED from those lines and preflight's fake exec. A device check with Background App Refresh off is optional, and NOT YET DESIGNED as a lab step (as WO-008).
  - The Cordova iOS board reads 7/7.
- **Lab changes.**
  - **No assertion, `platform_expect` or `absent_on` changes.** The reds go green on the scenarios as they stand.
  - **This order updates the text that records the finding,** as WO-006 updated api-smoke's (its Verification):
    - `api-smoke.yaml:54-63` names WO-009 and its green sweeps. `api-smoke-ios.yaml:43-55` and `ios-stop-start.yaml:35-36` already name WO-009 (WO-002's 2026-11-16 entry), so they add only the green sweeps. Their sweep citations were corrected to the committed captures' in the same entry (`:1349`); api-smoke's `-183522` is the first red;
    - the Cordova sentence at `api-smoke-ios.yaml:38-39` ("On Cordova stop#5 reaches nothing native …"), false once stop#5 reaches native (Scope check). The ordered reading at `:39-40` stays WO-002's (below);
    - `labs/platforms/cordova/lab.yaml:67`, and `:76`'s closing clause ("Cordova iOS reads 5/7 …; api-smoke-ios and ios-stop-start are red on the callback-style finding"), where the fix's target is 7/7;
    - the driver's comment at `index.html:274-276`. Its "empty function" failure callback (`www/index.js status()`) stops being one under D1 (a); its conclusion, that the callback form never settles on a native error, still holds for `BG.status(r)`;
    - the closing clause "Cordova's resolves nothing, callback-style" in 14 YAML comments across ios-registration, ios-fetch-sim, ios-stop-start, ios-timeout-sim, ios-task-sim and termination-calibration.

    All of these are comments or descriptions. WO-006 re-scored after such edits and found no capture `spec-changed` (WO-006 Log, 2026-11-13).
  - **These are WO-002's:**
    - the `outcome: settled …` reason strings in `ios-task-sim.yaml:40, :72`. They are part of the hooks, so editing them marks every bridge's capture spec-changed (INFERRED from WO-002 Log `:1310`);
    - returning the anchors to `returns`;
    - the ordered reading of stop(taskId)'s own cancel (`:1313`).
- **No unit tests.** The plugin has no JavaScript test runner (`package.json:10-15`) and no native tests. preflight carries the order's test anchor.

## Out of scope

- **Sibling work orders drafted alongside this one:**
  - **WO-010** (`WO-010-cordova-android-restart-ordering.md`): Cordova Android fails restart-foreground-android. When the app restarts itself in the foreground (the old Activity's destroy arrives after the new one resumes), the plugin does not act on the current Activity alone (WO-007's rule).
  - **WO-011** (`WO-011-flutter-stop-returns-int.md`): Flutter's `stop()` is `Future<int>` and resolves the status, where the types declare a boolean.
  - **WO-012** (`WO-012-flutter-android-restart-ordering.md`): Flutter Android fails restart-foreground-android. The plugin does not act on the current Activity alone (WO-007's rule), and its EventChannel is held process-wide rather than on the plugin instance.
  - **WO-013** (`WO-013-stop-no-arg-scope.md`): `stop()` with no argument means different things on Android. There React Native and Capacitor stop only their own fetch task, while Cordova and Flutter cancel every task, as the types' doc says. On iOS all four bridges cancel every task. This order keeps Cordova's `stop()` as it is.
- **configure:**
  - the SUSPECTED failure path through `onTimeout`;
  - its rejection with a bare number;
  - its Promise settled by a separate status call;
  - the required `onTimeout` in `index.d.ts:19`.

  These need their own work order, after a lab step feeds configure a bad option.
- **finish** stays `void`, as the types declare.
- **Making `start(); stop();` ordered without an await.**
  - **iOS:** unordered on every bridge. Each runs start inside the SDK's asynchronous status block (PROVEN: `RNBackgroundFetch.mm:65-84`, Capacitor's `BackgroundFetchPlugin.swift:69-86`, Flutter's `ios/background_fetch/Sources/background_fetch/BackgroundFetchPlugin.m:109-129` at `11d53b8`, `CDVBackgroundFetch.m:73-93`; `TSBackgroundFetch.m:244-249 (399765c)`). The race is MEASURED on Cordova alone (Diagnosis) and INFERRED on the others.
  - **Cordova Android:** ordered. exec does not return until the native start is done (PROVEN; the capture is consistent with it: Platform parity).
  - **React Native, Capacitor and Flutter Android:** NOT CHECKED.
- **Android `start()` after `stop()` doing nothing.** That is WO-002 suspected defect 2, in the native SDK.
- **iOS start's colliding failure ints** (D4; SUSPECTED).
- **The stale `src/typescript/index.js`,** and whether it should ship.
- **The docs site's `examples.md:113`** (`await BackgroundFetch.status;`, no call).
- **One rejection shape across bridges.** React Native rejects an Error, Capacitor a string, and Cordova, under D4 (a), a number for configure and start and a string for scheduleTask.

## Not an acceptable shortcut

- **Relaxing api-smoke or api-smoke-ios for Cordova,** by an `absent_on`, a per-platform `returns`, `outcome: resolved` or a `not_applicable`. These scenarios hold every bridge to the types (`api-smoke.yaml:29`).
- **A Cordova `platform_expect` in api-smoke-ios** pinning bgSubmits `appRefresh=2` or sdkSubscribers `cordova-background-fetch`. It would enshrine a race that bgSubmits read green in two sweeps of six, one on each channel, and it would fail at random.
- **Promisifying `BackgroundFetch` in the lab driver.** api-smoke would then test the lab's wrapper, not the surface customers call (`index.html:70-71, :343`).
- **Changing the types** to `void`, callbacks, or `Promise<…> | void`. React Native and Capacitor meet them, and the docs site shows them on Cordova's pages.
- **Promises without stop(taskId).** Android api-smoke would go green while stop#5 still cancels every task: jobIds `-` cannot tell (`api-smoke.yaml:63`), and only the logcat shows it. On iOS stop#5 would still throw.
- **Resolving in JavaScript before the native reply,** for example `Promise.resolve(true)` after `exec`, or start resolving STATUS_AVAILABLE. Nothing would then order start before a later stop on iOS, failures would never reject, and scheduleTask#4 would stay `want-rejected-got-resolved`.
- **Calling `status()` after `start()` to supply start's value.** WO-008 rejected that route: it fabricates the value and still returns before native.
- **Fixing the ordering natively instead,** for example a synchronous iOS start outside the SDK's status block. Every value stays undefined, failures stay silent, and Cordova would diverge from React Native and Capacitor, which reply from that block.
- **Never rejecting, to avoid unhandled rejections.** The other bridges reject, and api-smoke-ios expects scheduleTask to reject on the Simulator. D1 (a)'s handler covers callback callers.
- **Dropping the callbacks in a minor release** (D1 c).
- **Changing what `stop()` with no argument cancels.** That is WO-013's question, and the answer would change every Cordova customer's runtime behaviour.
- **Editing `www/index.js` or `index.d.ts` by hand.** preflight refuses a release whose output differs from a compile (`preflight.sh:49-55`), and an install from git loads `www/index.js` as committed.
- **Ignoring "Invalid action" in JavaScript** instead of fixing `execute()`'s return.

## Verification

- [x] D1–D6 decided
- [x] preflight's `(WO-009)` assertions red at `368eb85` and green on the fix; `npm run build` clean; the committed `www/index.js` and `index.d.ts` pass preflight step 2
- [x] Cordova Android `api-smoke` green on the fix's label: status#2 and start#3 read a number, the other three hooks `true`; the job rows unchanged (sweep id recorded here)
- [x] In that capture's `logcat.txt`: stop#5 cancels only the smoke task; no TypeError; no "second callback" warning
- [x] Cordova Android events-duplicate and events-task-android green; the board 12/13, the one red being WO-010's, or 13/13 if WO-010's fix is in the swept code
- [x] Cordova iOS `api-smoke-ios` and `ios-stop-start` green on FIX and FIX-SPM, three sweeps per channel; the board 7/7 (sweep ids recorded here)
- [x] In the iOS captures: `[TSBackgroundFetch start]` before `ret id=start#3`; `[TSBackgroundFetch stop] com.transistorsoft.fetchlab.task` between `ret id=scheduleTask#4` and `app=api id=stop#5 outcome=ok`
- [x] README Methods table and examples updated; the CHANGELOG lines and every `// (WO-009)` anchor in place
- [x] Lab text updated as the Test plan lists (`api-smoke-ios.yaml:38-39`, `lab.yaml:76` and `index.html:274-276` included), with no assertion changed
- [x] For every lab sweep above, Android and iOS, the Log records the plugin stamp (`run.json`'s `plugin_source`) and any other order's code in the `cordova/background-fetch` checkout
- [x] Platform parity: both OSes fixed through the one JavaScript file, and Android's `execute()` line in the same commit; iOS has no counterpart (`CDVCommandQueue.m:186-193`); React Native and Capacitor untouched

## Log

- 2026-11-16 — **Created (draft)** from WO-002's lab board.
  - **Captures and sweeps:** Cordova Android api-smoke, sweep `20261115-215127` (the committed capture; first red in `-183522`). Cordova iOS api-smoke-ios and ios-stop-start, sweeps `-223641` (FIX) and `-224122` (FIX-SPM), the committed captures; `-220040` and `-220507` read from their sweep records.
  - **Checked by hand while writing:**
    - the Cordova JavaScript, its `.d.ts` and both native halves at `368eb85`;
    - React Native, Capacitor and the types at `8d9ab19`, `3fa169a` and `dc4f5b8`; native at `399765c`;
    - the exec paths of cordova-android 15.0.0 and cordova-ios 8.1.1;
    - the README, docforge's db and its Cordova pages;
    - `labapi.js` and the Cordova driver.
  - **Corrections to WO-002's summary** (the scenario lines are `0c596d2`'s; all four were since recorded in WO-002's 2026-11-16 entry, `:1334`, `:1342`, `:1347`, `:1349`, and the two scenarios' comments corrected):
    - The committed Android capture is `-215127`'s: the iOS slice re-ran Android. It is not `-190943`'s.
    - The bgSubmits and sdkSubscribers reds are a race. FIX-SPM `-224122` reads them red and FIX `-223641` green. By bgSubmits four of six sweeps read red, and each channel read it both ways: `-212810` (FIX, red) and `-213250` (FIX-SPM, green) scored it from native lines although their driver lines never reached the capture.
    - The `.647`/`.648` times survive in no record. That they are `-220040`'s is INFERRED from `api-smoke-ios.yaml:43, :52-53` (`-212810`, also FIX and `appRefresh=2`, is the other candidate), and that capture was overwritten. `api-smoke-ios.yaml:43` and `ios-stop-start.yaml:36` cite `-220040`/`-220507`, where the committed captures are `-223641`/`-224122`.
    - Not in the Log: Android's `execute()` answers scheduleTask twice (`logcat.txt:369-370`).
  - **Not done:** no branch, no code; nothing built, compiled or run. preflight was not run, so whether `www/index.js` is byte-equal to a compile at `368eb85` is INFERRED.
  - **Reviewed (three lenses, three skeptics per finding): 25 findings, 21 upheld.** The substantive changes:
    - WO-013's summary: the split is Android only; on iOS all four bridges stop every task.
    - Android's bare `success()` sends the string `"OK"`, not nothing: the wrapper supplies `true` and never passes native's value through, and preflight's fake exec answers `"OK"`.
    - Platform parity: Android's start ordering is not affected (exec is synchronous), now stated; Out of scope's "unordered on every bridge" narrowed to iOS. The title says "on iOS".
    - The race re-counted by bgSubmits over six sweeps: four red, each channel both ways (`-212810`, `-213250` added).
    - status's `callback` becomes optional.
    - Typed test doubles that return nothing break: Scope check, D1, D6 and the CHANGELOG line.
    - Three more lab passages the fix makes false: `api-smoke-ios.yaml:38-39`, `lab.yaml:76`, `index.html:274-276`.
    - iOS start's native reject recorded as unreachable on the Simulator; stopTask and scheduleTask runtime checks added to preflight's list.
    - D2: Android's `stop(taskId)` TypeError is asynchronous and comes after the stop-everything, so an app may rely on it unaware.
  - **Cross-checked with WO-009..WO-013 (2026-11-16):**
    - Lab counts: Android 12/13 with WO-010's red, or 13/13 if WO-010's fix is in the swept code. A Test plan bullet and a Verification box have the Log record each sweep's plugin stamp and any other order's code in the checkout (WO-010 changes `CDVBackgroundFetch.java` and `plugin.xml` too).
    - WO-002's 2026-11-16 entry now points its open items here and records the corrections: the Verification box for the open items, and the Test plan's sweep-citation correction, removed; the Diagnosis and this Log say where each was corrected.
    - `api-smoke-ios.yaml` gained a line: the Cordova paragraph is `:43-55`; the `.647`/`.648` citations are to `0c596d2`'s text.
    - Lab text: the two iOS passages already name WO-009 and add only the green sweeps; api-smoke's names WO-009 and its green sweeps.
- 2026-11-16 — **Approved by Chris** ("approve all WO 9-12"), with the recommended decisions: D1 (a) a Promise and the callbacks both, with a no-op rejection handler when a callback is passed; D2 (a) a string first argument is the taskId; D3 (a) stopTask kept, returning stop(taskId)'s Promise, `@deprecated`; D4 (a) a rejection carries what native sends; D5 (a) preflight's `Pick` guard, the typed reads and the runtime checks; D6 (a) the next release. Status `in-progress`. Chris then chose to take the four orders one at a time, this one first ("maybe we should work through the WO one-by-one?").
  - **Re-verified before code.** `cordova/background-fetch` is at `368eb85`, its origin/master, as when this order was drafted: the Diagnosis' citations stand.
  - **Branch** `fix/WO-009-cordova-callback-api` in `cordova/background-fetch`, from `368eb85`: the checkout the lab stages. The lab text and these work-order edits are on fetch-forge's branch of the same name, which also records WO-010, WO-011 and WO-012's approvals.
- 2026-11-16 — **Implemented** on `fix/WO-009-cordova-callback-api` in `cordova/background-fetch`, from `368eb85`; uncommitted, for Chris's diff review.
  - **`src/typescript/index.ts`:** one helper, `call()`, makes the native call and returns a Promise settled by its reply. It resolves `asIs` (start, status) or `alwaysTrue` (stop, stopTask, scheduleTask), rejects with what native sends (D4 a), calls the caller's callbacks with what they got before, and gives the Promise a no-op `catch` when a callback is passed (D1 a). `stop` tells its first argument apart by type: a string is the taskId, a function the old success callback, `undefined`/`null` is `stop()` with the callbacks after it, anything else throws, as stopTask's check does (D2 a). stopTask is `@deprecated` (D3 a); status's callback is optional. Rebuilt with `npm run build` (tsc 4.1.6): `www/index.js` and `index.d.ts` are the compile's.
  - **`CDVBackgroundFetch.java`:** `result = true` for scheduleTask, with the `(WO-009)` comment.
  - **preflight `(WO-009)`** (D5 a): the `Pick` over the types' class; typed reads of start, status, scheduleTask, stop(taskId), stop(), stopTask; the callback forms. The fake `cordova.exec` records each call and answers later, as Android does (the status; `"OK"`), or fails; `check.js` asserts the Test plan's list, and that no exec call is left over.
    - **Red at `368eb85`, MEASURED** (the new preflight over the old code, in a scratch worktree): TS2322 on the `Pick` (stop's signature) and on every typed read, TS2554 on `status()`, TS2345 on `stop(taskId)` and on start's status callback, TS2554 on `stop(taskId, success, failure)`. With the compile let through, the runtime checks fail at the first (`stop(taskId) resolves true`: undefined).
    - **Green on the fix:** preflight passes all four steps. D5's NOT CHECKED is now MEASURED: stop's two overloads and configure's required `onTimeout` pass the `Pick`.
  - **The Scope check's TypeScript 5 question, MEASURED** (5.9.3, the packed fix, `node16`, scratch files). These fail with or without `strict`: a `void` annotation on a result (TS2322); a double that returns nothing, `() => {}` (TS2322); a double resolving `Promise.resolve()` (TS2322); `.then((v: void) => …)` (TS2345). A double resolving an explicit `undefined` fails under `strict` only. A bare `await`, a fire-and-forget call and a truthiness test compile. As WO-006 measured for Capacitor.
  - **README** (the Methods table, the three scheduleTask examples) and the two CHANGELOG lines.
  - **Lab, MEASURED,** native `4.1.1-master-399765c`, plugin `368eb85+dirty`: the WO-009 working tree, with no other order's code in the checkout.

    | Sweep | What | Result |
    |---|---|---|
    | `20261116-114446` | Cordova Android, the suite, FIX | 12/13: api-smoke, events-duplicate and events-task-android green; restart-foreground-android red (WO-010) |
    | `20261116-115144` / `-115624` | Cordova iOS, the board, FIX / FIX-SPM | 7/7 each |
    | `-120058`, `-120418` / `-120239`, `-120557` | api-smoke-ios and ios-stop-start, FIX / FIX-SPM | 2/2 each |

    - **By hand, Android api-smoke:** status#2 and start#3 read `type=number value=2`, scheduleTask#4, stop#5 and stop#7 `type=boolean value=true`; stop#5 logs `- stop: com.transistorsoft.fetchlab.smoke` and cancels job `-592898938` alone, stop#7 job 999; no TypeError, no "second callback", no "Invalid action".
    - **By hand, iOS (the captures of `-120418` FIX and `-120557` FIX-SPM, since replaced by the re-sweep below):** `[TSBackgroundFetch start]` before `ret id=start#3`; `[TSBackgroundFetch stop] com.transistorsoft.fetchlab.task` between `ret id=scheduleTask#4` (rejected with the error's message, "BGTaskScheduler is not available on this…": D4 a) and `app=api id=stop#5 outcome=ok`. bgSubmits `appRefresh=1` and sdkSubscribers `-` in all six api-smoke-ios runs (each a PASS).
  - **Re-scored:** 208 captures, 37 not PASS. Exactly this order's seven reds turned green, and no other verdict changed. selftest passes (204 checks); vendor_check passes.
  - **Lab text,** as the Test plan lists: api-smoke.yaml, api-smoke-ios.yaml (the stop#5 sentence and the Cordova paragraph), ios-stop-start.yaml, lab.yaml (the callback-style and the 5/7 notes), index.html's tick comment, and the 14 anchor comments ("Cordova's resolved nothing until WO-009"). No assertion changed. The index.html comment was edited after these sweeps; the re-sweep below built with it.
  - **Reviewed (four lenses, three skeptics per finding): 15 findings, 9 upheld, all applied.**
    - **`stop(null, fn)` and `stop(undefined, fn)`,** the 7.x form with an empty success slot, read `fn` as the success callback, so an app's failure handler ran on every successful stop (MEASURED by the review on a fake exec: `fn("OK")` on Android, `fn(null)` on iOS), against D1 and D6. Now it keeps the 7.x meaning: `fn` is the failure callback, unless a third argument names the failure. `index.d.ts` declares three overloads: `stop(taskId?: string | null)`, `stop(taskId: string, success?, failure?)` and `stop(success?: (() => void) | null, failure?)`. The README's stop row says so.
    - **`call()` ran `exec` inside the Promise's executor,** so a synchronous throw from exec became a rejection, which the no-op handler swallowed for a callback caller. exec now runs outside it, and its throw reaches the caller as before.
    - **check.js:** a call that never settled ended the script quietly before its last check; a `beforeExit` guard now fails it. The fake also answers other statuses (1, 0) and iOS's null. New checks: `stop(taskId, fn)`, `stop(null)`, `stop(null, fn)` on a success and on a failure, `stopTask(taskId, fn)`, scheduleTask on a null answer, and a success-only caller's failure (no unhandled rejection). The compile block adds `stop(null, failure)`. **Six mutants** of `www/index.js` each fail check.js: `stop(null, fn)` read as success, a constant status, native's `"OK"` passed through, a call that never settles, no no-op handler, `stop(taskId)` sending `[]`. preflight passes on the fix.
    - **The record:** this order's own sweeps overwrote the 2026-11-15 captures whose logcats the Diagnosis cites (Citation bases now says so); api-smoke-ios.yaml's stop#5 sentence; the `ios-stop-start.yaml:76` citation pinned to `0c596d2`.
    - **Refuted (6),** among them: the order of stop's overloads for `Parameters<>`-style typings, and the strict-mode stop forms beyond the CHANGELOG's two.
  - **Re-swept on the reviewed code,** native `4.1.1-master-399765c`, plugin `368eb85+dirty` (the WO-009 tree alone). These are the captures to commit.

    | Sweep | What | Result |
    |---|---|---|
    | `20261116-122513` | Cordova Android, the suite, FIX | 12/13: restart-foreground-android red (WO-010) |
    | `20261116-123205` / `-123640` | Cordova iOS, the board, FIX / FIX-SPM | 7/7 each |
    | `-124114`, `-124433` / `-124254`, `-124612` | api-smoke-ios and ios-stop-start, FIX / FIX-SPM | 2/2 each |

    - **By hand, on the final captures** (`-122513`; `-124433` FIX, `-124612` FIX-SPM): the same lines as above, with `cancelTaskRequestWithIdentifier: com.transistorsoft.fetchlab.task` right after stop#5's native stop.
    - **Re-scored:** 208 captures, 37 not PASS, the same verdicts as after the first sweeps. selftest passes.
- 2026-11-16 — **Committed** (Chris: "Commit both"): plugin `f905ec4` on `fix/WO-009-cordova-callback-api` (from `368eb85`), and fetch-forge's branch of the same name (this Log, the lab text, the captures). Not merged; the `cordova/background-fetch` pointer moves when the plugin branch is merged, and the order is `done` then.
- 2026-11-16 — **Merged and done.** Plugin `master` `5e3602d` (the `--no-ff` merge of `f905ec4`), pushed to GitHub (Chris: "Push plugin, then bump"); fetch-forge `master` `3ffd215`, and the `cordova/background-fetch` pointer moved to `5e3602d` in the commit that marks this order `done`. Every Verification box is ticked. Not released: the CHANGELOG lines wait under `## Unreleased` (D6 a).
- 2026-11-17 — **Released** in cordova-plugin-background-fetch 7.4.0 (was 7.3.1), beside WO-001's, WO-003's and WO-010's lines. A minor, as D6 (a) recommended (Chris: "Minors as listed").
  - **The release:** `53016af` (`release.sh prepare`) and `336bbed` (the CHANGELOG's and README's warning for apps that also use cordova-background-geolocation-lt; `@transistorsoft/background-fetch-types` `^1.0.1`) on `chore/release-7.4.0`, merged `--no-ff` into master as `6394125`, tagged `7.4.0` there and pushed (Chris: "Push all three").
  - **Published, MEASURED:** release workflow run `44126056780`, with provenance (sigstore logIndex `3949124793`). npm: `latest` is 7.4.0, gitHead `6394125`.
  - **This order's code ships as merged.** `src/typescript/`, `www/` and `scripts/` are the same at `6394125` as at `5e3602d`, and so is the `(WO-009)` line in `CDVBackgroundFetch.java` (WO-010 changed the file elsewhere). The README adds only the warning. WO-002's 236/236 board (fetch-forge `b6ac6e7`) swept `2b5f956`, which the release changes only in the CHANGELOG, the README and package metadata.
  - **Left for the release, now done:** the two `(WO-009)` CHANGELOG lines moved from `## Unreleased` to `## [7.4.0]` (D6 a). fetch-forge's `cordova/background-fetch` pointer moves to `6394125` with this entry, on `chore/release-2026-11-17`.
