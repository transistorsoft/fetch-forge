---
id: WO-004
title: Where BGTaskScheduler is unavailable (the iOS Simulator), stop() leaves UIKit's legacy background fetch armed, and a fetch after it is requested again and its completion handler never called
status: done
created: 2026-11-11
platforms: [ios]
submodules: [native/background-fetch]
branch: fix/WO-004-ios-stop-legacy-fetch
issues: []
supersedes: []
---

> **Found by** WO-002's device lab: suspected defect 12, which the Tier 2 scenario `ios-stop-start` measured red.
>
> **Evidence markers:** **MEASURED** = seen in a lab capture; **PROVEN** = read in source; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at.
>
> Source citations are native `5de5242`, the lab's pin until 2026-11-12, when `FIX` and `FIX-SPM` moved to 4.2.1's `399765c`. `TSBackgroundFetch.m` is the same at `fc74063`; the fix (`0670713`, released in 4.2.1) changes it. The bridges' are their checked-out commits: React Native `85a6bf4`, Cordova `38ad0fe`, Capacitor `9dd204a`, Flutter `11d53b8`.

## Diagnosis

**MEASURED** (`labs/captures/react-native-ios/ios-stop-start/{FIX,FIX-SPM}/stopped`, sweeps `20261111-122633` and `20261111-122826`, Lab1, iPhone 17 Pro Simulator, iOS 27.2; native `4.1.1-master-5de5242` (iOS 4.2.0), react-native plugin `85a6bf4`; CocoaPods and SwiftPM alike). Those red captures are in git at `3f74ec3`; since 2026-11-12 the same paths hold `FIX`'s and `FIX-SPM`'s green ones, on native 4.2.1 (Log).

The app in front, its boot `configure()` resolved, then `stop()`, then `fire: fetch` (`simctl push` with `content-available`) 3 s later:

| After the push | Line |
|---|---|
| 0.000 s | SpringBoard `Executing <SBBackgroundFetchTask: app<com.transistorsoft.fetchlab.react…>`; UIKit calls the app delegate, `LAB point=legacyFetch state=active remaining=30.0` |
| 0.000 s | `[TSBackgroundFetch performFetchWithCompletionHandler]`, then a second `submitTaskRequest: <BGAppRefreshTaskRequest: com.transistorsoft.fetch …>`, refused (`BGTaskScheduler unavailable — falling back to legacy API`) |
| — | no delivery to the driver; the probe reads `subscribers=- pendingFetch=true bgTask=true` |
| +24.6 to +28.7 s | the SDK's UIBackgroundTask ends (`bgTask=false`); `pendingFetch=true` to the step's end |
| +31.2 s (pods), +31.3 s (SPM) | SpringBoard `Error completing <SBBackgroundFetchTask …>: invalid response (the process probably exited)`, then `with result: Error` |

The scenario's `stopped` step reads `bgSubmits com.transistorsoft.fetch:appRefresh=2` (expected 1) and `springboardResults Error=1` (expected `NoNewDataAvailable=1`): FAIL. Its `restarted` step (`stop()`, `start()`, a fetch) passes: the fetch is delivered and finished.

**The code path (PROVEN, source):**
- **configure() arms the legacy API where BGTaskScheduler refuses.** `scheduleBGAppRefresh` submits a `BGAppRefreshTaskRequest`; on `BGTaskSchedulerErrorCodeUnavailable` it calls `setMinimumFetchInterval`, which sets UIKit's minimum background fetch interval (`TSBackgroundFetch.m:117-122`, `:153-158`).
- **stop() cancels only the BGTaskScheduler half.** Every bridge's `stop()` with no taskId calls `removeListener:PLUGIN_ID` (React Native `RNBackgroundFetch.mm:92-94` at `85a6bf4`, Cordova `CDVBackgroundFetch.m:100`, Capacitor `BackgroundFetchPlugin.swift:90`, Flutter `BackgroundFetchPlugin.m:135`). Once no listener is left, that runs `cancelBGAppRefresh` (`:288-290`). Its iOS 13+ branch cancels the request and clears `fetchScheduled` (`:142-145`); only the pre-iOS 13 branch sets the interval back to Never (`:146-149`). So the interval configure() set stays.
- **A fetch after it re-arms and is held.** `performFetchWithCompletionHandler:applicationState:` clears `fetchScheduled` and calls `scheduleBGAppRefresh` (`:182-183`), so the interval is set again. It keeps UIKit's handler, begins a UIBackgroundTask, and hands the fetch to the listeners (`:185-198`), of which there are none.
- **Nothing completes the handler.** The UIBackgroundTask's expiration block calls `[TSBGAppRefreshSubscriber onTimeout]` and ends the background task (`:188-196`). With no listener, `onTimeout` does nothing and returns `NO` (`TSBGAppRefreshSubscriber.m:80-96`), and the block ignores the return. The BGTaskScheduler path's expiration handler, by contrast, completes the task with `NO` when `onTimeout` returns `NO` (`TSBackgroundFetch.m:165-169`).

**Where it reaches users:**
- **The iOS Simulator** (MEASURED): BGTaskScheduler is unavailable there, so every configured app takes the legacy fallback. A developer who tests `stop()` on the Simulator sees the app still fetched, and each fetch after it end in `Error` about 31 s later.
- **A device** (INFERRED): not reached. configure() takes BGTaskScheduler's path, so the legacy interval is never set, and `cancelBGAppRefresh` cancels the request that was submitted.
  - (Corrected 2026-11-11, from the review below.) That holds only when the app permits `com.transistorsoft.fetch`. (Corrected 2026-11-12, the device check, Log: on a device it fails only for an app with no `BGTaskSchedulerPermittedIdentifiers` key, MEASURED. Apple documents that the key's presence disables the legacy fetch methods on iOS 13+, so an app whose key lacks `com.transistorsoft.fetch` takes the SDK's legacy branch but UIKit ignores it, INFERRED.)
    - React Native's install docs make listing it a manual "required" step (`docs/INSTALL-AUTO-IOS.md:41-45`); Cordova's `plugin.xml:28-33` adds it.
    - An app that skipped it never registers the refresh task, so `useTaskScheduler` stays NO. configure() then sets the legacy interval at `:129-131`, the iOS 13+ branch, on a device too (PROVEN, source).
    - Whether iOS then keeps running opportunistic legacy fetches after `stop()` is INFERRED.
- **Anywhere else BGTaskScheduler answers Unavailable to a configured app:** NOT CHECKED (an iOS app on a Mac, for one). configure() only schedules when `backgroundRefreshStatus` is Available (`:208-215`), so a user who turned Background App Refresh off does not reach the fallback.
- **Which apps hand the legacy fetch to the SDK at all** (PROVEN, source): Cordova's plugin (`CDVBackgroundFetch.m:23-27`) and Flutter's (`BackgroundFetchPlugin.m:29-32`) forward UIKit's `performFetchWithCompletionHandler:`. React Native's category that did (`RNBackgroundFetch+AppDelegate.m`) is compiled by neither its podspec nor its `Package.swift`, so only an app that forwards it itself, like the lab host, reaches the SDK. Capacitor's plugin has no forward (NOT CHECKED whether Capacitor's app template adds one). Where nothing forwards it, the fetch never reaches the SDK, but the interval still stays set after `stop()`.

## Proposed fix

In native `TSBackgroundFetch.m`, two changes:

1. **`cancelBGAppRefresh`: also set UIKit's minimum background fetch interval back to Never on iOS 13+**, on the main queue, as the pre-iOS 13 branch already does (`:146-149`). After that, `stop()` leaves nothing armed, and UIKit answers a SpringBoard fetch itself (`NoNewDataAvailable`, without calling the app; MEASURED 2026-11-10 for an app whose interval is Never, WO-002 D3 spike). (Disproved 2026-11-11 for SpringBoard-delivered fetches: UIKit calls the app delegate whatever the interval. See the Log.) `start()` re-arms it through `scheduleBGAppRefresh`'s fallback, as today. Both writes are dispatched to the main queue, so `stop()` then `start()` lands in that order (INFERRED).
2. **The legacy expiration block: complete UIKit's handler when no listener handled the timeout.** When `onTimeout` returns `NO` and the completion handler is still held, call it (with `UIBackgroundFetchResultFailed`, per D3) and clear it, mirroring the BGTaskScheduler path's `setTaskCompletedWithSuccess:NO` (`:168`). After change 1, this is reached only by a legacy fetch already in flight when its last listener is removed.

The anchor `// (WO-004)` goes at both changes, and the native CHANGELOG entry carries `(WO-004)`.

**Decided 2026-11-11 by Chris ("Approve as recommended"): D1 include change 2, D2 always, D3 `UIBackgroundFetchResultFailed`.**

**Revised 2026-11-11, approved by Chris ("Approve revision"), after change 1's premise was disproved and an independent review (Log):**
- **Change 1** stays. Its case is opportunistic legacy fetches, which iOS schedules from the interval. That covers where BGTaskScheduler is unavailable (the Simulator, `:121`) and where the app does not permit `com.transistorsoft.fetch` (`:129-131`, a device too). A fetch SpringBoard delivers reaches the app whatever the interval (MEASURED), so change 1 alone does not stop that one. Its effect on opportunistic fetches is INFERRED, since the lab cannot trigger them.
- **Change 2** is tightened: it completes the held handler when no *unfinished* subscriber took the timeout, rather than only when no subscriber is left. A subscriber that already finished does not take a timeout (`TSBGAppRefreshSubscriber.m:148`), yet `+onTimeout` counts it (`:84-96`).
  - (Added 2026-11-11, from the second review: part of change 2's correctness.) With change 2 the expiry, on main, is a second place that completes the held handler. A subscriber's finish arrives on its caller's thread, React Native's module queue among them, so the two could both call it, or release it twice.
  - Both now take the handler with `_takeCompletionHandler`, under the lock `_backgroundTask` already uses, and only the one that gets it completes it. Storing it in `performFetchWithCompletionHandler` takes the same lock.
  - The BGTaskScheduler path keeps its own `![onTimeout]` test (`:168`), so the two expiries no longer agree on a finished subscriber (see Out of scope).
- **Change 3 (new):** `performFetchWithCompletionHandler`, when no subscriber is left, completes UIKit's handler at once with `UIBackgroundFetchResultNoData` and returns. It does not reschedule, does not begin a background task, and does not keep the handler. No listener had opted in to the fetch, and rescheduling would re-arm what `stop()` cancelled. (Corrected 2026-11-11, the commit-time review: this said nothing would finish such a fetch. A listener added later in that launch would have; that is the trade-off below.) A cold launch of a still-configured app is unaffected, because its subscribers are loaded back at `+load` before the delegate is called (MEASURED by `ios-fetch-sim`'s `cold` step).
  - (Added 2026-11-11, the second review.) The trade-off: a listener added later in that launch would have finished such a fetch. With nothing persisted (an install that never configured, a launch after a session that called `stop()`, or a wiped defaults domain), a fetch that arrives before that launch's `configure()` is now answered with no data, where the old code held it and `configure()`'s `addListener:` delivered it.
  - Kept as approved: no listener had opted in.
  - It is reached on the Simulator (a pushed fetch) and, INFERRED, on a device on the `:129-131` path: after a defaults wipe, or after the configure-then-stop race (Out of scope), which arms the interval with no listener in the same launch. (Corrected 2026-11-11, the commit-time review: this said only after a defaults wipe.)
- **Not included:** completing a held handler at once when the last listener is removed (proposed as an optional change 4). Change 2 still completes it at the expiry.

**DECISION D1: change 2 in this work order or not.**
- **Recommended: include it.** It is the other half of the measured symptom (the handler never called), it is a few lines, and it makes the two expiration paths agree.
- **Alternative: change 1 only.** It turns `ios-stop-start` green on its own (INFERRED), but leaves the in-flight case: a fetch held when the last listener goes away is still never completed.

**DECISION D2: reset the interval always, or only when the fallback set it.**
- **Recommended: always** (on iOS 13+, in `cancelBGAppRefresh`). On a device the interval was never set, so setting Never changes nothing (INFERRED), and the code stays a mirror of the pre-iOS 13 branch. (Corrected 2026-11-11: on a device whose app does not permit `com.transistorsoft.fetch`, configure() did set it, at `:129-131`. That is a reason to reset it always, not only after the Simulator's fallback.)
- **Alternative: a flag** set where the fallback arms the interval (`:121`), checked in `cancelBGAppRefresh`. More state for no behaviour difference that we know of.

**DECISION D3: the result change 2 reports.**
- **Recommended: `UIBackgroundFetchResultFailed`**, the legacy counterpart of `setTaskCompletedWithSuccess:NO`.
- **Alternative: `UIBackgroundFetchResultNoData`.** Kinder to UIKit's scheduling, but it says the fetch ran and found nothing, which it did not.

## Scope check

- **`cancelBGAppRefresh`** has one caller, `removeListener:` (`:288-290`), which runs it once the last listener is gone. `removeListener:` is called by every bridge's `stop()` with no taskId (cited above) and on plugin teardown: Cordova's `dealloc` (`CDVBackgroundFetch.m:194`), Capacitor's `deinit` (`BackgroundFetchPlugin.swift:38`), Flutter's `detachFromEngineForRegistration:` (`BackgroundFetchPlugin.m:70`). On each, change 1 also resets the legacy interval, matching what the cancel already does to the BGTaskScheduler request. (Corrected 2026-11-11, the commit-time review: this said change 1 also stops the legacy fetch. The interval governs only the fetches iOS schedules itself, INFERRED. A fetch SpringBoard delivers still arrives, MEASURED, and change 3 answers it.)
  - (Corrected 2026-11-11, the second review.) Flutter never calls that method. The protocol's selector is `detachFromEngineForRegistrar:`, sent only to an instance registered with `publish:`, which the plugin never calls. So a Flutter teardown does not reach `removeListener:`. That is a pre-existing Flutter-bridge bug, out of scope here.
  - The native `stop:` and the Swift `stop()` do not remove the listener either (`BackgroundFetch.swift:71-73`; PROVEN, the second review). For a native consumer neither change 1 nor change 3 follows a stop, only the release of its `EventSubscription`.
- **`setMinimumFetchInterval`** is called from `scheduleBGAppRefresh`'s fallback (`:121`) and its pre-iOS 13 branches (`:130`, `:133`). (Corrected 2026-11-11: only `:133` is pre-iOS 13. `:129-131` is the iOS 13+ branch for an app that does not permit `com.transistorsoft.fetch`. At the iOS 15 minimum, `:133` and `cancelBGAppRefresh`'s old pre-iOS 13 reset were dead code, so before this change the cancel never reset the interval on any supported iOS.) Untouched. `start()`, configure() and each fetch's reschedule still re-arm through it.
- **The legacy expiration block** (`:188-196`) runs only for fetches handed to `performFetchWithCompletionHandler:`. With a listener that handles the timeout, `onTimeout` returns `YES` and change 2 does nothing: `ios-timeout-sim`'s path (the plugin's onTimeout, then the driver's finish) is unchanged.
- **The BGTaskScheduler path** (`handleBGAppRefreshTask:`, `:161-176`) and custom tasks (`TSBGTask`) are untouched.
- **The native Swift API** (`SwiftInterface/BackgroundFetch.swift`) wraps the same methods; whether its own stop path removes the listener is NOT CHECKED. (Corrected 2026-11-11: it does not. The Swift `stop()` calls only `stop:` (`:71-73`), and the listener goes when `onFetch`'s `EventSubscription` is released; PROVEN by the second review, the first bullet's sub-bullets above.)

## Platform parity

- **iOS:** affected where BGTaskScheduler is unavailable (MEASURED on the Simulator, on both distribution channels; device not affected, INFERRED, as above). (Corrected 2026-11-11 and 2026-11-12: also on a device for an app with no `BGTaskSchedulerPermittedIdentifiers` key, the `:129-131` path; that path MEASURED on an iPhone on the fix, the defect itself not run there. Log.) The fix is in native `TSBackgroundFetch.m` and reaches every bridge through the native library. Whether each bridge picks up a native patch release without a change: React Native's podspec requires `~> 4.2` (WO-002 Log, 2026-11-10); the others' iOS requirements are NOT CHECKED here.
- **Android:** N/A. There is no legacy interval to leave armed. `stop()` with no taskId finishes any running task, cancels every job and clears the fetch callback (`BackgroundFetch.java:147-160`), and the app holds no job afterwards (MEASURED by `api-smoke`, `jobIds -`). Android's own stop/start question is WO-002's suspected defect 2 (`stop-start`), a different defect.

## Test plan

- **The lab (the proof).** A variant pinning native at the fix branch's commit (e.g. `labs/harness/variants/WO-004.yaml`, its iOS label built by `publish_native_fetch_ios.sh` from that commit), on both channels:
  - **Change 2 alone first** (if D1 keeps it): `ios-stop-start`'s `stopped` reads `legacyResults failed=1` at the expiry (`LAB point=legacyFetchComplete result=failed`): the handler is completed.
  - **Both changes:** `stopped` green. No delivery, `bgSubmits` configure's alone, SpringBoard `NoNewDataAvailable`. INFERRED: UIKit no longer calls the delegate, so `legacyFetchStates` reads `-`. (Corrected 2026-11-11: UIKit still calls the delegate; with changes 1 and 2 alone `stopped` stayed red, and change 3's at-once answer is what turned it green. `legacyFetchStates` reads `active=1`.)
  - `restarted` stays green: `start()` re-arms and the fetch is delivered.
  - No regression: `ios-fetch-sim`, `ios-timeout-sim`, `ios-task-sim`, `ios-registration`, `api-smoke-ios` and `api-constants` stay green on the fix label.
- **XCTest** (`TSBackgroundFetchTests.m`, 39 tests): all pass. A new `test_wo_004_…` for change 2 needs the expiration block reachable from a test (it is an inline block today), for instance factored into a method the test can call; INFERRED, not designed further.
  - (Revised 2026-11-11.) The target runs without a host app, so a stand-in replaces `UIApplication.sharedApplication` where a test must see what the SDK hands UIKit.
  - Tests for change 1 (the reset, and that a later listener-less fetch leaves it Never), change 2 (no subscriber; only finished subscribers; a subscriber that takes the timeout keeps it) and change 3 (completed at once with `NoData`, no interval written, no background task begun).
- **On-device:** not needed for the defect, which a device does not reach (INFERRED). (Corrected 2026-11-11: needed for an app whose Info.plist does not permit `com.transistorsoft.fetch`, the `:129-131` path. Not yet designed.) A device check that `stop()`/`start()` still behave on BGTaskScheduler's path belongs to the manual tier (`ios-device-bgtask`), not yet designed. (2026-11-12: both were run by hand on an iPhone with `devicectl` and LLDB, outside the lab's tooling, the first as an app with no `BGTaskSchedulerPermittedIdentifiers` key (Verification); the tier itself is still WO-002's to design. Log.)

## Out of scope

- **WO-002's suspected defect 11:** a timed-out fetch reported to the OS as a success (`TSBackgroundFetch.m:368-377`). Its own work order if confirmed.
- **The SDK's `stop:nil` not cancelling the fetch request by itself.** The cancel lives in `removeListener:`, and every bridge calls both. Whether a native consumer calling `stop:nil` alone should cancel is an API question, NOT CHECKED, and its own work order if wanted.
- **Any change to the BGTaskScheduler path, custom tasks, or Android.**
- **Removing or redesigning the legacy fallback.**
- (Added 2026-11-11, from the two reviews; each its own work order if wanted.)
  - **`configure()` then an immediate `stop()`:** configure's delayed schedule runs after the cancel and arms the interval with no listener (`:208-217`: its `status:` block, `:211-216`, runs later on the main queue, `:201-206`). Pre-existing.
  - **A finish that arrives after `stop()` is dropped** (`:352-356`), so change 2 completes that fetch as `Failed` at the expiry. The optional change 4 was not approved.
  - **The BGTaskScheduler expiry's finished-subscriber gap:** `handleBGAppRefreshTask`'s `![onTimeout]` (`:168`) leaves a task uncompleted when every remaining subscriber has finished. That is change 2's case, on the other path. (2026-11-12: WO-005, `draft`.)
  - **The native `stop:` and the Swift `stop()` do not remove the listener,** and the docforge Swift examples discard `onFetch`'s `EventSubscription`. Releasing it removes the listener and cancels the request `configure()` just submitted (the second review; NOT CHECKED here).
  - **Flutter's teardown selector** (`detachFromEngineForRegistration:` instead of `detachFromEngineForRegistrar:`, and no `publish:`). A fix must account for every engine sharing one listener id.
  - **A second legacy fetch while one is held** overwrites the first's handler, which is then never completed. Pre-existing; change 3 answers the second at once only when no subscriber is left.

## Not an acceptable shortcut

- **Completing a listener-less legacy fetch at once with `NoData`, as the whole fix.** It ends SpringBoard's `Error`, but `performFetchWithCompletionHandler` still requests the fetch again first (`:182-183`), so `stop()` stays undone and the app keeps being fetched. `ios-stop-start`'s `bgSubmits` stays red. (Revised 2026-11-11, approved: taken as change 3 together with skipping that reschedule, which removes the objection. Changes 1 and 2 stay.)
- **Resetting the interval in each bridge's `stop()`.** The cancel is the SDK's. Four bridges and the Swift API would each need it, and a native consumer's `removeListener:` would still leave it armed.
- **Dropping the legacy fallback**, so that configure() fails on the Simulator. It is what makes fetch testable on the Simulator for every customer; removing it is a product decision, not this fix.
- **Changing the lab to avoid the symptom:** the lab AppDelegate not forwarding legacy fetches after `stop()`, or `ios-stop-start`'s `stopped` expectations moved to today's behaviour. The lab measures the shipped SDK and must not compensate for it.
- **Dismissing it as Simulator-only.** The Simulator is where customers develop and test fetch, and a `stop()` that does not stop, with fetches ending in `Error`, is what they would report.

## Verification

- [x] D1–D3 decided (2026-11-11, as recommended)
- [x] `ios-stop-start` `stopped` green, `restarted` still green, on a label built from the fix commit, CocoaPods and SwiftPM (Lab1 and Lab2) (2026-11-11, label `4.1.1-WO-004-0670713`: sweeps `20261111-164604` and `20261111-165050` on Lab1, one run per channel on Lab2; Log)
- [x] If D1 keeps change 2: `stopped` with change 2 alone reads `legacyResults failed=1` (2026-11-11, Lab1, CocoaPods: `failed` at +25.997 s; the capture was overwritten by the next run, the numbers are from its console output)
- [x] The other react-native iOS cells green on the fix label (2026-11-11, the same two sweeps)
- [x] `TSBackgroundFetchTests` pass, including `test_wo_004_…` (2026-11-11: 45 of 45 at native `0670713`, a fresh build on the scratch Simulator. Five of the six `test_wo_004_…` shown red without their change; the sixth, `…LeavesHandlerToSubscriberTimeout`, is a guard that passes without it by design. Log)
- [x] Native CHANGELOG entry carries `(WO-004)` (`CHANGELOG-iOS.md`, `## Unreleased`, in `0670713`)
- [x] WO-002's suspected defect 12 points at this work order's result (2026-11-11)
- [x] On a device (Test plan): an app with no `BGTaskSchedulerPermittedIdentifiers` key (the `:129-131` path), and `stop()` then `start()` on BGTaskScheduler's path (2026-11-12, iPhone 17 Pro, iOS 26.7.1, FetchLab with a device build of `0670713`, driven by `devicectl` and LLDB: both PASS; Log). (Corrected 2026-11-12: the box said "an app that does not permit `com.transistorsoft.fetch`". Apple documents that the key's presence disables the legacy fetch methods on iOS 13+, so on a device that path is live only for an app with no key, which is what was measured.)

## Log

- 2026-11-11 — created from WO-002's suspected defect 12, measured red by `ios-stop-start` on both channels (WO-002 Log, 2026-11-11). Draft, awaiting Chris's approval and D1–D3.
- 2026-11-11 — **approved** by Chris with D1–D3 as recommended ("Approve as recommended"). Status `in-progress`.
  - **Re-verified against current source before any code:** native `origin/master` is `478767b`, which is the lab's pin `5de5242` plus one `.gitignore` line. `TSBackgroundFetch.m`, `TSBGAppRefreshSubscriber.m` and `TSBackgroundFetchTests.m` are byte-identical, so every citation above holds. (The parent repo's pointer, `fc74063`, is an ancestor of both, with the same sources.)
  - **Branches:** `fix/WO-004-ios-stop-legacy-fetch` in `native/background-fetch` from `origin/master` `478767b`, and the same name in the parent, from `master` `b5fbb61`, for this work order, the lab variant and the captures.
- 2026-11-11 — **Implemented both changes and measured them: change 2 works, change 1's premise does not hold (REVERSAL, MEASURED). Stopped for Chris's decision, nothing committed.**
  - **Native, uncommitted on `fix/WO-004-ios-stop-legacy-fetch`:**
    - change 2: the legacy expiry block moved into `_expireLegacyFetch`, which completes UIKit's handler with `Failed` when no subscriber took the timeout;
    - change 1: `cancelBGAppRefresh` sends `setMinimumBackgroundFetchInterval:Never` to the main queue on every iOS version;
    - three `test_wo_004_…` XCTests;
    - an `## Unreleased` changelog line carrying `(WO-004)`.
  - **XCTest** (scratch iPhone 17 Pro Simulator `WO004-XCTest`, iOS 27.2):
    - The baseline at `478767b` passes 39 of 39; with the fix, 42 of 42.
    - **The target has no host app.** `TSBackgroundFetchTests` runs in the `xctest` process, where `UIApplication.sharedApplication` is nil. A first version of the cancel test recorded nothing for that reason; it now swaps in a stand-in for `sharedApplication`, and skips itself if the target ever gets a host app.
    - **Red without change 1:** with change 1 removed, `test_wo_004_cancelResetsLegacyFetchInterval` fails (MEASURED).
  - **Lab, Lab1, CocoaPods, `ios-stop-start`,** on labels built from the uncommitted branch (`dirty: yes`, variant `WO-004`):
    - **Change 2 alone** (`4.1.1-WO-004-change2-dirty`, slice `e0eb8418…`):
      - `stopped`: `legacyFetchComplete result=failed` 25.997 s after the delivery, and SpringBoard `with result: Error` with no "invalid response". `pendingFetch` false afterwards.
      - `bgSubmits` (`appRefresh=2`) and `springboardResults` (`Error=1`) are still red.
      - `restarted` PASS.
      - So the Verification box for change 2 alone holds: `legacyResults failed=1`.
    - **Both changes** (`4.1.1-WO-004-dirty`, slice `b8df5e77…`): no difference.
      - `stop()` logged the cancel, so change 1's reset was queued after configure's interval.
      - SpringBoard's fetch 3.4 s later still reached the delegate. The SDK requested it again, and change 2 completed it failed at +26.0 s; SpringBoard recorded `Error`.
      - Red on the same two rows; `restarted` PASS.
  - **Why (MEASURED, a throwaway app without the SDK, on the scratch Simulator):**
    - A `simctl push` content-available fetch reached `application(_:performFetchWithCompletionHandler:)` whatever the app's minimum fetch interval was: never set, 900, 900 then Never, or Never alone. That held with the app in front and suspended: 8 of 8.
    - UIKit does not gate this delivery on the interval.
    - Completing with `.noData` at once gave SpringBoard `NoNewDataAvailable` within 1 ms.
  - **This contradicts WO-002's D3 spike note (2026-11-10).** It says that with Never (a fresh install that never set an interval), UIKit answered `NoNewDataAvailable` without calling the delegate.
    - All of that spike's `NoNewDataAvailable` results came from its second round. That round's app logged no delegate call and was rebuilt afterwards; whether it implemented the fetch delegate method then is unknown (INFERRED confound).
    - That note is cited by this work order (change 1, and the `NoNewDataAvailable` reason), by `ios-stop-start.yaml`'s description, by WO-002's Log and by the react-native `lab.yaml` note ("which UIKit needs before it delivers"). None of those citations holds.
  - **So change 1 cannot stop the fetch the lab sends.** Nor, INFERRED, Xcode's Simulate Background Fetch (NOT CHECKED).
    - What it still does is undo the interval for opportunistic legacy fetches.
    - The Simulator does not run those on its own and the lab cannot trigger them, so its effect is NOT MEASURED.
  - **The route that would turn `stopped` green is one this work order rejected:** completing a fetch that arrives with no listener at once, with `noData`.
    - It was rejected as "the whole fix" because `performFetchWithCompletionHandler` requests the fetch again first.
    - Done together with not requesting it again when no listener is left, that objection no longer applies.
    - Per the procedure, choosing it is Chris's decision, not taken here.
- 2026-11-11 — **Independent review of the uncommitted native diff** (a 30-agent workflow: four lenses, then two skeptics per finding). 15 findings, 13 after dedup: 12 upheld by both skeptics, 1 refuted by both. I checked the device-path claims against source myself; the rest are as the reviewers cite them.
  - **The device path (medium).** An app that does not permit `com.transistorsoft.fetch` sets the legacy interval at `:129-131` on iOS 13+, a device included. So the defect is not Simulator-only. Corrected above, in the Diagnosis, Scope check, D2 and the Test plan.
  - **Change 1 does not last (medium).** The next fetch delivered after `stop()` runs `performFetchWithCompletionHandler`, which always clears `fetchScheduled` and reschedules (`:182-183`). That queues configure's interval again after the Never. A second `stop()` cannot undo it: `removeListener:` returns early once the listener is gone (`:283-286`).
  - **Change 2 refinement (low).** It completes only when no subscriber is left. But a held fetch whose remaining subscribers have all finished (another subscriber removed before finishing) is still never completed, because `+onTimeout` returns YES for finished subscribers too (`TSBGAppRefreshSubscriber.m:84-96`, `:148`).
  - **A finish after stop() is dropped (low).** The subscriber is gone (`:352-356`), so change 2 holds the fetch about 26 s and reports it `Failed` although the app finished it.
  - **configure() then an immediate stop() (low, pre-existing).** configure schedules inside a later main-queue status block (`:211-216`), which can run after the cancel and re-arm the interval with no listener.
  - **Docs and tests:**
    - The CHANGELOG line overclaims ("stop() now stops the legacy background fetch as well"), contradicted by the measurement (rated high).
    - Change 1's anchor comment states the disproved premise and omits `:129-131`.
    - Test 3 counts completions without checking which one completed.
    - The change-2-alone capture was overwritten by the both-changes run (same variant path); its numbers in the Log above come from that run's console output.
  - **Refuted by both skeptics:** that Flutter's per-engine detach turning off the interval is a new regression.
  - (Line numbers in this entry converted to `5de5242` at commit time. The reviewers cited the uncommitted first implementation, whose numbering differs.)
  - **Proposed revision (awaiting Chris; not implemented):**
    - **Change 1:** keep. Its case is now the device path `:129-131` and the Simulator fallback `:121`, for opportunistic fetches (INFERRED, not measurable in the lab). Rewrite its comment.
    - **Change 2:** keep, completing when no *unfinished* subscriber took the timeout.
    - **Change 3 (new):** in `performFetchWithCompletionHandler`, when no subscriber is left, complete UIKit's handler at once with `NoData`, without rescheduling or beginning a background task.
      - That closes the re-arm. It should turn `stopped` green (`NoData` gives SpringBoard `NoNewDataAvailable`, MEASURED).
      - This takes the route "Not an acceptable shortcut" rejected as *the whole fix*. The rejection's reason, the re-request, no longer applies when the reschedule is skipped too.
    - **Optional change 4:** complete a held legacy handler at once when `removeListener:` removes the last listener, instead of at the expiry.
    - **Tests:** a change-3 test (a fetch with no subscriber completes at once, `NoData`, with no interval written afterwards), and test 3 tightened.
    - **CHANGELOG:** rewritten to what the code does.
    - **Separate work orders, if wanted:** the configure-then-stop race, and a device check of the `:129-131` path.
- 2026-11-11 — **The revision approved by Chris ("Approve revision"):** changes 1–3, change 2 tightened, recorded in the Proposed fix. The optional change 4 is not included. Implementation follows on the same native branch, still uncommitted.
- 2026-11-11 — **The approved revision implemented and measured** (native still uncommitted).
  - **Native:**
    - Change 1's comment now states the measured constraint and both arming paths.
    - Change 2 scans for an unfinished subscriber before `+onTimeout`.
    - Change 3 returns early from `performFetchWithCompletionHandler` when `[[TSBGAppRefreshSubscriber subscribers] count] == 0`.
    - The CHANGELOG line was rewritten to what the code does.
  - **XCTest:** 45 of 45 pass (39 existing, 6 `test_wo_004_…`). Four of the new tests were run red with the change they cover removed (MEASURED; corrected at commit time, this said each new test was; a fifth was run red at commit, see the last entry):
    - without change 1: the cancel test and the fetch-after-cancel test;
    - without change 3: the at-once test and the fetch-after-cancel test;
    - with change 2's old condition (`![onTimeout]`): the only-finished-subscribers test.
    - The fixed file's sha was checked after each restore.
  - **Lab,** label `4.1.1-WO-004-r2-dirty` (slice `8e97cb78…`, built from the uncommitted branch, `dirty: yes`):
    - **`ios-stop-start` on Lab1, CocoaPods: both steps PASS.**
      - In `stopped`, the push 3.4 s after `stop()` reached the delegate. The SDK logged `no subscriber: completed with no data` and completed it in the same millisecond (`legacyFetchComplete result=noData`).
      - SpringBoard recorded `with result: NoNewDataAvailable` 3 ms after `Executing`, and `bgSubmits` was configure's alone.
    - **All seven react-native iOS cells PASS on both channels:** sweeps `20261111-134847` (`WO-004`, CocoaPods) and `20261111-135517` (`WO-004-SPM`, SwiftPM). That includes `ios-fetch-sim`'s `cold` step: a still-configured app's subscribers are reloaded at `+load`, so change 3 does not answer its fetch.
    - **The first SwiftPM sweep could not build** (a second label at one mirror path). That was a lab defect, fixed in `build.sh` and logged in WO-002 the same day.
- 2026-11-11 — **Second independent review of the revised diff** (a 24-agent workflow, the same four lenses re-aimed, then two skeptics per finding).
  - **Results:** 12 findings, 10 after dedup, all low; all 10 upheld by both skeptics, none refuted.
  - **On the first review's 12:**
    - 8 addressed.
    - 2 out of scope by Chris's decision: the configure-then-stop race, and change 4.
    - 2 still open, both fixed now: the CHANGELOG tying the change to `stop()`, and capture overwrites.
  - **Fixed in code:**
    - **The double-completion race change 2 introduced** (the expiry on main against a subscriber's finish on React Native's module queue): both now take the handler once, under one lock (Proposed fix, change 2). INFERRED safe by construction; not reproduced, since the window is sub-millisecond.
    - **A test-order dependence:** a cancel's reset left queued by an earlier test landed in `test_wo_004_fetchWithoutSubscriberCompletesAtOnceWithNoData`'s stand-in. `installStandInApplication` now runs the main queue empty first.
      - MEASURED with the reviewer's example: `testMultiSubscriberFinishCounting` then that test fails without the drain and passes with it.
  - **Fixed in words:**
    - The CHANGELOG now says "while no fetch listener is registered (each plugin's `stop()` removes its listener)". The native `stop:` and the Swift `stop()` don't remove it.
    - Change 3's comment gives the real reason: no listener had opted in. It is not that nothing could finish the fetch.
    - Change 2's comment cites the BGTaskScheduler expiry only for the no-subscriber case.
    - In this document: the Flutter teardown that never runs, the trade-off change 3 makes before a launch's `configure()`, and the Test plan's disproved line.
  - **Out of scope, recorded above:** the configure-then-stop race, the dropped finish after `stop()`, the BGTaskScheduler expiry's finished-subscriber gap, the native and Swift `stop()` and the docforge Swift examples, Flutter's teardown selector, and a second legacy fetch overwriting a held handler. Offered to Chris as separate work orders.
  - **Captures (the review's point, stated plainly):** every measurement here ran as variant `WO-004`, and captures are keyed by variant. Each run overwrote the last:
    - the change-2-alone capture by the both-changes run;
    - the both-changes capture by the revision's run.
    - The numbers quoted for those two runs come from their console output, which is not kept in the repo.
    - The captures committed with this work order will be the final sweeps', on a label built from the fix commit.
  - **XCTest after these fixes:** 45 of 45; still the file's two pre-existing deprecation warnings, at the reset and at `setMinimumFetchInterval`, the same two as before WO-004.
  - **Lab after these fixes** (recorded at commit time; it was missing here): label `4.1.1-WO-004-r3-dirty` (slice `81b54bae…`, from the uncommitted branch, `dirty: yes`).
    - All seven react-native iOS cells PASS on both channels, `ios-stop-start` included (on Lab1).
    - Sweeps `20261111-142356` (`WO-004`, CocoaPods) and `20261111-142842` (`WO-004-SPM`, SwiftPM).
- 2026-11-11 — **Committed, and proved on the fix commit.** Chris approved ("approved") the order shown with the diffs: the native commit, a lab label built from it, the final sweeps, then the lab and work-order commits. Status `verify`: the device check is outstanding.
  - **Native:** `0670713` on `fix/WO-004-ios-stop-legacy-fetch`, from `478767b`. It holds `TSBackgroundFetch.m`, `TSBackgroundFetchTests.m` and `CHANGELOG-iOS.md`, the diff reviewed above. Not pushed or merged; that is Chris's decision.
  - **XCTest at `0670713`:** 45 of 45, from a fresh build on the scratch Simulator `WO004-XCTest` (iOS 27.2).
    - No new warnings. The build's four are all pre-existing: the file's two deprecations, and the test target's two linker warnings (XCTest built for a newer iOS than the target's 15.0).
    - **The red run that was missing (MEASURED):** with change 2 removed in a scratch worktree at `0670713`, `…AfterLastSubscriberRemovedCompletesHandler` and `…OnlyFinishedSubscribersCompletesHandler` fail, and the other 43 pass.
    - So five of the six `test_wo_004_…` have been shown red without their change. The sixth, `…LeavesHandlerToSubscriberTimeout`, is a guard: it passes without change 2 by design.
    - The native commit message says each new test fails without its change. It is corrected here rather than by amending the commit, which the label, the variants and the captures name.
  - **The label:** `4.1.1-WO-004-0670713`, published by `publish_native_fetch_ios.sh` from a detached worktree at `0670713` (`labs/checkouts/native-06707138c`, `dirty: no`).
    - Its Simulator slice's sha256, `81b54bae…`, is `4.1.1-WO-004-r3-dirty`'s, the build after the second review's fixes (the previous entry). So the commit builds the binary those sweeps measured.
    - The lab entries before that measured older code, without the lock: `change2-dirty`, `dirty` and `r2-dirty`.
    - The variants `WO-004` and `WO-004-SPM` now pin it (`native_source.ios` `0670713`).
  - **The final sweeps:** all seven react-native iOS cells PASS on each channel, `20261111-164604` (`WO-004`, CocoaPods) and `20261111-165050` (`WO-004-SPM`, SwiftPM).
  - **`ios-stop-start`, four runs, all PASS:** the sweeps ran it on Lab1, and one `./labkit run --build` per channel ran it on Lab2.
    - `stopped`: SpringBoard's fetch, 3.2 to 3.4 s after `stop()`, reached the delegate. The SDK logged `no subscriber: completed with no data` and completed it 2 to 3 ms after SpringBoard's `Executing` (`legacyFetchComplete result=noData`). SpringBoard recorded `NoNewDataAvailable` 5 to 9 ms after `Executing`. `bgSubmits` was configure's alone, and the driver got nothing.
    - `restarted`: the fetch reached configure()'s callback and the driver finished it. SpringBoard recorded `RetrievedNewData`, and `bgSubmits` was 3.
  - **Captures:** the committed ones are the two sweeps'. That is `run.json` and `summary.txt` under `labs/captures/react-native-ios/*/{WO-004,WO-004-SPM}/`; the rest is gitignored. Every `run.json` reads native `4.1.1-WO-004-0670713` at `0670713`, `dirty: no`.
    - The Lab2 runs overwrote `ios-stop-start`'s captures. The sweeps' were copied back from a copy taken before them and checked identical (`diff -r`).
    - The Lab2 captures are kept outside the repo; the numbers above come from them and from the sweeps'.
    - The measurement phases' captures were never committed; these replace them.
  - **The parent's `native/background-fetch` pointer is not bumped.** It records `fc74063`, and the lab pins native through its variants. A pointer at an unpushed commit would break `git submodule update` for anyone else.
  - **Still open:**
    - the device check (Verification);
    - pushing and merging the native branch, and a release with it;
    - `FIX` and `FIX-SPM` pin `5de5242`, so `ios-stop-start` stays red there until that pin moves.
  - **`0670713` exists only in this worktree's own submodule repo** (`.git/worktrees/npm-github-actions-setup-771f26/modules/native/background-fetch`) until the native branch is pushed. Found by the commit-time review.
    - Until then `./labkit setup` cannot rebuild the WO-004 label anywhere else. Removing this worktree would lose the commit.
    - So the native branch should be pushed, or at least fetched into the main checkout's submodule, before this parent branch is merged.
  - **The commit-time review** (a 50-agent workflow: four read-only reviewers for `build.sh`, the claims, the citations and the commit set, then two skeptics per finding): 23 findings, 18 upheld by both skeptics, 5 refuted by both. All 18 are addressed in these commits:
    - line numbers taken from uncommitted trees, now `5de5242`'s;
    - the `r3` sweeps, missing from this Log;
    - the red-test count, with the missing red run done;
    - the warnings;
    - stale or contradicted sentences in the Proposed fix and the Scope check;
    - the commit's reachability, above;
    - `build.sh`'s mirror claim. That one is reworded, and its limit (a label re-published with other content) is logged in WO-002. The code is unchanged.
- 2026-11-11 — **Pushed, and merged into fetch-forge.** Chris approved ("Push and merge"). Status stays `verify`: the device check is outstanding.
  - **Native:** `fix/WO-004-ios-stop-legacy-fetch` pushed to origin (`transistorsoft/transistor-background-fetch`) at `0670713`, the only commit it added; its parent `478767b` is native `master`. Not merged into native `master`, and not released.
  - **fetch-forge:** this branch merged into `master` as `4464e55` (`--no-ff`) and pushed. `./labkit selftest` on the merge, in the main checkout: fetch (182 checks), slots and vendor_check PASS.
  - **`0670713` is reachable now (MEASURED):** a fresh repo fetched it from origin by sha (`git fetch --depth=1 origin 0670713…`). That is how `lab_setup.native_checkout` gets a commit the submodule lacks (PROVEN, `labs/harness/lab_setup.py`). So the previous entry's note on this worktree's submodule repo no longer applies. A `./labkit setup` that rebuilds the label in another checkout has not been run (NOT CHECKED).
  - The parent's `native/background-fetch` pointer still records `fc74063`; the lab pins native through its variants.
  - **Still open:**
    - the device check (Verification);
    - merging the native branch into native `master`, and a release with it;
    - `FIX` and `FIX-SPM` pin `5de5242`, so `ios-stop-start` stays red there until that pin moves.
- 2026-11-12 — **Device check passed; the fix released as TSBackgroundFetch 4.2.1; `FIX` and `FIX-SPM` green. Status `done`.** Chris chose, for the remaining items: "Run it on my phone", "Merge, release 4.2.1, Trunk", "No Catalyst; defer D4", "The 4.2.1 commit, iOS sweeps"; then "Go ahead, phone is ready".
  - **The device check (MEASURED),** on iPhone 17 Pro (Chris), iOS 26.7.1, over Wi-Fi:
    - **Builds:** TSBackgroundFetch archived for iphoneos from `labs/checkouts/native-06707138c` (`0670713`, clean), with code signing off and an ad-hoc signature after, like the lab's Simulator slice. That was the fix's first device compile, with no new warnings. FetchLab, Debug, was built against it through `LAB_TSBF` and signed with Apple Development for team `32A636YFGZ`, using the existing wildcard team profile (no account change). Its `FetchLab.debug.dylib` carries `_expireLegacyFetch` and the no-subscriber line. Check (a)'s build is the same app with `BGTaskSchedulerPermittedIdentifiers` deleted from its Info.plist, re-signed. Nothing tracked changed; the next sweep re-installed the lab app's gitignored Pods.
    - **Driver:** `devicectl` launched the app suspended, with the lab's launch arguments (`status`; `stop()` at `at_s` 30; `start()` at 90). LLDB attached through CoreDevice and set breakpoints that log and continue: `submitTaskRequest:error:`, `cancelTaskRequestWithIdentifier:`, `setMinimumBackgroundFetchInterval:`, and TSBackgroundFetch's handler, `stop:`, `start:`, listener and `finish:` methods. Timed actions ran on the main thread. The driver was rehearsed first on a scratch Simulator, since deleted.
    - **(b) The committed plist, BGTaskScheduler's path: PASS.**
      - configure() submitted the request.
      - A simulated launch (`_simulateLaunchForTaskWithIdentifier:`) at +17 s ran `handleBGAppRefreshTask`, which rescheduled. React Native's listener finished it 1 s later, and BackgroundTasks logged "Marking simulated task complete".
      - `stop()` at +38 s removed the listener, cancelled the request, and called `setMinimumBackgroundFetchInterval:` with Never (a call Apple documents as disabled while the key is present).
      - The launch at +60 s ran nothing; BackgroundTasks logged "No task request with identifier com.transistorsoft.fetch has been scheduled".
      - `start()` at +98 s added the listener and submitted again. The launch at +120 s was delivered and finished.
    - **(a) No key, the legacy path: PASS.**
      - Nothing was registered or submitted; configure() set the interval to 900.
      - `stop()` at +38.6 s removed the listener, called `cancelTaskRequestWithIdentifier:` (no request was pending), and set the interval to Never (change 1).
      - With no subscriber left, a legacy fetch at +60 s was completed inside the call with `1` (NoData). The fetch was the app delegate's `application:performFetchWithCompletionHandler:`, called from LLDB. The SDK logged `no subscriber: completed with no data`, and nothing set the interval again (change 3).
      - `start()` at +98.7 s set 900 again.
    - **Limits:**
      - Neither Xcode 27.2's debugger nor `devicectl` can make iOS deliver a legacy fetch to a device: the debugger has no Simulate Background Fetch, and `devicectl` has no such command. Xcode 27.2's Instruments still has one (its Background Fetch command, `simulateBackgroundFetchForPID:` over the device's process-control service); it was not tried (NOT CHECKED on iOS 26.7.1). So (a)'s fetch was called in-process, and whether iOS stops scheduling fetches after Never stays INFERRED.
      - (b)'s launches were simulated by BackgroundTasks itself.
      - The 5de5242 baseline was not run on the device.
    - **The `:129-131` case on a device** (a correction to the Diagnosis's and the CHANGELOG's wording): Apple documents that a `BGTaskSchedulerPermittedIdentifiers` key disables `application:performFetchWithCompletionHandler:` and `setMinimumBackgroundFetchInterval:` on iOS 13+. So on a device the legacy path is live only for an app with no key. An app whose key lacks `com.transistorsoft.fetch` takes the SDK's legacy branch, but UIKit ignores it (INFERRED). The CHANGELOG entry shipped in 4.2.1 as written.
    - **Evidence:** the LLDB transcripts, and FetchLab's lines from the phone's log.
      - `log collect` from a device needs root (Chris authorized `sudo` in his terminal) and ignores `--predicate`. So the whole log from 13:29Z was collected; FetchLab's 1,469 log entries were kept and the archive deleted.
      - The lab's own `LAB` lines are `<private>` in a device log.
      - None of this is in the repo. FetchLab was uninstalled afterwards.
  - **Native merge,** in the main checkout's submodule, following 4.2.0's convention: `release-4.2.1` was cut from `master`. `b6c6717` merges the fix branch into it, and `0459259` merges it into `master`, whose tree is `0670713`'s. Pushed `478767b..0459259`; `release-4.2.1` stays local.
  - **TSBackgroundFetch 4.2.1,** published by Chris (`./forge publish native ios --version 4.2.1 --no-cocoapods`, confirmed at its prompt):
    - Commits `7d277f3` (4.2.1, 4201), `c07623b` (the CHANGELOG heading) and `399765c` (url and checksum).
    - Tags `ios/4.2.1` and `4.2.1` on `399765c`, and the GitHub Release, Latest, at 13:51Z.
    - A fresh download matches Package.swift's checksum `93e8b3ac…`, and both slices read 4.2.1 (4201) and carry the fix.
    - `master` was pushed by hand (`0459259..399765c`): the script pushes only the tags.
    - No Catalyst slice (WO-001's D4, deferred again).
  - **CocoaPods Trunk,** pushed by hand from the tag. Lint passed.
    - Pushes at 13:55:58Z and at 14:01:52Z each printed "Calling the GitHub commit API timed out", as for 4.2.0 (WO-001). The Specs path was checked before the second push.
    - 4.2.1 was in `CocoaPods/Specs` and in Trunk's API (created 13:55:59 UTC) at 14:02:31Z. Its source is the release zip, iOS 15.0. The CDN index did not list it at 14:09:51Z and did at 14:30:22Z.
  - **`FIX` and `FIX-SPM` moved to the release commit,** label `4.1.1-master-399765c`.
    - android/ is unchanged since `5de5242` and the AAR is byte-identical (`74b20be4…`), so Android was not re-swept.
    - React Native iOS: sweeps `20261112-085802` (`FIX`) and `20261112-090249` (`FIX-SPM`), 7 of 7 each. `ios-stop-start`'s `stopped` went from FAIL to PASS on both: `bgSubmits` appRefresh 2 to 1, and `springboardResults` Error to NoNewDataAvailable.
    - The red captures stay in git at `3f74ec3`.
  - **Done:** every Verification box is ticked, and the previous entry's Still open items are closed.
  - **Left as they are:** the variants `WO-004` and `WO-004-SPM` (runnable evidence); the gitignored WO-004 labels and `labs/checkouts`; the local `release-4.2.1` branch.
  - **Follow-ups, not done:**
    - CLAUDE.md, `forge help` and `publish-ios.sh`'s header call `--push-cocoapods` opt-in, but the script pushes Trunk unless given `--no-cocoapods`; and `--create-branch` builds the old version.
    - TSLocationManager's use of the listener API, before its range widens to 4.2.
    - This work order's Out of scope items.
