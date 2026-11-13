---
id: WO-005
title: On BGTaskScheduler's path, a refresh task whose remaining listeners have all finished is never completed, because the expiry handler counts finished listeners as pending
status: draft
created: 2026-11-12
platforms: [ios]
submodules: [native/background-fetch]
branch: fix/WO-005-bgtask-expiry-finished-listeners
issues: []
supersedes: []
---

> **Found by** WO-004's reviews (its Out of scope: "the BGTaskScheduler expiry's finished-subscriber gap"), from source. Mapped on 2026-11-12 by a read-only workflow: four readers (diagnosis, reach, Android parity, tests), then two skeptics who tried to refute the core claim. Both upheld it and narrowed its conditions, below.
>
> **Evidence markers:** **MEASURED** = seen in a log or capture; **PROVEN** = read in source or an SDK header; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at. Nothing here is MEASURED: no XCTest, lab capture or device run has ever reached a BGTaskScheduler expiry.
>
> Citations are native `399765c` (TSBackgroundFetch 4.2.1; its `ios/` is byte-identical to `0670713`). The bridges are their checked-out commits: React Native `85a6bf4`, Cordova `38ad0fe`, Capacitor `9dd204a`, Flutter `11d53b8`. TSLocationManager is bg-forge's `native/ios` at `6190028`, the pointer bg-forge `addc50501` records. Apple's headers are the iPhoneOS SDK in Xcode 27.2.

## Diagnosis

**PROVEN in source.** A BGAppRefreshTask is completed in exactly two places, and neither fires when every remaining listener has already finished.

- **The expiry** (`TSBackgroundFetch.m:167-171`, set in `handleBGAppRefreshTask:` before the task is stored at `:176`) completes the task with NO only when `+[TSBGAppRefreshSubscriber onTimeout]` returns NO (`:170`). `+onTimeout` returns YES whenever any subscriber exists, finished or not (`TSBGAppRefreshSubscriber.m:84-87, :96`).
  - For a subscriber with a timeout block it dispatches `-onTimeout` (`:88-91`), which skips a finished subscriber (`:148`).
  - For a subscriber without one it calls `-[TSBackgroundFetch finish:]` synchronously (`:92-94`), which can complete the task YES from inside the handler.
- **A finish** completes the task with YES only when it leaves every current subscriber finished (`TSBackgroundFetch.m:401-416`). It then clears `bgAppRefreshTask` (`:418`), the only place that does.
- **Nothing re-counts after a change.**
  - `removeListener:` destroys the subscriber and cancels the next request only when none is left (`:324-334`). It never looks at the held task.
  - A finish for an id with no subscriber returns before the count (`:395-398`): a removed listener's late finish, or `finish(nil)`, which maps to `com.transistorsoft.fetch` (`:369`) and matches no consumer's id.

**So:** two listeners A and B, both with timeout blocks. A finishes (1 of 2, `:411`), then B is removed before it finishes. Nothing completes the task: not the remaining finish, not the removal, not the expiry. The app never calls `setTaskCompletedWithSuccess:`.

**The conditions, from the skeptics.** "Never completed" holds only while all of these are true:
1. **Every remaining finished subscriber has a timeout block.** Every in-repo registrant passes one:
   - the four bridges;
   - the Swift API (`BackgroundFetch.swift:138-146`);
   - TSLocationManager (`TSTrackingService.m:625-630`).

   A subscriber without one is finished by `+onTimeout` at the expiry, completing the task YES. The public `addListener:callback:` allows that (`TSBackgroundFetch.h:39`, `.m:303-305`), but no in-repo caller uses it.
2. **No later `finish:` names a remaining subscriber.** `finish:` is not idempotent (`:395-418`), so a repeated finish completes YES. No in-repo consumer sends one on its own (INFERRED).
3. **No listener is added while the task is held.** `addListener:` executes the subscriber at once (`:315-317`), and its finish completes YES. That includes a bridge's `configure()`/`start()`, TSLocationManager starting again, and Swift `onFetch`.
4. **At least one subscriber remains until the expiry.** If the last one is removed too, `+onTimeout` returns NO and the expiry completes NO.

**What happens in each case:**

| Case | Outcome |
|---|---|
| (a) One subscriber finishes | YES, at the finish. MEASURED once on a device in WO-004's check (b). |
| (b) The only subscriber is removed mid-task | NO, at the expiry: the task is held until then. |
| (c) A finishes, then B is removed | **Never completed.** |
| (d) B is removed, then A finishes | YES, at A's finish. |
| (e) B's finish arrives after B's removal | Dropped (`:395-398`). With others left and all finished, as (c); with none left, NO at the expiry. |
| (f) A subscriber restored from NSUserDefaults with no callback (`TSBGAppRefreshSubscriber.m:27-43, :111-117`) | Never executed (`:134`), so it stays unfinished. At the expiry `+onTimeout` finishes it and the task completes YES, after being held all the way. WO-002 suspected defect 6. |
| (g) A timeout block that never calls finish | Never completed: there is no SDK deadline. The app's contract. |
| (h) Only unfinished subscribers with timeout blocks at the expiry | Their timeout blocks run; their finishes complete YES. WO-002 suspected defect 11. |
| (i) A second launch while a task is held | The held task is overwritten at `:176` and never completed. Whether iOS launches a second refresh task while one is outstanding is NOT CHECKED. |

**Apple documents the cost** (`BGTask.h`, paraphrased):
- The expiry handler should cancel and clean up, then call `setTaskCompletedWithSuccess:` so the app can be suspended (`:25-32`).
- An uncompleted task keeps the app running in the background until its time is used up, wasting battery (`:49-52`).
- Not completing before the time expires may get the app killed (`:61-62`).
- Only a task with no expiry handler is marked complete and unsuccessful by the system (`:41`, `:59-60`).
- Whether a never-completed task throttles later launches is NOT CHECKED.

**Adjacent defects, PROVEN in source:**
- **The stale task.** The expiry's NO completion (`:170`) never clears `bgAppRefreshTask`. In (c) nothing ever does. If the process survives, a later `addListener:` delivers a fetch event the OS never sent, and its finish completes the expired task a second time, with YES (`:315-317`, `:416`).
- **The unlocked count.** The count at `:403` reads `_subscribers` without the lock that `+remove:` takes (`TSBGAppRefreshSubscriber.m:57-61`), and the BGTask branch takes no lock of its own. So a removal concurrent with a finish can land either side of the count.

**Who reaches it (INFERRED, from PROVEN source):**
- **Not a single-listener app.** Each bridge registers one fixed id, so removing its only listener is case (b): NO at the expiry.
- **Apps that pair background-geolocation with a fetch plugin.** bg-geo's install docs recommend that pairing.
  - TSLocationManager's listener `"TSLocationManager"` (`TSTrackingService.m:64`) finishes at the start of every fetch (`:603`, `:622`). So one more step reaches (c).
  - That step is app code calling `BackgroundFetch.stop()` during a fetch, before that plugin's finish, while bg-geo stays on. stop()-then-finish is enough: no bridge's `stop()` finishes, so the finish is dropped.
  - The reverse also works: TSLocationManager removed at `:647` (its scheduler off, `:553`) while the fetch plugin's handler is still running.
- **Rarer routes:** a plugin torn down mid-task (Cordova `dealloc`, Capacitor `deinit`), or a Swift app releasing one of two `onFetch` subscriptions.
- **How often:** NOT CHECKED.
- **The phantom rescue (f).** With bg-geo's default `stopOnTerminate: true`, a fetch that launches the app may carry a restored `"TSLocationManager"` with no callback. It rescues the task at the expiry (YES), but holds it until then.

**Not a 4.2.1 regression (PROVEN).** The behaviour dates from 2020-2021 (`8fb13da5`, `fbae69c9`). `50990075` (2026-04-15, first tagged 4.1.1) rewrote `+onTimeout` and the count without changing their meaning. No 4.2.0→4.2.1 hunk touches this path. bg-geo apps, the main population, are on 4.1.x today: TSLocationManager 4.8.3 requires `~> 4.1.0`. So it is no reason to hold the plugin releases that link 4.2.1.

## Proposed fix

**Change 1, the twin of WO-004's change 2 (recommended).**
- Move the expiration block into a method, as WO-004 did with `_expireLegacyFetch`. It:
  1. takes the held task once, under the `@synchronized (self)` lock that `_takeCompletionHandler` uses, and clears `bgAppRefreshTask`;
  2. scans for unfinished subscribers before calling `+onTimeout`;
  3. calls `+onTimeout`, so timeout blocks still run for the unfinished, and callback-less subscribers are still finished;
  4. completes the task with NO when no unfinished subscriber was left. It completes only if `+onTimeout`'s synchronous finish did not already, so the task is never completed twice.
- Route the finish branch at `:414-418` through the same take-once. Both completions then release the task, which ends the stale-task re-delivery above.
- Log a distinct line when the expiry completes the task. Today `:170` logs nothing, so a device log cannot tell completed from not.
- `+onTimeout`'s contract does not change. `testClassOnTimeoutReturnsTrueWithSubscribers` (`TSBackgroundFetchTests.m:494-502`) and the legacy expiry keep their meaning.
- Correct the comments that misstate the contract: `TSBackgroundFetch.m:169` and `TSBGAppRefreshSubscriber.m:81-83`.

**Change 2, completing at once on removal (optional, D2).**
- While a task is held, re-count in `removeListener:`, and on a finish whose id is not found. Complete when no unfinished subscriber is left.
- Without it, (b), (c) and (e) still hold the task until the expiry, which is the battery cost `BGTask.h:49-52` describes.
- WO-004's equivalent on the legacy path, its change 4, was not approved.

**Decisions:**

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | The value when the expiry completes an only-finished task | NO, as `:170` does and as WO-004's D3 (Failed) did; or YES, since the remaining listeners finished | NO, consistent with WO-004 |
| D2 | Change 2 | Take it; or leave removal as is | Your call, given WO-004's change 4. Taking it stops the wasted background time. |
| D3 | Scope of the adjacent defects | Fold in the stale-task release, which change 1's take-once covers anyway; leave the rest to Out of scope | Fold in the stale-task release only |
| D4 | Where it ships | 4.2.x (4.2.2); or also a 4.1.x patch for today's bg-geo apps | 4.2.x, timed with TSLocationManager's widening to 4.2. A 4.1.x backport only if you want bg-geo apps fixed before that. |
| D5 | Where the device check's second listener lives | `LabModule.m`, behind a launch argument, reusing its Darwin-notification hooks; or `AppDelegate.swift`, calling the SDK directly | `LabModule.m`: a later Simulator scenario for the legacy-path twin can reuse it |

The anchor `// (WO-005)` goes at each change, the tests are `test_wo_005_…`, and the native CHANGELOG entry carries `(WO-005)`.

## Scope check

- **`+onTimeout`** is called by this expiry (`:170`) and by the legacy expiry (`_expireLegacyFetch`, `:222`). Change 1 keeps its contract and counts in the caller, as WO-004 did, so the legacy path and its tests are untouched (INFERRED).
- **`_finishAppRefreshWithTaskId:`** is reached from `finish:` (`:378`) by:
  - every bridge's `finish`;
  - TSLocationManager (`TSTrackingService.m:603, :622, :627`);
  - the Swift API;
  - `+onTimeout`'s synchronous finish inside the expiry (`TSBGAppRefreshSubscriber.m:92-94`).

  The take-once must cover that last one too, or the task is completed twice.
- **`removeListener:`** (only if change 2): every bridge's `stop()`, Cordova `dealloc`, Capacitor `deinit`, TSLocationManager's `:647`, and releasing a Swift `EventSubscription`.
- **Shared state with the legacy path:** `_completionHandler` and the `_takeCompletionHandler` lock. `_finishAppRefreshWithTaskId:` prefers `bgAppRefreshTask` over the legacy handler (`:414`), so a task left set leaks into the legacy branch and its tests.
- **Processing tasks** (`TSBGTask`, `TSBGTask.m:38, :252`) are not touched.

## Platform parity

- **iOS:** affected, as above (`TSBackgroundFetch.m:167-171, :389-418`; `TSBGAppRefreshSubscriber.m:80-97, :143-153`).
- **Android: not affected the same way.** Android has no subscriber count to wait on:
  - The fetch is JobScheduler job 999, wrapped in a `BGTask`, with one callback, where the last `configure()` wins (`BackgroundFetch.java:72, :85`).
  - The first `finish(taskId)` completes the job (`BackgroundFetch.java:195-201` → `BGTask.java:106-121`, which calls every completion handler, i.e. `jobFinished`).
  - The OS stop always finishes the job (`FetchJobService.java:64-76`, `jobFinished` at `:74`).
  - background-geolocation has no second native consumer on Android (INFERRED from the reach map).
- **Android is affected differently:**
  - The SDK's 60 s timeout only notifies, so an unfinished job is held until the OS stops it.
  - `onStopJob` never finishes or removes its `BGTask`, so a stale entry can shadow the next run with the same id.
  - A second consumer overwrites the shared callback, so its events go missing or to the wrong consumer.

  These are WO-002 suspected defect 3's ground, or their own work order. 4.2.1 changes nothing on Android: `android/` is identical from `5de5242` to `399765c`.

## Test plan

- **XCTest: the red/green proof.**
  - **Setup:**
    - Add a new class, `TSBackgroundFetchWO005Tests`. Don't subclass WO-004's class, or XCTest re-runs its tests.
    - A test category declares `-handleBGAppRefreshTask:`.
    - A stand-in `WO005Task`: `BGAppRefreshTask` cannot be instantiated (`BGTask.h:44-45`). It holds an `expirationHandler`, and its `-expire` calls the handler and clears it. Its `-setTaskCompletedWithSuccess:` records every value and clears the handler, as `BGTask.h:30-32, :40` describe.
    - Between tests, reset `bgAppRefreshTask` (through KVC), `fetchScheduled`, and the persisted `TSBGAppRefreshSubscriber` defaults key. Also set the in-memory subscribers aside, as WO-004's tests do.
  - **Red at `399765c`:** `…OnlyFinishedSubscribersCompletesTask` (case c), `…OnlyFinishedSubscribersReleasesTask`, and `…AfterLastSubscriberRemovedReleasesTask` (the stale task).
  - **Red only if D2 takes change 2:** `…RemoveLeavingOnlyFinishedSubscribersCompletesTask`.
  - **Guards that pass today:**
    - `…AfterLastSubscriberRemovedCompletesTask` (b);
    - `…LeavesTaskToSubscriberTimeout` (h);
    - `…FinishedAndUnfinishedLeavesTaskToUnfinished`;
    - `…RemoveBeforeFinishCompletesOnFinish` (d);
    - `…FinishedSubscriberWithoutTimeoutCompletesOnce`, which catches a naive copy of change 1 that completes twice.
  - **Order:** run them red at `399765c` first and record which fail (MEASURED), then green on the fix with all 45 existing tests.
- **On a device: designed, not run.** This would be the defect's first measurement. It uses WO-004's driver (`devicectl` + LLDB, Log 2026-11-12) on an iPhone:
  - **A second listener compiled into FetchLab,** started by a launch argument. It goes in either `AppDelegate.swift` or `LabModule.m` (D5), never an LLDB block, whose memory may be freed before it is called.
  - **Two orders:**
    - React Native's listener finishes, then the second is removed;
    - the second finishes, then `stop()` removes React Native's.
  - **Expiry and evidence:**
    - Then `_simulateExpirationForTaskWithIdentifier:`, which works only on a running simulated task. It has never been run in this repo.
    - Completed or not shows in BackgroundTasks' "Marking simulated task complete" line. A breakpoint on `-[BGTask setTaskCompletedWithSuccess:]` won't do: WO-004's run never hit it, though its tasks completed (MEASURED, its transcript).
  - **Runs:** `399765c` first, to measure the defect red, then the fix. Record what iOS 26.7.1 does with an expired, uncompleted task: kill, suspend, or nothing visible.
- **The lab** cannot reach this path: the Simulator answers every BGTaskScheduler request with Unavailable. Its seven React Native iOS cells, on both channels, guard the shared code the fix touches.

## Out of scope

Each is its own work order if wanted:
- **(f)** A callback-less restored subscriber is finished at the expiry, completing YES after the task was held throughout (WO-002 suspected defect 6).
- **(g)** There is no SDK deadline for a timeout block that never finishes.
- **(h)** YES after a timeout (WO-002 suspected defect 11).
- **(i)** A second launch overwrites a held task. Related, NOT CHECKED: a fast finisher, such as TSLocationManager, may count a subscriber whose `-execute` has not yet run as already finished.
- **The general race** between `finish:`, `removeListener:` and `_subscribers`, beyond the take-once that change 1 adds.
- **Flutter's teardown** selector and the event sink its engines share (WO-004's Out of scope). When that is fixed, it must not call `removeListener` on every engine's detach. With bg-geo present, that would turn each secondary engine's teardown during a fetch into case (c).
- **The Swift `EventSubscription`'s removal by identifier,** and the docforge Swift examples (WO-004's Out of scope; WO-002 suspected defect 8).
- **Android hardening:** stale `BGTask` entries, and the timeout that only notifies (WO-002 suspected defect 3).
- **Widening TSLocationManager's TSBackgroundFetch range** (bg-forge).

## Not an acceptable shortcut

- **Making `+onTimeout` return NO when every subscriber has finished.** The legacy expiry uses it too, and `testClassOnTimeoutReturnsTrueWithSubscribers` pins its contract. WO-004 kept the contract and counted in the caller; do the same.
- **Completing the task unconditionally in the expiry handler.** That cuts off unfinished subscribers whose timeout blocks are still running (case h), which is the contract the handler exists for.
- **A copy of change 1 without the take-once.** `+onTimeout`'s synchronous finish of a callback-less subscriber can complete the task YES inside the handler, and the copy then completes it again with NO. `…FinishedSubscriberWithoutTimeoutCompletesOnce` guards this.
- **Having each bridge finish before it removes its listener.** The task is the SDK's to complete. TSLocationManager, the Swift API and native consumers would still reach the gap, and a removed listener's finish must not be what the task waits on.
- **Removing the expiry handler so that iOS marks the task unsuccessful** (`BGTask.h:41`). Timeout blocks would then never run, for every consumer.
- **Dismissing it as rare.** bg-geo plus a fetch plugin is the pairing bg-geo's docs recommend, and the cost is an app kept awake until its time runs out, and possibly killed.
- **Holding the plugin releases that link 4.2.1 for it.** The behaviour is from 2021, and its main population can't install the 4.2-requiring bridges until TSLocationManager widens its range. Whether to release is a separate decision.

## Verification

- [ ] D1–D5 decided
- [ ] `test_wo_005_…` red at `399765c` where expected, green on the fix; all 45 existing `TSBackgroundFetchTests` pass
- [ ] On a device (WO-004's driver, a second listener): at `399765c` the finished-then-removed order leaves the task uncompleted at a simulated expiry (MEASURED red); on the fix it is completed once, with D1's value; what iOS does with the uncompleted task recorded
- [ ] React Native iOS cells green on the fix's label, CocoaPods and SwiftPM
- [ ] Native CHANGELOG entry carries `(WO-005)`
- [ ] Platform parity: Android not affected, as cited; nothing to change there

## Log

- 2026-11-12 — **Created (draft)** from WO-004's Out of scope, at Chris's request ("Yes, draft it").
  - **The mapping:** a read-only workflow of four readers and two skeptics. Both skeptics failed to refute the core claim. Their narrowed conditions are in the Diagnosis.
  - **Checked by hand while writing:** the line numbers above at `399765c`, Android's at the same commit, TSLocationManager's listener at bg-forge `native/ios` `6190028`, and `BGTask.h`.
  - **Not done yet:** nothing measured, no branch, no code.
  - **Asked at the same time:** whether this should hold the plugin releases linking 4.2.1. It should not: the behaviour is pre-existing, and the gate for those releases is TSLocationManager's widening (WO-001).
