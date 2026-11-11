---
id: WO-004
title: Where BGTaskScheduler is unavailable (the iOS Simulator), stop() leaves UIKit's legacy background fetch armed, and a fetch after it is requested again and its completion handler never called
status: draft
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
> Source citations are native `5de5242` (the lab's pin); `TSBackgroundFetch.m` is unchanged at native `HEAD` (`fc74063`). The bridges' are their checked-out commits: React Native `85a6bf4`, Cordova `38ad0fe`, Capacitor `9dd204a`, Flutter `11d53b8`.

## Diagnosis

**MEASURED** (`labs/captures/react-native-ios/ios-stop-start/{FIX,FIX-SPM}/stopped`, sweeps `20261111-122633` and `20261111-122826`, Lab1, iPhone 17 Pro Simulator, iOS 27.2; native `4.1.1-master-5de5242` (iOS 4.2.0), react-native plugin `85a6bf4`; CocoaPods and SwiftPM alike).

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
- **Anywhere else BGTaskScheduler answers Unavailable to a configured app:** NOT CHECKED (an iOS app on a Mac, for one). configure() only schedules when `backgroundRefreshStatus` is Available (`:208-215`), so a user who turned Background App Refresh off does not reach the fallback.
- **Which apps hand the legacy fetch to the SDK at all** (PROVEN, source): Cordova's plugin (`CDVBackgroundFetch.m:23-27`) and Flutter's (`BackgroundFetchPlugin.m:29-32`) forward UIKit's `performFetchWithCompletionHandler:`. React Native's category that did (`RNBackgroundFetch+AppDelegate.m`) is compiled by neither its podspec nor its `Package.swift`, so only an app that forwards it itself, like the lab host, reaches the SDK. Capacitor's plugin has no forward (NOT CHECKED whether Capacitor's app template adds one). Where nothing forwards it, the fetch never reaches the SDK, but the interval still stays set after `stop()`.

## Proposed fix

In native `TSBackgroundFetch.m`, two changes:

1. **`cancelBGAppRefresh`: also set UIKit's minimum background fetch interval back to Never on iOS 13+**, on the main queue, as the pre-iOS 13 branch already does (`:146-149`). After that, `stop()` leaves nothing armed, and UIKit answers a SpringBoard fetch itself (`NoNewDataAvailable`, without calling the app; MEASURED 2026-11-10 for an app whose interval is Never, WO-002 D3 spike). `start()` re-arms it through `scheduleBGAppRefresh`'s fallback, as today. Both writes are dispatched to the main queue, so `stop()` then `start()` lands in that order (INFERRED).
2. **The legacy expiration block: complete UIKit's handler when no listener handled the timeout.** When `onTimeout` returns `NO` and the completion handler is still held, call it (with `UIBackgroundFetchResultFailed`, per D3) and clear it, mirroring the BGTaskScheduler path's `setTaskCompletedWithSuccess:NO` (`:168`). After change 1, this is reached only by a legacy fetch already in flight when its last listener is removed.

The anchor `// (WO-004)` goes at both changes, and the native CHANGELOG entry carries `(WO-004)`.

**DECISION D1: change 2 in this work order or not.**
- **Recommended: include it.** It is the other half of the measured symptom (the handler never called), it is a few lines, and it makes the two expiration paths agree.
- **Alternative: change 1 only.** It turns `ios-stop-start` green on its own (INFERRED), but leaves the in-flight case: a fetch held when the last listener goes away is still never completed.

**DECISION D2: reset the interval always, or only when the fallback set it.**
- **Recommended: always** (on iOS 13+, in `cancelBGAppRefresh`). On a device the interval was never set, so setting Never changes nothing (INFERRED), and the code stays a mirror of the pre-iOS 13 branch.
- **Alternative: a flag** set where the fallback arms the interval (`:121`), checked in `cancelBGAppRefresh`. More state for no behaviour difference that we know of.

**DECISION D3: the result change 2 reports.**
- **Recommended: `UIBackgroundFetchResultFailed`**, the legacy counterpart of `setTaskCompletedWithSuccess:NO`.
- **Alternative: `UIBackgroundFetchResultNoData`.** Kinder to UIKit's scheduling, but it says the fetch ran and found nothing, which it did not.

## Scope check

- **`cancelBGAppRefresh`** has one caller, `removeListener:` (`:288-290`), which runs it once the last listener is gone. `removeListener:` is called by every bridge's `stop()` with no taskId (cited above) and on plugin teardown: Cordova's `dealloc` (`CDVBackgroundFetch.m:194`), Capacitor's `deinit` (`BackgroundFetchPlugin.swift:38`), Flutter's `detachFromEngineForRegistration:` (`BackgroundFetchPlugin.m:70`). On each, change 1 also stops the legacy fetch, matching what the cancel already does to the BGTaskScheduler request.
- **`setMinimumFetchInterval`** is called from `scheduleBGAppRefresh`'s fallback (`:121`) and its pre-iOS 13 branches (`:130`, `:133`). Untouched. `start()`, configure() and each fetch's reschedule still re-arm through it.
- **The legacy expiration block** (`:188-196`) runs only for fetches handed to `performFetchWithCompletionHandler:`. With a listener that handles the timeout, `onTimeout` returns `YES` and change 2 does nothing: `ios-timeout-sim`'s path (the plugin's onTimeout, then the driver's finish) is unchanged.
- **The BGTaskScheduler path** (`handleBGAppRefreshTask:`, `:161-176`) and custom tasks (`TSBGTask`) are untouched.
- **The native Swift API** (`SwiftInterface/BackgroundFetch.swift`) wraps the same methods; whether its own stop path removes the listener is NOT CHECKED.

## Platform parity

- **iOS:** affected where BGTaskScheduler is unavailable (MEASURED on the Simulator, on both distribution channels; device not affected, INFERRED, as above). The fix is in native `TSBackgroundFetch.m` and reaches every bridge through the native library. Whether each bridge picks up a native patch release without a change: React Native's podspec requires `~> 4.2` (WO-002 Log, 2026-11-10); the others' iOS requirements are NOT CHECKED here.
- **Android:** N/A. There is no legacy interval to leave armed. `stop()` with no taskId finishes any running task, cancels every job and clears the fetch callback (`BackgroundFetch.java:147-160`), and the app holds no job afterwards (MEASURED by `api-smoke`, `jobIds -`). Android's own stop/start question is WO-002's suspected defect 2 (`stop-start`), a different defect.

## Test plan

- **The lab (the proof).** A variant pinning native at the fix branch's commit (e.g. `labs/harness/variants/WO-004.yaml`, its iOS label built by `publish_native_fetch_ios.sh` from that commit), on both channels:
  - **Change 2 alone first** (if D1 keeps it): `ios-stop-start`'s `stopped` reads `legacyResults failed=1` at the expiry (`LAB point=legacyFetchComplete result=failed`): the handler is completed.
  - **Both changes:** `stopped` green. No delivery, `bgSubmits` configure's alone, SpringBoard `NoNewDataAvailable`. INFERRED: UIKit no longer calls the delegate, so `legacyFetchStates` reads `-`.
  - `restarted` stays green: `start()` re-arms and the fetch is delivered.
  - No regression: `ios-fetch-sim`, `ios-timeout-sim`, `ios-task-sim`, `ios-registration`, `api-smoke-ios` and `api-constants` stay green on the fix label.
- **XCTest** (`TSBackgroundFetchTests.m`, 39 tests): all pass. A new `test_wo_004_…` for change 2 needs the expiration block reachable from a test (it is an inline block today), for instance factored into a method the test can call; INFERRED, not designed further.
- **On-device:** not needed for the defect, which a device does not reach (INFERRED). A device check that `stop()`/`start()` still behave on BGTaskScheduler's path belongs to the manual tier (`ios-device-bgtask`), not yet designed.

## Out of scope

- **WO-002's suspected defect 11:** a timed-out fetch reported to the OS as a success (`TSBackgroundFetch.m:368-377`). Its own work order if confirmed.
- **The SDK's `stop:nil` not cancelling the fetch request by itself.** The cancel lives in `removeListener:`, and every bridge calls both. Whether a native consumer calling `stop:nil` alone should cancel is an API question, NOT CHECKED, and its own work order if wanted.
- **Any change to the BGTaskScheduler path, custom tasks, or Android.**
- **Removing or redesigning the legacy fallback.**

## Not an acceptable shortcut

- **Completing a listener-less legacy fetch at once with `NoData`, as the whole fix.** It ends SpringBoard's `Error`, but `performFetchWithCompletionHandler` still requests the fetch again first (`:182-183`), so `stop()` stays undone and the app keeps being fetched. `ios-stop-start`'s `bgSubmits` stays red.
- **Resetting the interval in each bridge's `stop()`.** The cancel is the SDK's. Four bridges and the Swift API would each need it, and a native consumer's `removeListener:` would still leave it armed.
- **Dropping the legacy fallback**, so that configure() fails on the Simulator. It is what makes fetch testable on the Simulator for every customer; removing it is a product decision, not this fix.
- **Changing the lab to avoid the symptom:** the lab AppDelegate not forwarding legacy fetches after `stop()`, or `ios-stop-start`'s `stopped` expectations moved to today's behaviour. The lab measures the shipped SDK and must not compensate for it.
- **Dismissing it as Simulator-only.** The Simulator is where customers develop and test fetch, and a `stop()` that does not stop, with fetches ending in `Error`, is what they would report.

## Verification

- [ ] D1–D3 decided
- [ ] `ios-stop-start` `stopped` green, `restarted` still green, on a label built from the fix commit, CocoaPods and SwiftPM (Lab1 and Lab2)
- [ ] If D1 keeps change 2: `stopped` with change 2 alone reads `legacyResults failed=1`
- [ ] The other react-native iOS cells green on the fix label
- [ ] `TSBackgroundFetchTests` pass, including `test_wo_004_…`
- [ ] Native CHANGELOG entry carries `(WO-004)`
- [ ] WO-002's suspected defect 12 points at this work order's result

## Log

- 2026-11-11 — created from WO-002's suspected defect 12, measured red by `ios-stop-start` on both channels (WO-002 Log, 2026-11-11). Draft, awaiting Chris's approval and D1–D3.
