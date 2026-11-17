---
id: WO-013
title: On Android, React Native's and Capacitor's stop() with no argument cancels only their own fetch task, where the types, the docs, their own iOS halves, Flutter and Cordova cancel every task
status: approved
created: 2026-11-16
platforms: [android]
submodules: [react-native/background-fetch, capacitor/background-fetch]
branch: fix/WO-013-stop-no-arg-scope
issues: []
supersedes: []
---

> **Found by** WO-002's lab board. Its Flutter Android entry (2026-11-13) lists it under "Open, for work orders": React Native and Capacitor "map null to their own fetch taskId", and api-smoke cannot tell that from stopping everything. This order re-verified that against the captures and the source. The divergence is **Android only**: on iOS all four bridges stop every task.
>
> **Evidence markers:** **PROVEN** = read in source; **MEASURED** = seen in a lab capture; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at; **SUSPECTED** = a defect inferred from source that no device run reproduced. The defect itself is SUSPECTED: what each bridge hands the SDK is MEASURED, but no capture keeps a second task alive across a no-argument `stop()` on React Native or Capacitor Android.
>
> **Citation bases** (source read on 2026-11-16):
> - **Bridges and types,** at their checked-out commits: React Native `8d9ab19`, Capacitor `3fa169a`, Flutter `11d53b8`, Cordova `368eb85`, types `dc4f5b8`.
> - **Native:** TSBackgroundFetch 4.2.1 (iOS) / tsbackgroundfetch 4.1.1 (Android), both `399765c` (`versioning/TSBackgroundFetch.properties`, `android/versioning/tsbackgroundfetch.properties`; lab label `4.1.1-master-399765c`), read with `git -C native/background-fetch show 399765c:<path>`. The native checkout sits on `0670713`. Paths below shorten `android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/` to `…/tsbackgroundfetch/` and `ios/TSBackgroundFetch/TSBackgroundFetch/` to `ios/…/`.
> - **Captures** (variant `FIX`, plus `FIX-SPM` on iOS; each capture's `run.json` `when` matched to its sweep's claim in `labs/harness/.claims/`):
>   - Android `api-smoke`: React Native and Capacitor `20261113-175459` (Lab1 and Lab2), Flutter `20261113-183048` (Lab2), Cordova `20261115-215127` (Lab2). Android 16.
>   - iOS `api-smoke-ios`, iOS 27.2 Simulators: React Native and Capacitor `20261113-175553` / `-175657`, Flutter `20261114-123454` / `-123933`, Cordova `20261115-223641` / `-224122` (FIX / FIX-SPM).
>   - Line numbers refer to the on-disk `logcat.txt`. Git keeps only `run.json` and `summary.txt` (`labs/.gitignore:6-10` ignore logcat and the other raw files). The tracked `run.json` files match the on-disk captures.

## Diagnosis

**The contract (PROVEN). Every document that states a scope says "all", outright or, in Cordova's README, by implication:**
- The types: "If no `taskId` is provided, *all* background-fetch and scheduled tasks are cancelled" (`types/background-fetch/src/BackgroundFetch.ts:188-189`), with `// Stop all background-fetch events.` over `stop()` (`:196-197`). React Native's typings are this class (`react-native/background-fetch/index.d.ts:1-8`).
- The docs site's API page: "If no `taskId` is provided, **all** background-fetch and scheduled tasks are cancelled" (`docforge/db/BackgroundFetch.stop.yaml:7`). The native Kotlin example reads `stop(null)` as "all" (`:35-36`). The React Native, Capacitor and Flutter READMEs send their API docs there (`README.md:33` in each).
- The docs site's Examples pages, one per bridge: `// Stop ALL tasks (including the default fetch event)` over a no-argument `stop()` (`docforge/src/static/react-native/examples.md:121-122`, `capacitor/examples.md:125-126`, `flutter/examples.md:135-136`, `cordova/examples.md:134-135`; fetch-forge `2cec8ae`, 2026-04-14). Each is in its bridge's nav (`docforge/scripts/generate_mkdocs.py:445`) and is copied as written (`:541-554`), not generated from `BackgroundFetch.stop.yaml`. The React Native, Capacitor and Flutter READMEs link them (`README.md:34` in each). That the live site serves them is INFERRED (not checked: no network).
- Capacitor's own JSDoc: `// Stop everything.` over `BackgroundFetch.stop()` (`capacitor/background-fetch/src/index.ts:313-314`).
- Flutter's Dart doc: "If provided no `taskId`, will stop all tasks" (`flutter/background_fetch/lib/background_fetch.dart:494`, and `// Or stop ALL tasks` at `:520-521`).
- React Native's README, until 4.4.0 (historical): from 3.0.1 its `stop` row read "Stop the background-fetch API and all `#scheduleTask` from firing events. … If you provide an optional `taskId`, only that `#scheduleTask` will be stopped" (`3.0.1:README.md:283`, unchanged at `4.2.9:README.md:395`). Its history is below.
- Cordova's README, by implication: `stop` will "Stop the background-fetch API from firing fetch events. Your `callbackFn` provided to `#configure` will no longer be executed" (`cordova/background-fetch/README.md:446`), and a `scheduleTask` task "will be executed in the same `Callback` function provided to `#configure`" (`:442`). So no task reaches the app after `stop()`. `stopTask` stops one task (`:443`). This README does not link the docs site (grep), but the site's Cordova Examples page says "ALL" (above).

No document says the no-argument call stops the fetch task alone.

**The native SDK (PROVEN, at `399765c`).**
- **Android, `BackgroundFetch.stop(String)`** (`…/tsbackgroundfetch/BackgroundFetch.java:140-175`):
  - `null` (`:147-160`): clears the fetch callback (`:148`). For every config in the process, it finishes the running BGTask, cancels the job and destroys the persisted config (`:150-157`). Then `BGTask.clear()` and `mConfig.clear()` (`:158-159`).
  - **What `null` reaches (PROVEN in source; NOT MEASURED).** The loop walks `mConfig`, an in-memory map (`:74`). It holds what this process's `configure()` and `scheduleTask()` put there (`:83-101`, `:177-192`), what `onBoot` reloaded (`:103-124`: configs with `startOnBoot` and not `stopOnTerminate`; it destroys the rest, `:106-110`), and what `onFetch` reloaded into an empty map (`:226-241`). `onBoot` runs from the BootReceiver on BOOT_COMPLETED or MY_PACKAGE_REPLACED (`BootReceiver.java:15-22`; `android/tsbackgroundfetch/src/main/AndroidManifest.xml:12-13`), and on Android 16 BOOT_COMPLETED also arrives at a launch after a force-stop (WO-002's Log, `WO-002-background-fetch-labkit.md:381`). A task an earlier process scheduled, and that none of these reloaded, keeps its job and its persisted config (INFERRED). iOS differs (next bullet).
  - A taskId (`:161-174`): finishes that BGTask, cancels that job and destroys that config. The callback is kept.
  - It logs `- stop` for null and `- stop: <taskId>` otherwise (`:141-145`). So the SDK's own line shows which argument arrived.
  - Every task, the fetch and custom ones, is delivered through that one callback (`doFetch`, `:272-275`; `BGTask.onTimeout`, `BGTask.java:207-211`).
- **iOS, `-stop:`** (`ios/…/TSBackgroundFetch.m:355-366`; declared `nullable`, `TSBackgroundFetch.h:45`):
  - `nil` stops every `TSBGTask` (`:357-361`). Each stop destroys the task and cancels its BGTaskScheduler request (`TSBGTask.m:192-201`). That list is every persisted task: `+load` reads them all at launch (`TSBGTask.m:49-73`), so `nil` also reaches tasks an earlier launch scheduled.
  - A taskId stops that task only. An id that is not a `TSBGTask` finds nothing (`:363-364`; `TSBGTask.m:75-86`).
  - The fetch is not a `TSBGTask`. It is cancelled by `-removeListener:` once no subscriber is left (`TSBackgroundFetch.m:324-334` → `cancelBGAppRefresh`, `:141-153`). So on iOS "stop everything" is the bridge's `removeListener` plus `stop:nil`.
- Native's `null`-means-all on Android dates from `16030b7` (2020-02-06), the commit that introduced custom tasks (PROVEN, `git log -S "BGTask.clear()"`).

**Each bridge's no-argument `stop()` (PROVEN in source; what the SDK received MEASURED).** Logcat lines are from each host's `labs/captures/<host>/api-smoke/FIX/run/logcat.txt` on Android, and `…/api-smoke-ios/<variant>/run/logcat.txt` on iOS.

| Bridge | Android | iOS |
|---|---|---|
| React Native | JS passes `taskId \|\| null` (`index.js:73-75`). The module maps null to its fetch taskId: `if (taskId == null) taskId = FETCH_TASK_ID;` (`RNBackgroundFetchModule.java:70`, constant `:18`). MEASURED: `- stop: react-native-background-fetch`, then `- cancel taskId=react-native-background-fetch, jobId=999` (logcat `:651-652`). | Removes its listener, then `stop:nil` (`ios/RNBackgroundFetch/RNBackgroundFetch.mm:92-95`). MEASURED: the fetch's cancel, then `[TSBackgroundFetch stop] (null)` (FIX `:43-44`, FIX-SPM `:43-44`). |
| Capacitor | JS sends `{taskId: undefined}` (`src/index.ts:336`). The plugin maps null to its fetch taskId (`BackgroundFetchPlugin.java:114-115`, constant `:27`). MEASURED: `- stop: capacitor-background-fetch` (`:769-770`). | Removes its listener, then `stop(nil)` (`BackgroundFetchPlugin.swift:89-94`). MEASURED: `[TSBackgroundFetch stop] (null)` (FIX `:45-46`, FIX-SPM `:44-45`). |
| Flutter | Dart passes null (`background_fetch.dart:524-525`). The module hands it on unmapped (`BackgroundFetchModule.java:125-126`, `:166-170`). MEASURED: bare `- stop` (`:417`). | Removes its listener, then `stop:nil`. The source comment says it stops "all custom-tasks" (`BackgroundFetchPlugin.m:133-138`). MEASURED: `stop] (null)` (FIX `:44-45`, FIX-SPM `:44-45`). |
| Cordova | `stop()` sends `[]` (`src/typescript/index.ts:79-81`). The plugin passes null (`CDVBackgroundFetch.java:81-89`). MEASURED: bare `- stop` (`:383-384`). | `[]` removes the listener, then `stop:nil` (`CDVBackgroundFetch.m:99-104`). MEASURED (below). |

So React Native and Capacitor diverge on Android only. On iOS they already do what the types say. Each JS call means two different things on that plugin's two OSes.

**What the captures can and cannot show.**
- **MEASURED: what reached the SDK.** The table's log lines show the argument on every cell.
- **MEASURED, by accident of Cordova's callback API: native `stop(null)` cancels a custom task kept to that point.**
  - **Android:** Cordova's `stop#5` is the types' `stop(taskId)`, but Cordova takes the id as the success callback and sends `[]` (WO-009's finding). So the SDK got `stop(null)` with the smoke task alive: `[CDVBackgroundFetch] stop()`, `- stop`, `- cancel taskId=cordova-background-fetch, jobId=999`, `- cancel taskId=com.transistorsoft.fetchlab.smoke, jobId=-592898938` (`cordova/api-smoke/FIX/run/logcat.txt:373-376`). One call cancelled both jobs.
  - **iOS:** Cordova's `stop#5` reached nothing native. So `stop#7`, the no-argument call, found the custom task still listed and removed it: the fetch's cancel, `[TSBackgroundFetch stop] (null)`, `[TSBGTask destroy] com.transistorsoft.fetchlab.task`, then its cancel (`cordova-ios/api-smoke-ios/FIX/run/logcat.txt:40-43`; FIX-SPM `:39-42`).
- **NOT MEASURED: React Native or Capacitor Android with a second task alive.** In api-smoke, `stop#5` stops the smoke task before `stop#7` (`api-smoke.yaml:48-53`), so no second task is alive at `stop#7`. The SDK's own lines show each stop's cancels in order: React Native `- stop: com.transistorsoft.fetchlab.smoke` and its cancel (logcat `:644-645`), then the fetch's (`:651-652`); Capacitor `:754-755`, then `:769-770`.
  - Its metrics cannot add the missing case. api-smoke scores jobIds, read at the step's end (`api-smoke.yaml:114-118`; `labs/harness/fetch_pack.py:1006-1020`), and no jobsCancelled. jobsCancelled, where a scenario scores it, is the set of `- cancel` jobIds over the whole window (`fetch_pack.py:1123-1128`). Neither kind can say which of two stops in one step made a cancel.
  - On iOS, bgCancels is a tally over the window (`fetch_pack.py:982-990`) and sdkTasks the last tick (`:915-919`); `api-smoke-ios.yaml:37-40` says neither tells which stop acted.

**What happens on React Native and Capacitor Android (SUSPECTED; INFERRED from source and the measured argument).**
- `stop()` reaches the SDK's taskId branch with the fetch's id (`BackgroundFetch.java:161-174`). The fetch job (999) is cancelled. Every task from `scheduleTask()` keeps its job, its config and its persisted copy.
- The fetch callback is kept, so in the foreground each such task still fires into `configure()`'s callback after the app called `stop()` (`:272-275`).
- When the app is headless the callback plays no part. A task scheduled with `stopOnTerminate: false` and `enableHeadless: true` fires the headless task (`:279-285`; both bridges set the job service only for `enableHeadless`, `RNBackgroundFetchModule.java:154-156`, `BackgroundFetchPlugin.java:170-172`). Any other stops itself when it fires (`:276-278`; `stopOnTerminate` defaults to true, `BackgroundFetchConfig.java:48`).
- A periodic task keeps firing in that process. In a later process whose map does not hold it (one that called `configure()` but neither scheduled it again nor reloaded it through `onBoot`), its first fire finds no config and cancels its job (`:264-270`). In a fresh headless process, whose map is empty, it is reloaded (`:226-241`) and handled as above.
- One scheduled with `startOnBoot: true`, `stopOnTerminate: false` and `enableHeadless: true` comes back after a reboot (`BGTask.java:147`; `BackgroundFetch.java:106-113`). Without `enableHeadless`, the SDK turns `startOnBoot` off and `stopOnTerminate` back on (`BackgroundFetchConfig.java:199-207`).

**Not a regression; each bridge's own documents say "all", Cordova's by implication (PROVEN, `git log -S` / `git blame`).**
- **React Native:** the mapping and the iOS `removeListener` + `stop:nil` both arrived in `75f59a1` (2020-02-10), the commit that added `scheduleTask`. Its CHANGELOG called that 2.8.0, which was renumbered 3.0.0 before release (`CHANGELOG.md:301-304`; `ff1b839`), so 3.0.0 (`d4026c4`, untagged) is the first release with the mapping. What the README said of `stop()`:
  - 3.0.0: "Stop the background-fetch API from firing fetch events. Your `callbackFn` provided to `#configure` will no longer be executed", while a `scheduleTask` task "will be executed in the same `Callback` function provided to `#configure`" (`d4026c4:README.md:274`, `:270`). That implies "all". The first sentence goes back to `aa79322` (2016), before custom tasks.
  - From 3.0.1 (`35086e0`, 2020-02-20) until `213968b` (2026-04-14): "all `#scheduleTask`" outright (the contract above; `3.0.1:README.md:283`, `4.2.9:README.md:395`). `213968b` replaced the API table with links to the docs site, and shipped in 4.4.0.
  - The typings said only "Stop subscribing to fetch events" until 4.4.0 (`cd4fc41^:index.d.ts:162-165`). 4.4.0 (`cd4fc41`, 2026-04-14) re-exports the types (`cd4fc41:index.d.ts:1-8`), whose "*all*" sentence first appeared in types `9c296a8` (2026-04-12). Its README links the docs site (first in React Native `213968b`; today `README.md:33-34`), whose "all" came with fetch-forge `c94ac6c` (2026-04-12, `BackgroundFetch.stop.yaml`) and `2cec8ae` (2026-04-14, the Examples pages).
  - So since 3.0.1 React Native's README has said "all" while its Android module handed the SDK the fetch's taskId (`3.0.1:android/src/main/java/com/transistorsoft/rnbackgroundfetch/RNBackgroundFetchModule.java:67`). What each release's native library then did with that id is NOT CHECKED; at `399765c` it stops the fetch alone.
- **Capacitor:** the mapping dates from `53aa1c5` (2021-06-01), the plugin's first working commit. Its iOS side has passed nil since that commit (`53aa1c5:ios/Plugin/Plugin.m:59-67`). Its "Stop everything" JSDoc arrived a day later (`c29e3c5`), before the first beta, 0.0.3 (`CHANGELOG.md`, 2021-06-02). So Capacitor Android has contradicted its own documentation since its first release.
- **Flutter** has passed null since `65c3742` (2020-02-03), with "stop all tasks" in the same commit. **Cordova** has done so since `bec800a` (2020-02-19).
- No commit message or CHANGELOG entry gives a reason for the mapping. No CHANGELOG in the four bridges mentions the scope of `stop()` (grep).

**Who may rely on it (INFERRED; how many is NOT CHECKED).** The public issue trackers were not searched (no network).
- React Native Android apps since 2020 and Capacitor Android apps since 2021 that call `stop()` and expect their `scheduleTask()` tasks to keep running. The same code on iOS already loses those tasks.
- Apps that call `stop()` and later `scheduleTask()` without a new `configure()`. On these two bridges the task still reaches the app, because the callback survives. Both bridges' example apps can do this: a toggle calls `stop()` (React Native `example/expo/App.tsx:83`; Capacitor `example/src/app/home/home.page.ts:86`), and a button calls `scheduleTask()` (React Native `onClickScheduleTask`, `App.tsx:105-117`, button `:160`; Capacitor `onClickScheduleTask`, `home.page.ts:92`, `:122-127`).

## Proposed fix

**Under D1 (a), the recommendation: hand null to the SDK on Android, as the two bridges' iOS halves, Flutter and Cordova already do.**
- `react-native/background-fetch/android/src/main/java/com/transistorsoft/rnbackgroundfetch/RNBackgroundFetchModule.java`, `stop(String, Promise)` (`:69-74`): remove the null → `FETCH_TASK_ID` line (`:70`).
- `capacitor/background-fetch/android/src/main/java/com/transistorsoft/bgfetch/capacitor/BackgroundFetchPlugin.java`, `stop(PluginCall)` (`:113-119`): remove `:115`.
- No change to JS, iOS, the native SDK, the types or docforge. Capacitor's JSDoc already says "Stop everything".
- **Anchors:** `// (WO-013)` at each changed method. Branch `fix/WO-013-stop-no-arg-scope` in each submodule. Each plugin's CHANGELOG gets an entry under `## Unreleased`, for example:

  > * [Changed] [Android] `stop()` with no argument now cancels every task the SDK holds in the app's running process, as documented: the fetch task and the tasks scheduled with `scheduleTask()`. Until now it stopped only the fetch task on Android, and scheduled tasks kept running. To stop one task, pass its `taskId`. (WO-013)

  The line does not say "as on iOS". iOS also reaches tasks an earlier launch persisted, which Android's `stop(null)` does not (Diagnosis, the native SDK), and iOS keeps delivering a task scheduled after `stop()` without `configure()`, which Android will not (cost 2 below). Only the scope within one process matches.

**Decisions:**

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | The direction | (a) Align React Native and Capacitor Android to the contract (the change above). (b) Change the contract to "the fetch task only", and change the six cells that stop everything: all four iOS bridges, plus Flutter and Cordova Android. Also change the types, docforge (`BackgroundFetch.stop.yaml` and the four Examples pages), Capacitor's JSDoc and Flutter's doc. (c) Change no code; document that React Native and Capacitor Android stop only the fetch task, including on their Examples pages, which say "Stop ALL tasks" today. Status `wont-fix`. | (a). See below. |
| D2 | Which release carries it | (a) Each plugin's next release, with the `[Changed] [Android]` line above. (b) Hold it for each plugin's next major. | (a). This makes Android's scope what the docs, the types and the same plugin's iOS side already say and do; the callback split it adds is weighed in Why (a). (b) is the conservative choice: the change is at runtime, and it cancels tasks that some apps keep today. |
| D3 | Red before green | (a) WO-002 builds the Test plan's scenarios first and sweeps all four bridges at the current pins, before any plugin change. (b) Build them on the fix branch only. | (a). The defect is SUSPECTED until then. The lab has measured red before it fixed, as WO-006 and WO-007 did. |
| D4 | The cost side of D1 (a) | (a) Add a `rescheduled` step: `stop()`, then `scheduleTask()` again, then fire. Record where the task goes, without asserting it. (b) Leave it unmeasured. | (a). It measures the one pattern D1 (a) changes besides the scope itself (Scope check). |

**What each D1 option breaks:**
- **(a), two lines on Android.**
  - Apps that relied on Android keeping their custom tasks across `stop()` lose the ones the process holds, persisted configs included (`:150-157`). So a task scheduled with `startOnBoot: true`, `stopOnTerminate: false` and `enableHeadless: true` no longer comes back after a reboot.
  - After `stop()`, a task scheduled without a new `configure()` reaches no callback, because `stop(null)` clears it (`:148`, `:272-275`). Clearing it is pinned native behaviour (`testStopAllNullsCallback`, `…/test/…/TSBackgroundFetchTest.java:479-499`). Nothing then finishes that task's job until JobScheduler's own timeout. INFERRED. Flutter and Cordova Android behave this way today (INFERRED, NOT MEASURED).
  - iOS does not. Each bridge's `scheduleTask` gives the `TSBGTask` its own callback (React Native `RNBackgroundFetch.mm:111-117`, `:168-175`; Capacitor `BackgroundFetchPlugin.swift:107-115`; Flutter `BackgroundFetchPlugin.m:164-170`; Cordova `CDVBackgroundFetch.m:120-126`), the SDK keeps it on the task (`TSBackgroundFetch.m:266-300`), and neither React Native's nor Capacitor's JS `stop()` removes the JS event subscription that `configure()` made (`index.js:73-75`; `src/index.ts:334-344`); the native `removeListener` drops only the SDK's fetch subscriber. **So the fix gives these two plugins a new per-OS split:** on this pattern Android drops the task and iOS delivers it, as on Flutter and Cordova today (INFERRED; D4 (a) would measure it).
  - A `stop()` called while a custom task runs now finishes that task's BGTask too (`:150-154`). INFERRED.
  - Like Flutter's and Cordova's today, the call reaches only the tasks the process holds, not one an earlier process scheduled and this one has not reloaded (Diagnosis, the native SDK). INFERRED.
- **(b), the opposite direction.**
  - It changes six cells across four submodules, plus the types and docforge (`BackgroundFetch.stop.yaml` and the four Examples pages).
  - It breaks every iOS app, and every Flutter and Cordova Android app, that uses `stop()` to cancel its tasks.
  - It leaves no single call that cancels every task the SDK holds, so an app would have to track every id itself. Tasks persist across launches (iOS `+load`, `TSBGTask.m:49-73`; Android `onBoot` and the empty-map reload in `onFetch`, `BackgroundFetch.java:103-124`, `:226-241`), and on Android `stop(taskId)` reaches only a config the process holds (`:166-173`).
  - It contradicts the native API's own `null` = all (docforge `:35-36`).
- **(c), document the divergence.** Nothing breaks at runtime. The same JS call keeps two meanings on one plugin. The new scenario would then carry a dated per-platform expectation for React Native and Capacitor Android, as your decision.

**Why (a):**
- Every document that states a scope says "all" (the contract above: the types, the docs site's API page and its four Examples pages, Capacitor's JSDoc, Flutter's Dart doc, React Native's README from 3.0.1 until 4.4.0, and Cordova's README by implication). None states "fetch only".
- Six of the eight bridge×OS cells already do it, React Native's and Capacitor's iOS halves among them.
- The change is one line per bridge, on Android only, with no native release.
- **The cost has two sides.** The scope cost removes behaviour iOS never had: there a no-argument `stop()` already cancels custom tasks. The callback cost (cost 2 under (a)) removes behaviour iOS keeps. So the fix trades the scope split on React Native and Capacitor for the narrower split Flutter and Cordova already have. That trade is yours, in D1 and D2.

## Scope check

- **Who calls the changed methods:**
  - React Native: only `index.js:74` (`git grep`).
  - Capacitor: only `src/index.ts:336`.
  - The `stop(taskId)` path is untouched. Only the null branch changes.
  - The SDK's own `stop(taskId)` calls (stopOnTerminate's lazy stop, `BackgroundFetch.java:278`; the no-job-service branch, `:290`) are native and unaffected.
- **`start()` after `stop()` on Android is broken either way.** `stop(taskId)` destroys the fetch config (`:166-173`), and `stop(null)` clears all configs (`:159`). In both cases `start()` finds no config (`:249-251`). This is WO-002's suspected defect 2, unchanged by this order (INFERRED).
- **The example apps** call `stop()` from a toggle: React Native `example/expo/App.tsx:83`, Capacitor `home.page.ts:86`. Under D1 (a), on Android, the toggle cancels scheduled tasks. Both also schedule a task from a button: React Native's (`App.tsx:105-117`, button `:160`; `forceAlarmManager: true`, no `enableHeadless`) and Capacitor's (`home.page.ts:92`, `:122-127`; `stopOnTerminate: false`, `enableHeadless: true`). On either, a task scheduled while the toggle is off no longer reaches the page's `configure()` callback on Android (INFERRED, D4). Capacitor's still reaches its headless task once the app is terminated, which needs no callback (`BackgroundFetch.java:279-285`; INFERRED).
- **The lab:**
  - api-smoke is the only Android scenario that calls `stop()` (`api-smoke.yaml:106`, `:112`; grep).
  - On the fix, React Native's and Capacitor's `stop#7` would log a bare `- stop` and cancel job 999 alone, because `stop#5` already stopped the smoke task. Its expectations (`jobIds -`, `jobsRegistered`) are unchanged (INFERRED).
  - Its description at `:22-24` ("the native module reads it as its own fetch taskId") becomes wrong and must be corrected (Verification).
- **Other users of the Android SDK in the same process:** `stop(null)` cancels every config the process-wide singleton holds (`:150-157`), including any another library scheduled. NOT CHECKED, beyond WO-007's grep of bg-forge `addc50501`, which covered `capacitor/background-geolocation/android` and `native/` and found no `tsbackgroundfetch` reference there "apart from a demo app" (`WO-007-capacitor-android-destroy-not-headless.md:127`). bg-forge's other bridges' Android sources are NOT CHECKED.
- **Unchanged:** both iOS halves, the JS, the TypeScript declarations, the native library, Flutter and Cordova.

## Platform parity

- **Android, React Native and Capacitor:** affected. PROVEN (`RNBackgroundFetchModule.java:70`, `BackgroundFetchPlugin.java:115`). The argument is MEASURED (`- stop: <fetch taskId>`); the scope with a second task alive is NOT MEASURED.
- **Android, Flutter:** not affected. It passes null (`BackgroundFetchModule.java:166-170`). The argument is MEASURED (bare `- stop`, `flutter/api-smoke/FIX/run/logcat.txt:417`). That it cancels a live custom task is INFERRED from the same native call, MEASURED on Cordova's `stop#5`: in Flutter's api-smoke, `stop#5` had already cancelled the smoke task (`:410-411`), so `stop#7` cancelled job 999 alone (`:417-418`).
- **Android, Cordova:** not affected. It passes null (`CDVBackgroundFetch.java:81-89`). MEASURED: the native `stop(null)` cancelled a live custom task (`logcat.txt:374-376`).
- **Cross-launch reach, Android against iOS, under any D1 option (PROVEN in source; NOT MEASURED).** Android's `stop(null)` walks `mConfig` (`BackgroundFetch.java:74`, `:149-159`), which holds what this process configured, scheduled or reloaded (`:103-124`, `:226-241`). iOS's `stop:nil` walks every persisted `TSBGTask` (`TSBGTask.m:49-73`; `TSBackgroundFetch.m:357-361`). So a task an earlier process scheduled, and this one has not reloaded, survives a no-argument `stop()` on Android only (INFERRED). That holds for Flutter and Cordova Android today, and for React Native and Capacitor Android after the fix. Out of scope here (below).
- **iOS, all four bridges:** not affected. Each removes its listener and calls `stop:nil` (React Native `RNBackgroundFetch.mm:92-95`; Capacitor `BackgroundFetchPlugin.swift:91-94`; Flutter `BackgroundFetchPlugin.m:133-138`; Cordova `CDVBackgroundFetch.m:99-104`). `stop] (null)` is MEASURED on all four, on both channels. The removal of a kept task is MEASURED on Cordova only, and INFERRED for the other three, which make the same SDK call.
  - **Simulator only:** the custom task was never submitted (taskSubmitErrors). What a device does with a real request is NOT CHECKED (the manual tier).
- **Apps that use TSBackgroundFetch directly** pass their own argument. NOT CHECKED.

## Test plan

**Lab, built by WO-002 (D3). The scenarios are the lab's; their descriptions name WO-013.** `stop-scope` is the Tier 2 scenario WO-002 already plans under that name (`WO-002-background-fetch-labkit.md:243`, where it is named only); this order defines it, and adds its iOS twin `stop-scope-ios`. They use only idioms the lab already has:
- `job:` with `expect: absent`: `events-task-android.yaml:48-49`.
- `fire: task` and taskFires `missing`: `ios-task-sim.yaml:48-53`.
- a configure declaration with `min: 0, max: 0`: `headless-activity-destroyed.yaml:49-50`.
- hook indices count every hook in the step (`labs/harness/labkit.py:1337`), and a step without its own hooks takes the scenario's (`:2873`).

Each step makes exactly one `stop` call, so its whole-step tallies (jobsCancelled, bgCancels) and its end-of-step reads (jobIds, sdkTasks) belong to that call.

- **`stop-scope` (Android; all four bridges).** Hooks: `status` (`outcome: resolved`), `scheduleTask {taskId: com.transistorsoft.fetchlab.task, delay: 600000, periodic: false}` (`outcome: resolved`), `stop` with no args (`outcome: resolved`), so `status#0`, `scheduleTask#1`, `stop#2`. What `stop` returns is api-smoke's to score (WO-011, WO-009).
  - **Step `held`, no fire:** jobsRegistered `-1681675378,999`; jobsCancelled `-1681675378,999`; jobIds `-`.
  - **Step `fired`:** `{after: {api: "stop#2"}, delay_s: 3, job: com.transistorsoft.fetchlab.task, expect: absent}`; driverEvents `-`; fetchTally `-`. This step carries the configure declaration: `subscribe: configure` with events-task-android's args (`:42-43`), `min: 0, max: 0`.
  - **PREDICTED, at the current pins:**
    - React Native and Capacitor red: jobsCancelled `999`, jobIds `-1681675378`. The fire reads `missed(present)` and delivers: driverTally `fetch:com.transistorsoft.fetchlab.task=1`, through the retained callback.
    - Flutter and Cordova green.
- **`stop-scope-ios` (Simulator; all four bridges; FIX and FIX-SPM).** Hooks: `status` (`outcome: resolved`), `scheduleTask` as ios-task-sim's dated `outcome: settled` anchor (`:38-40`), then `stop` with no args (`outcome: resolved`). `outcome: resolved` on `status` and `stop`, as the lab's iOS anchors do (`ios-stop-start.yaml:48-51`), lets Flutter's int and Cordova's callback-style return pass here. A `returns: boolean` on `stop` reads them red, as `ios-stop-start`'s `stop#1` does (MEASURED: `flutter-ios/ios-stop-start/FIX/stopped/summary.txt:9`, `want-boolean-got-number`, sweep `20261115-195448`; `cordova-ios/…/summary.txt:9`, `want-boolean-got-undefined`, sweep `20261115-223641`). Action: `{after: {api: "stop#2"}, delay_s: 3, fire: task, taskId: com.transistorsoft.fetchlab.task}`. The step carries the configure declaration with ios-task-sim's args (`:42-43`), `min: 0, max: 0`.
  - Expect: taskSubmitErrors `BGTaskSchedulerErrorDomain:1=1`; bgCancels `com.transistorsoft.fetch=1,com.transistorsoft.fetchlab.task=1`; taskFires `task:missing=1`; sdkTasks `-`; sdkSubscribers `-`; driverEvents `-`.
  - PREDICTED green on all four. Under D1 (a) it is the guard that iOS stays as it is.
- **D4 `rescheduled` step, in both scenarios, recorded only.** Hooks: `status#0` (`outcome: resolved`), `scheduleTask#1`, `stop#2` (no args, `outcome: resolved`), `scheduleTask#3` (the same args). On Android both `scheduleTask` hooks are `outcome: resolved`; on iOS both are ios-task-sim's dated `outcome: settled` (`:38-40`), since both reject on the Simulator, and Cordova's callback-style calls return nothing. Action 3 s after `scheduleTask#3`: on Android `{after: {api: "scheduleTask#3"}, delay_s: 3, job: com.transistorsoft.fetchlab.task}` (`events-task-android.yaml:48`), on iOS `{after: {api: "scheduleTask#3"}, delay_s: 3, fire: task, taskId: com.transistorsoft.fetchlab.task}` (`ios-task-sim.yaml:48`). Record driverTally.
  - It must not carry `min: 0, max: 0`, which would assert the absence it only records. How its configure declaration records the delivery is WO-002's choice (labkit's dated `arm_only` sends the count to the `recorded` row, `labkit.py:1502-1512`).
  - It stays a step of these scenarios, separate from WO-002's `task-without-configure` (also named only, at `:243`): it measures the state a `stop()` leaves, and asserts nothing. If WO-002 folds it into that scenario, D4's record-only expectation goes with it.
  - PREDICTED, at the current pins: delivered on React Native and Capacitor Android, not delivered on Flutter and Cordova Android, delivered on iOS.
- **Not seen by `stop-scope`: the cross-launch reach** (Platform parity). Its steps schedule and stop in one process, so the gap stays INFERRED from source, not measured. It belongs to its own work order (Out of scope).
- **On the fix (D1 a):**
  - `stop-scope` green on React Native and Capacitor, on Lab1 and Lab2.
  - The Android suite on both bridges as green as before the fix.
  - `stop-scope-ios` unchanged.
  - React Native's and Capacitor's api-smoke `stop#7` logging a bare `- stop`.
- **No platform_expect relaxes the scope** unless you choose D1 (c).
- **No unit tests.**
  - None of the four plugins has a test of the plugin itself (`git ls-files`; none of the files below names `stop()`). React Native's `test` script is npm's placeholder (`package.json:7`), and it tracks no test file. Capacitor's plugin module has only the templates `android/src/test/…/ExampleUnitTest.java` and `android/src/androidTest/…/ExampleInstrumentedTest.java`, and its example app only template Android and Angular specs. Flutter has only the example's template tests (`example/test/widget_test.dart`, `example/test_driver/main_test.dart`), Cordova only the Ionic example's (`example/ionic/src/test.ts` and two `*.spec.ts`).
  - Native's Robolectric suite (`…/test/…/TSBackgroundFetchTest.java` at `399765c`; WO-002's coverage table lists "stop all vs one", `WO-002-background-fetch-labkit.md:29`) has four stop tests, each on one configured fetch task (PROVEN): `stop(null)` clears the callback (`testStopAllNullsCallback`, `:479-499`) and that config (`testStopAllClearsMConfig`, `:557-579`); `stop(taskId)` keeps the callback (`testStopSingleTaskLeavesCallback`, `:501-521`) and removes that config (`testStopSingleTaskRemovesConfig`, `:581-601`). None schedules a custom task, so none pins that `stop(null)` cancels one; that rests on `BackgroundFetch.java:150-159`. The fix touches no native file, so the suite need not run.
  - Each plugin's `scripts/preflight.sh` must stay green. It checks compile-time shapes only.

## Out of scope

- **WO-009** (`WO-009-cordova-callback-api.md`): Cordova's JS API is callback-style. start, stop, status and scheduleTask return nothing where the types declare Promises. `stop(taskId)` throws on iOS and cancels every task on Android. On iOS, start's native half is unordered against a later stop. This order covers only Cordova's no-argument path, which conforms.
- **WO-010** (`WO-010-cordova-android-restart-ordering.md`): Cordova Android fails `restart-foreground-android`: when the app restarts itself, the old Activity's destroy arrives after the new one resumes, and the plugin goes headless with the new page on screen (WO-007's rule).
- **WO-011** (`WO-011-flutter-stop-returns-int.md`): Flutter's `stop()` is `Future<int>` and resolves the status, where the types declare a boolean.
- **WO-012** (`WO-012-flutter-android-restart-ordering.md`): Flutter Android fails `restart-foreground-android`: the plugin does not act on the current Activity alone, and its EventChannel is held process-wide rather than on the plugin instance.
- **WO-002 suspected defect 2:** Android `start()` after `stop()` does nothing, under either argument.
- **Stopping the fetch alone while keeping custom tasks.** On Android, `stop('<the bridge's fetch taskId>')` does it. On iOS no bridge offers it: a taskId skips `removeListener`, and `stop:` finds no `TSBGTask` by that id (`RNBackgroundFetch.mm:92-95`; `TSBackgroundFetch.m:363-364`). PROVEN in source, not measured. If customers need it, it gets its own work order.
- **`stop(null)` clears the Android callback,** so a later `scheduleTask()` without `configure()` delivers nothing (D4 records it). That is native behaviour, and Flutter and Cordova already have it. iOS delivers such a task (cost 2 under D1 (a)).
- **Android `stop(null)` reaches only the configs the process holds,** not tasks an earlier process persisted and this one has not reloaded, where iOS's `stop:nil` reaches every persisted task (Platform parity). A native question, under any D1 option; if it matters, it gets its own work order.
- **iOS:** `removeListener` cancels the fetch request only when no other subscriber is left (`TSBackgroundFetch.m:331-333`).
- **React Native's `taskId || null`** (`index.js:74`) turns `stop('')` into stop-everything.
- **The return values of `stop()`:** WO-006 (verify) and WO-011.

## Not an acceptable shortcut

- **Relaxing the lab for React Native or Capacitor Android.** That includes a platform_expect such as `jobsCancelled: "999"`, dropping the post-stop `job:`, a `not_applicable`, or reading `outcome: settled` where the scope is asserted. Each one makes the red go away by declaring the divergence correct. Only D1 (c) can do that, as your dated decision, never as a lab fix.
- **Measuring it in api-smoke by removing `stop#5`.** api-smoke asserts `stop(taskId)` too, and holds every bridge to the types (`api-smoke.yaml:25-29`). The scope needs its own scenario, with one `stop` per step.
- **Reading the scope from a step that also calls `stop(taskId)`.** jobIds and sdkTasks are end-of-step reads (`fetch_pack.py:1006-1020`, `:915-919`); jobsCancelled and bgCancels are whole-step tallies (`:1123-1128`, `:982-990`). Neither kind can say which of two stops in one step acted (`api-smoke-ios.yaml:37-40`).
- **Changing the native SDK instead,** for example making `stop(<fetch taskId>)` cancel everything. stopOnTerminate's lazy stop (`BackgroundFetch.java:278`) and the no-job-service branch (`:290`) each call `stop(taskId)` for one task, so one terminated task would cancel all. If such a change also cleared the callback, as `stop(null)` does, native's `testStopSingleTaskLeavesCallback` (`TSBackgroundFetchTest.java:501-521`) would fail too (INFERRED). The mapping is the bridges', and so is the fix.
- **Looping over task ids in JS.** JS does not know the SDK's task list. Tasks persist across launches and can be scheduled headless (on Android, configs an earlier process persisted are reloaded by `onBoot`, `BackgroundFetch.java:103-124`, and by `onFetch` into an empty map, `:226-241`; on iOS by `+load`, `TSBGTask.m:49-73`). And on Android `stop(taskId)` acts only on a config the process holds (`:166-173`).
- **Passing null and then calling `configure()` again to keep the callback.** `stop(null)` empties the map (`BackgroundFetch.java:159`), so `configure()` takes its new-config branch and registers the fetch job again (`BackgroundFetch.java:96-100` → `start()`, `:128-137`), which undoes the stop.
- **Fixing React Native and leaving Capacitor, or the other way round.** Both carry the same one-line mapping, and the contract is shared.
- **Editing the types or docforge to say "fetch task only"** without D1 (b) or (c). It would contradict iOS on the same plugins, plus Flutter, Cordova and native.
- **Claiming device behaviour from the Simulator.** `stop-scope-ios` shows the SDK's list and its cancel call, not BGTaskScheduler dropping a real request.

## Verification

- [x] D1–D4 decided
- [x] WO-002: `stop-scope` (steps `held`, `fired`) and `stop-scope-ios` built; red recorded at the current pins (React Native and Capacitor Android red as predicted; Flutter and Cordova Android green; all four iOS green on FIX and FIX-SPM), with sweep ids
- [x] D4's `rescheduled` step recorded on both OSes (if taken)
- [ ] On the fix: `stop-scope` green on React Native and Capacitor Android, on Lab1 and Lab2
- [ ] On the fix: React Native's and Capacitor's Android suites as green as before; api-smoke's `stop#7` logs a bare `- stop`
- [ ] `stop-scope-ios` still green on all four bridges, on FIX and FIX-SPM (no iOS code changed)
- [ ] `// (WO-013)` at both changed methods; each plugin's CHANGELOG entry under `## Unreleased` carries `(WO-013)`; the scenario descriptions name WO-013; `api-smoke.yaml:22-24` and `:48-53` corrected (prose only)
- [ ] Platform parity: Flutter and Cordova Android and all four iOS bridges unchanged, as cited

## Log

- 2026-11-16 — **Created (draft)** from WO-002's lab board: the Flutter Android entry's open item (2026-11-13). Re-verified against the `api-smoke` captures (React Native and Capacitor `20261113-175459`, Flutter `20261113-183048`, Cordova `20261115-215127`) and the `api-smoke-ios` captures, FIX and FIX-SPM (`20261113-175553` / `-175657`, `20261114-123454` / `-123933`, `20261115-223641` / `-224122`), and against source at the commits in the header.
  - **Corrected from the Log's summary:** the mapping is Android only. On iOS, React Native and Capacitor remove their listener and pass nil, like Flutter and Cordova. PROVEN in source, and MEASURED as `[TSBackgroundFetch stop] (null)`. WO-002's 2026-11-16 entry records the correction.
  - **More is measured than the summary said:**
    - The SDK's own `- stop` line shows the argument on every Android cell.
    - Native `stop(null)` cancelling a live custom task is MEASURED on Cordova, on both OSes, through its callback-style `stop`.
    - Still NOT MEASURED: React Native and Capacitor Android with a second task alive.
  - **Not done:** no branch, no code, no lab change, nothing run.
  - **Reviewed (three lenses, three skeptics per finding): 31 findings, 26 upheld.** All 26 applied after re-checking source and captures, several with the skeptics' corrections; nothing built or run for them. The substantive changes:
    - Docs history: React Native's README said "all `#scheduleTask`" outright from 3.0.1 until 4.4.0, and implied it in 3.0.0. Cordova's README implies "all". The heading no longer says "not documented". 2.8.0 is now 3.0.0, and each hash names its repo.
    - The docs site's four Examples pages ("Stop ALL tasks") added to the contract, Why (a) and D1 (b)/(c).
    - Android `stop(null)` reaches only the configs the process holds; iOS reaches every persisted task. The CHANGELOG line is narrowed and drops "as on iOS"; (b)'s argument is qualified; a Platform parity bullet and an Out of scope item are added; `stop-scope` cannot see it.
    - The fix trades the scope split for a callback split on React Native and Capacitor (Android drops a task scheduled after `stop()` without `configure()`, iOS delivers it). Cost 2 and Why (a) say so.
    - Headless delivery and reboot survival need `stopOnTerminate: false` and `enableHeadless: true`; the retained callback is foreground only.
    - Native's suite has four stop tests, cited for cost 2; none pins the cancel of a custom task.
    - Flutter's parity scope is INFERRED: only Cordova's `stop#5` met a live task.
    - Test plan: `stop-scope` is WO-002's planned Tier 2 scenario (`:243`); full hook lists and outcomes, the per-OS fire, which step carries the `min: 0, max: 0` configure declaration; D4 kept separate from `task-without-configure`.
    - React Native's example app added to the Scope check.
  - **Cross-checked with WO-009..WO-013 (2026-11-16):**
    - Out of scope, WO-010: the self-restart case (the old Activity's destroy after the new one resumes), not a recreation, which is green on Cordova (`cordova/recreate-foreground-android/FIX/{run,stop-on-terminate-false}` PASS).
    - Out of scope, WO-009: start's unordered native half is iOS only, as its title says. Cordova Android's start is ordered (MEASURED: `cordova/api-smoke/FIX/run/logcat.txt:331-332` before `ret id=start#3`, `:351`).
    - `platforms: [android]`: the recommended fix changes only Android files. iOS stays in the Test plan as `stop-scope-ios`, the guard.
    - The Android-only correction is recorded in WO-002's 2026-11-16 entry. No box here points WO-002's open item at this order or corrects sweep citations; the `api-smoke.yaml` prose edit stays, because the fix makes it wrong. The one citation into `api-smoke-ios.yaml` (`:37-40`) is above the added line and still holds.
- 2026-11-16 — **Approved by Chris** ("Approve as recommended"): D1 (a) align React Native and Capacitor Android to the contract (pass null); D2 (a) each plugin's next release, with the `[Changed] [Android]` line; D3 (a) WO-002 builds `stop-scope` and `stop-scope-ios` and sweeps all four bridges at the current pins first; D4 (a) the record-only `rescheduled` step. Status `approved`.
  - **Re-checked first, after WO-009..WO-012 merged** (Cordova `2b5f956`, Flutter `5a01d8d`; React Native `8d9ab19` and Capacitor `3fa169a` unchanged): the mapping is still `RNBackgroundFetchModule.java:70` and `BackgroundFetchPlugin.java:115`; Cordova's no-argument `stop()` still sends `[]` (now `src/typescript/index.ts:127-131`, native `CDVBackgroundFetch.java:87-95`), and Flutter's still passes null (`lib/background_fetch.dart:531`, `BackgroundFetchModule.java:214-216`). The Diagnosis holds; those Cordova and Flutter line citations have moved.
  - **One piece of evidence changed:** native `stop(null)` cancelling a live custom task was MEASURED on Cordova, on both OSes, through its old `stop(taskId)` bug, which WO-009 fixed; those logcats were overwritten by WO-009's sweeps. `stop-scope` (D3 a) measures it again on Flutter and Cordova Android, and `stop-scope-ios` on all four iOS bridges.
- 2026-11-16 — **D3 (a) done: the red MEASURED at the current pins** (WO-002 built `stop-scope` and `stop-scope-ios`, on fetch-forge `feat/WO-002-stop-scope`). React Native `8d9ab19`, Capacitor `3fa169a`, Flutter `5a01d8d` and Cordova `2b5f956` (this worktree's Flutter and Cordova checkouts sat on `4cf0ae2` and `256372c`, whose trees the `--no-ff` merges kept unchanged), native `4.1.1-master-399765c`.

  | Sweep | What | Result |
  |---|---|---|
  | `20261116-232354` | `stop-scope`, Android, FIX | React Native and Capacitor FAIL (Lab2), Flutter and Cordova PASS (Lab1) |
  | `20261116-231623` / `-231952` | `stop-scope-ios`, FIX / FIX-SPM | PASS on all four bridges |

  - **`held`, as predicted:** React Native and Capacitor read jobsCancelled `999` and jobIds `-1681675378`: one `stop()` cancelled the fetch job and left the custom task's. Flutter and Cordova read jobsCancelled `-1681675378,999` and jobIds `-`.
  - **`fired`, as predicted:** on React Native and Capacitor the task's job was still there (actionsDone `action0=missed(present)`) and its fetch reached `configure()`'s retained callback (eventAssertions `configure@0=fired-1-over-max-0`, driverEvents `fetch=1`, fetchTally `com.transistorsoft.fetchlab.task=1`). Flutter and Cordova: the job gone, nothing delivered. **So native `stop(null)` cancelling a live custom task is MEASURED again** (Flutter and Cordova), after WO-009 removed the accidental Cordova reading.
  - **`stop-scope-ios`:** bgCancels `com.transistorsoft.fetch=1,com.transistorsoft.fetchlab.task=1`, taskFires `task:missing=1`, sdkTasks `-`, sdkSubscribers `-`, driverEvents `-` on every cell: the guard for the fix. `stop:nil` removing a kept `TSBGTask` (`[TSBGTask destroy] com.transistorsoft.fetchlab.task` right after `[TSBackgroundFetch stop] (null)`) is now MEASURED on all four bridges, FIX and FIX-SPM: it replaces the lost Cordova iOS reading and Platform parity's "INFERRED for the other three".
  - **D4, `rescheduled` (recorded):** Android, React Native and Capacitor delivered (driverTally `fetch:com.transistorsoft.fetchlab.task=1`, configure@0 delivered 1); Flutter and Cordova did not (driverTally `-`, configure@0 delivered 0), though the SDK received the fetch (fetchTally `com.transistorsoft.fetchlab.task=1`) and logged no `- Stopping on terminate`. iOS delivered on all four, both channels (taskFires `task:ran=1`). So cost 2's premise is MEASURED on Flutter and Cordova Android; the cleared callback behind it is INFERRED from `BackgroundFetch.java:148`, `:272-275` (399765c); and that React Native and Capacitor Android will behave so after the fix is INFERRED (the same native `stop(null)`) until the fix's sweep reads `rescheduled`.
  - **Re-scored:** 236 captures (28 new), 30 not PASS: the 4 predicted reds and the 26 WO-004 captures; no existing verdict changed.
  - **Reviewed (three lenses, three skeptics per finding): 17 findings, 12 upheld, all applied.** `fired` now asserts jobsRegistered `-1681675378,999` in its own step (its `expect: absent` could otherwise pass with no job behind it, as every other absent-job step in the lab guards); `stop-scope` re-swept for it (below). The rest is wording: fetchTally is the SDK's receipt, not a delivery; the measured lines in both descriptions; dated anchor comments on every step; cost 2's MEASURED and INFERRED parts separated; the iOS task-destroy reading. Re-swept: `20261116-234720`, the same readings, with `fired`'s jobsRegistered `-1681675378,999` ok on all four; its captures are the committed ones.
