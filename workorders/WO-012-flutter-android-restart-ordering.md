---
id: WO-012
title: On Flutter Android, when an app restarts itself the old Activity detaches after the new one has attached, and that detach makes TSBackgroundFetch headless with the new screen up and removes the new engine's fetch event channel
status: in-progress
created: 2026-11-16
platforms: [android]
submodules: [flutter/background_fetch]
branch: fix/WO-012-flutter-android-restart-ordering
issues: []
supersedes: []
---

> **Found by** WO-002's lab board. `restart-foreground-android` is WO-007's lab guard. It was PREDICTED red on Flutter from source, then MEASURED red on both steps: first in sweep `20261113-175923`, then in `20261113-183048`, whose captures are committed (WO-002 Log, 2026-11-13: the sweeps at `WO-002-background-fetch-labkit.md:1202`, `:1204`; the red, predicted and measured, at `:1207-1209`; the open item at `:1222`, which WO-002's 2026-11-16 entry assigns to this order). React Native and Capacitor pass the same steps in sweep `20261113-104449`.
>
> **Evidence markers:** **MEASURED** = seen in a lab capture; **PROVEN** = read in source; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at; **SUSPECTED** = a defect inferred from source that no device run reproduced. The restart red is MEASURED. The event-channel half (Diagnosis) is PROVEN in source, and its effect is SUSPECTED: no capture shows it.
>
> **Citation bases** (source read 2026-11-16):
> - **Bridges,** at the commits the lab measured (the checked-out heads): Flutter `11d53b8` (1.7.0 plus Unreleased), React Native `8d9ab19`, Capacitor `3fa169a` (the lab measured `4d431c1`; their `android/` is identical: `git diff 4d431c1 3fa169a -- android` is empty), Cordova `368eb85`; types `dc4f5b8`.
>   - Flutter paths are relative to `flutter/background_fetch/`.
>   - Its Android classes are in `android/src/main/java/com/transistorsoft/flutter/backgroundfetch/`.
> - **Native:** TSBackgroundFetch 4.2.1 = `399765c` (lab label `4.1.1-master-399765c`), read with `git -C native/background-fetch show 399765c:<path>`. Each native file is cited in full once, then by name with `(399765c)`.
> - **Flutter's embedding and framework:** the Flutter 3.47.5 SDK checkout on this Mac (`6a19cca`).
>   - Java is under `engine/src/flutter/shell/platform/android/io/flutter/`, and Dart under `packages/flutter/lib/src/services/`.
>   - That the lab APK's prebuilt embedding was built from this source is INFERRED (same release).
> - **React Native's framework:** react-native 0.88.0, in `labs/platforms/react-native/app/node_modules/react-native/`.
> - **Captures:** variant `FIX`, Android 16 / API 36. Flutter's restart cells ran on Lab2 (`emulator-5582`), plugin `11d53b8`, clean.
>   - Captures carry no sweep id (`labs/harness/sweep.py:33`). I matched each cited capture to its sweep by its `run.json` `timings.t0`, against that sweep's `board.json` in `labs/harness/.claims/` (gitignored).
>   - `run.json` and `summary.txt` are tracked. `logcat.txt` is on disk only (`labs/.gitignore:6`); its line numbers are cited.
>   - **Since replaced, twice:** WO-011's sweep `20261116-135555` (committed in `62a813a`, still red on both restart steps), then this order's sweep and runs (Log). So the Diagnosis' logcat line numbers can no longer be re-read; `20261113-183048`'s `run.json` and `summary.txt` are at `6b8fcbc`.
>   - **Lab files** (scenarios, the Flutter host) are cited at fetch-forge `18e406a`, where this order was drafted: later edits move some of their lines.

## Diagnosis

**MEASURED: `restart-foreground-android`, step `run`.** Config: `stopOnTerminate: true`, `enableHeadless: false`. Source: `labs/captures/flutter/restart-foreground-android/FIX/run/`, sweep `20261113-183048`.
- **The trigger:** the host answers `finish` with `finish()`, then `startActivity()` of its own launch intent (`labs/platforms/flutter/app/android/app/src/main/kotlin/com/transistorsoft/fetchlab/flutter/MainActivity.kt:49-63`).
- **The job** runs 4 s after the old Activity's `point=onDestroy` (`labs/harness/scenarios/restart-foreground-android.yaml:80`).

From `logcat.txt`:

| Time | Line |
|---|---|
| 32.088 | `point=finishNow activity=MainActivity@d57d03c restart=true` (`:407`) |
| 32.098 | `point=onCreate activity=MainActivity@f579c0 … creates=2` (`:424`). Its FlutterActivity builds a second engine and attaches it, so the new plugin instance's `onAttachedToActivity` runs before `onCreate:afterSuper` at 32.176 (`:451`). The attach is not logged; its place inside `onCreate` is PROVEN (`FlutterActivity.java:644-645` → delegate `:202-214`; the host logs before and after `super.onCreate`, `MainActivity.kt:37-40`) |
| 32.667–32.705 | The new isolate boots (`app=boot n=2`, `:460`) and configures: `- configure`, `Re-configured existing task`, `- cancel …jobId=999` (`:467-469`) |
| 32.730 | `point=onDestroy activity=MainActivity@d57d03c isChangingConfigurations=false isFinishing=true headless=false … current=MainActivity@f579c0` (`:539`). The new Activity has already resumed: `current=` is the Activity the host's `onResume` recorded last (`MainActivity.kt:43-46`) |
| 32.731 | `HeadlessMode? true` (`:542`) |
| 32.741 | `point=onDestroy:afterSuper … headless=true … current=MainActivity@f579c0` (`:543`) |
| 34.178 | The new Activity's `point=resumed+2s … headless=true` (`:564`) |
| 37.014 | `- Background Fetch event received`, `- Stopping on terminate`, `- stop: flutter_background_fetch`, `- jobFinished`, `- cancel taskId=flutter_background_fetch, jobId=999` (`:576-580`) |

- **Never logged after the trigger:** any LifecycleManager `onStart` or `onResume` line. Every tick of the new isolate reads `headless=true`: before the fetch with `tasks=flutter_background_fetch` (`:573`), after it with `tasks=-` (`:804` to `:945`).
- **Score (`summary.txt`):** FAIL.
  - **configure()'s callback never ran:** `driverEvents -` (want `fetch=1`), `driverTally -`, `eventAssertions configure@0=never-fired`.
  - **The job is gone:** `stoppingOnTerminate 1` (want 0), `jobIds -` (want 999).
  - **`fetchFinished 0`** (want 1). `stop()` finishes the BGTask without `finish()`'s `- finish:` line, so `jobFinished` reads 1 (`native/background-fetch/android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/BackgroundFetch.java:162-165`, `:196` (399765c)).
  - **`headless true`** (want false).
  - **Green:** `restartOrder resumed-first`, `appProcesses 1`, `crashes 0`, `fetchReceived 1`. Also `jobsCancelled 999`, Flutter's `platform_expect`: the new engine's `configure()` reschedules.
  - **Recorded:** `lifecycleTally headless:true=1,onCreate=1,onResume=1,onStart=1`.

**MEASURED: step `stop-on-terminate-false`.** Config: `stopOnTerminate: false`, `enableHeadless: true`. Same sweep. Source: `…/stop-on-terminate-false/logcat.txt`.
- **The same order:**
  - the new Activity's `onCreate` at 14.827 (`:309`);
  - its isolate's `- configure` at 15.406 (`:352`);
  - the old `point=onDestroy … current=MainActivity@a0ffa7` at 15.431 (`:420`);
  - `HeadlessMode? true` at 15.432 (`:428`).
- **The fetch at 19.686 goes headless with the UI on screen:**
  - `[HeadlessTask flutter_background_fetch]` (`:486`) and `$ initialized` (`:490`): a second FlutterEngine starts in the live process;
  - the lab's Dart headless task logs `point=headless event=fetch`, then `point=task-probe … current=MainActivity@a0ffa7` (`:491-492`);
  - then `- finish:` and `- jobFinished` (`:497-498`).
- **Score:** FAIL.
  - Red: `driverEvents -`, `headlessEvents.fetch 1` (want 0), `headless true`, `driverTally -`, `eventAssertions configure@0=never-fired`.
  - Green: `fetchFinished 1` and `jobIds 999`. The headless task finished the fetch.

**React Native and Capacitor pass both steps** (sweep `20261113-104449`; React Native `8d9ab19` on Lab1, Capacitor `4d431c1` on Lab2). In each `run`:
- the old `point=onDestroy` names the new Activity as current (React Native `run/logcat.txt:520`; Capacitor `:901`);
- the new Activity's `resumed+2s` reads `headless=false` (`:538`; `:936`);
- the fetch reaches the page as `app=event=fetch` (`:552`; `:1008`);
- neither logs `HeadlessMode? true`.

**The code path (PROVEN, source):**
- **Each FlutterActivity has its own engine and its own plugin instance. The module is one per process.**
  - FlutterActivity destroys an engine it created along with itself (`embedding/android/FlutterActivity.java:1081-1092`).
  - Every engine builds its own plugin instances: it runs the generated registrant (`embedding/engine/FlutterEngine.java:426-428`), which adds a `new BackgroundFetchPlugin()` (the lab app's generated, untracked `labs/platforms/flutter/app/android/app/src/main/java/io/flutter/plugins/GeneratedPluginRegistrant.java:19`).
  - Each `BackgroundFetchPlugin` forwards to `BackgroundFetchModule.getInstance()`, a process singleton (`BackgroundFetchModule.java:28`, `:55-65`; `BackgroundFetchPlugin.java:20`, `:25`, `:31`, `:49`).
- **The embedding tells each engine's plugins about that engine's Activity only.**
  - **Attach:** `FlutterActivity.onCreate` → `delegate.onAttach` → `attachToActivity` → `onAttachedToActivity`. Cited: `FlutterActivity.java:645`; `embedding/android/FlutterActivityAndFragmentDelegate.java:202-214`; `embedding/engine/FlutterEngineConnectionRegistry.java:366-372`.
  - **Detach:** `FlutterActivity.onDestroy` → `delegate.onDetach`. A config change calls `detachFromActivityForConfigChanges`; anything else calls `detachFromActivity` → `onDetachedFromActivity`. Then the engine is destroyed. Cited: `FlutterActivity.java:913-921`; `FlutterActivityAndFragmentDelegate.java:784-792`, `:810-811`; `FlutterEngineConnectionRegistry.java:396-408`.
  - Nothing on that path asks which Activity is current.
- **The plugin acts on every real detach.** `onDetachedFromActivity` calls `setActivity(null, null)` (`BackgroundFetchPlugin.java:47-50`). That:
  - calls `LifecycleManager.getInstance().setHeadless(true)` (`BackgroundFetchModule.java:109`);
  - removes the handler of whatever channel `mEventChannelTask` holds, and nulls the field (`:110-113`).
- **On a restart, the new instance's attach comes first.** MEASURED order of the Activity callbacks: the new `onCreate:afterSuper` (`run/logcat.txt:451`, 32.176) before the old `point=onDestroy` (`:539`, 32.730). The hooks' placement in them is PROVEN: the attach runs inside the new `onCreate` (`FlutterActivity.java:645`), the detach inside the old `onDestroy` (`FlutterActivity.java:913-921`), where its `HeadlessMode? true` is logged (`run/logcat.txt:542`, between `:539` and `:543`).
  - Its `setActivity(newActivity, newMessenger)` has already cleared the flag, and replaced `mEventChannelTask` with a channel on the NEW engine's messenger (`:103-107`).
  - The old instance's detach then sets the flag, and removes the new engine's handler.
- **Nothing clears the flag afterwards.**
  - The module clears it only when an Activity attaches (`:103`).
  - The SDK clears it on `ProcessLifecycleOwner`'s ON_START and ON_RESUME (`native/background-fetch/android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/LifecycleManager.java:154`, `:192` (399765c)).
  - A restart sends neither. MEASURED: no LifecycleManager line after the trigger. WO-007 gives the reason: the new Activity starts before the old one stops (`WO-007-capacitor-android-destroy-not-headless.md:300`).
- **So the fetch takes a headless branch of `doFetch`** (`BackgroundFetch.java:272-291` (399765c)):
  - **`stopOnTerminate: true`:** `- Stopping on terminate`, then `stop(taskId)` (`:276-278`). That cancels the job and drops its config (`:161-173`).
  - **`false` with a job service:** `fireHeadlessEvent` (`:279-285`). That starts the plugin's `HeadlessTask` engine (`HeadlessTask.java:148`) beside the live one.

**The second half: one event channel and one sink for the whole process. PROVEN in source; the effect SUSPECTED, not measured.**
- **One channel, one sink, on the singleton.** `mEventChannelTask` (`BackgroundFetchModule.java:53`), and one `FetchStreamHandler` with one `mEventSink` (`:42`, `:68`, `:242`).
  - Each attach replaces the field (`:104-107`). Each real detach removes whatever channel it holds (`:110-113`).
  - The handler's `onCancel` is empty (`:271-273`), so the sink is never cleared.
  - **1.6.2 fixed this same "last-attached wins" shape for the method channel,** with one channel per messenger (`:47-52`; `CHANGELOG.md:16`; `7933027`). The event channel kept it.
- **A sink set before the removal keeps working.**
  - `EventSink.success` checks only `hasEnded` and whether it is still its request handler's `activeSink` (`plugin/common/EventChannel.java:187`, `:248`). `setStreamHandler(null)` resets neither: it only removes the channel's entry from the messenger (`EventChannel.java:107-118`; `embedding/engine/dart/DartMessenger.java:202-208`). So it never asks whether the channel still has a handler.
  - In both captures, the new isolate's `- configure` came before the old detach: 28 ms before in `run` (32.702 → 32.730) and 25 ms in `stop-on-terminate-false` (15.406 → 15.431).
  - Dart's `configure()` sends `listen` before its `configure` call (`lib/background_fetch.dart:442-451`, then `:462-463`). That order is INFERRED from Dart's async semantics.
  - So the lab's red is the flag's alone.
- **A listen after the removal is lost.** Each step is PROVEN in Flutter's source; the chain is INFERRED:
  - the platform side answers a message that has no handler with an empty reply (`embedding/engine/dart/DartMessenger.java:294-295`);
  - Dart's `invokeMethod('listen')` then throws `MissingPluginException` (`platform_channel.dart:360-364`), which `receiveBroadcastStream` reports and swallows (`:710-721`);
  - the plugin's Dart never listens again in that isolate (`background_fetch.dart:442`);
  - the module's sink is still the old engine's. A delivery to it reaches a detached `FlutterJNI`, which logs a warning and drops it (`embedding/engine/FlutterJNI.java:1177-1185`).
- **So an app whose new isolate first calls `configure()` after the old Activity is destroyed would lose every foreground fetch for that isolate's life, even with the flag fixed.** SUSPECTED: no scenario delays that `configure()`.

**Why Flutter's other Android cells are green, and must stay green** (sweep `20261113-183048`):
- **A recreation** (`recreate-foreground-android`: both steps PASS, Lab1).
  - A config-change destroy calls `onDetachedFromActivityForConfigChanges` (`FlutterActivityAndFragmentDelegate.java:787-788`). It is empty in the plugin (`BackgroundFetchPlugin.java:34-39`).
  - The old engine is gone before the new Activity is created: `point=onDestroy … isChangingConfigurations=true` (`run/logcat.txt:396`, 57.146), then `onDestroy:afterSuper` (`:504`, 57.166: `super.onDestroy`, and with it the engine's destroy, has returned; `FlutterActivityAndFragmentDelegate.java:810-811`), then the new `point=onCreate … recreated=true` (`:597`, 57.170).
  - The fetch reaches the new isolate (`:908-909`).
- **A plain finish** (`headless-activity-destroyed` and `stop-on-terminate/finish`: PASS, Lab2).
  - The finishing Activity is the current one: its `point=onDestroy` names itself as `current=` (`headless-activity-destroyed/FIX/run/logcat.txt:384`).
  - The detach sets the flag (`:387`). The fetch goes to the Dart headless task (`:397-405`), which finishes it (`:407-408`). `flutterHeadlessInit 1`.
- **A new process** (`headless-kill`: PASS, Lab1).
  - No Activity attaches. The SDK starts headless (`LifecycleManager.java:40` (399765c)) and logs it from its ON_CREATE timer (`headless-kill/FIX/run/logcat.txt:347`).
  - The `HeadlessTask` engine registers this plugin too (`HeadlessTask.java:148`), but attaches it to no Activity, so `setActivity` never runs.
  - Both fetches go headless (`:349`, `:386`). `flutterHeadlessInit 1`.

**How React Native and Capacitor avoid it:**
- **WO-007's rule:** act on a destroy only for the Activity resumed most recently, and never for a recreation (`WO-007-capacitor-android-destroy-not-headless.md:251`, `:310`).
- **Capacitor implements it in the plugin** (`capacitor/background-fetch/android/src/main/java/com/transistorsoft/bgfetch/capacitor/BackgroundFetchPlugin.java`):
  - a static `WeakReference` holds the BridgeActivity resumed most recently, set in `handleOnResume` (`:38-49`);
  - `handleOnDestroy` goes headless only when its own Activity is that one and is not changing configurations (`:51-65`; the condition is `:62`).
- **React Native gets it from its framework.**
  - Its plugin goes headless on any destroy that is not a recreation (`react-native/background-fetch/android/src/main/java/com/transistorsoft/rnbackgroundfetch/RNBackgroundFetchModule.java:117-126`; WO-003's guard is `:121-122`).
  - React Native's host moves to its destroyed state on an Activity's destroy only when that Activity is its current one (`ReactAndroid/src/main/java/com/facebook/react/runtime/ReactHostImpl.kt:309-318`). That this is the path to the plugin's `onHostDestroy` is PROVEN for an Activity's destroy, under `ReactAndroid/src/main/java/com/facebook/react/`: `ReactActivityDelegate.java:208-210` → `ReactDelegate.kt:161-166` → that check → `moveToHostDestroy` (`ReactHostImpl.kt:891-895`) → `runtime/ReactLifecycleStateManager.kt:68-87` → `bridge/ReactContext.java:338-363`, which calls each listener's `onHostDestroy()` (`:358`); the plugin registers as one (`RNBackgroundFetchModule.java:25`). Two limits: the host's own teardown reaches the listeners with no Activity check (`ReactHostImpl.kt:1497`); and that the lab app takes `ReactDelegate`'s bridgeless branch (`:163-166`) is INFERRED from `newArchEnabled=true` (`labs/platforms/react-native/app/android/gradle.properties:12`), since the branch is a runtime flag (the legacy branch checks the Activity too, `ReactInstanceManager.java:721-727`). The behaviour is MEASURED (above).
- **Flutter has no such filter.**
  - Each engine notifies only its own plugins (above).
  - The ActivityAware callbacks include no resume (`embedding/engine/plugins/activity/ActivityAware.java:40`, `:62`, `:76`, `:101`).
  - So the rule needs process-wide state. The module, already a process singleton (`BackgroundFetchModule.java:28`, `:55-65`), is the natural place: a design choice, not a constraint. A static on Flutter's `BackgroundFetchPlugin` would serve as well, as Capacitor's static `WeakReference` on its plugin class does (Capacitor's `BackgroundFetchPlugin.java:38-40`, above).
- **WO-007's parity line for Flutter** ("not affected", `WO-007-…:200-203`) held only for a plain finish and a recreation (WO-002 Log, `:1209`).

**Not a regression (PROVEN; the dating INFERRED).**
- The detach has set the flag since 1.1.3 (`a333243`, 2023-01-10).
- The one event channel dates from the V2-embedding port (`f03a82c`, 2019-12-17). 1.6.2 (`7933027`, 2026-04-20) left it in place.

**Who reaches it (INFERRED; how often is NOT CHECKED):**
- **An app that restarts itself:** `finish()` then `startActivity()`, or a `NEW_TASK|CLEAR_TASK` relaunch, for example after a language or account change. This is the lab's route.
- **A user who relaunches within about a second of a finish.** WO-007's probe saw the new Activity resume first at 0.1 and 0.3 s on Capacitor (`WO-007-…:302-309`). On Flutter, NOT MEASURED.
- **An app with two FlutterActivity screens, each with its own engine** (add-to-app).
  - A back press from the second screen resumes the first before the second is destroyed (INFERRED from Android's ordering).
  - The second's detach then sets the flag with the first on screen.
  - The module's sink stays the second engine's. So even after the flag clears, the first screen gets no foreground fetch until another isolate listens.

**The cost:**
- **With `stopOnTerminate: true`** (the default, `lib/background_fetch.dart:150`): the next fetch cancels the job and drops the config (MEASURED: `jobIds -`). No fetch comes again until the app calls `configure()`. In most apps that is the next cold launch (INFERRED).
- **With `false` and `enableHeadless: true`:** fetches go to the Dart headless task in a second engine beside the live UI (MEASURED), never to `configure()`'s callback.
  - That lasts until another Activity attaches, or the app returns from the background (ON_START), per WO-007. INFERRED for Flutter.

## Proposed fix

Two changes, both in `flutter/background_fetch/android/`. There is no Dart, iOS or native change.

**Change 1, the flag: WO-007's rule, held in the module.**
- **`BackgroundFetchPlugin.java`: the instance remembers the Activity it is attached to.**
  - It sets it in `onAttachedToActivity` (`:29-32`), and also in `onReattachedToActivityForConfigChanges` (`:41-45`, empty today). The re-attach matters for a cached engine, whose instance survives a recreation.
  - It clears it in `onDetachedFromActivity` (`:47-50`).
  - `onDetachedFromActivityForConfigChanges` (`:34-39`) still leaves the flag alone (WO-003).
- **`BackgroundFetchModule.java`: `setActivity(Activity, BinaryMessenger)` (`:92-115`) becomes an attach and a detach that name the plugin instance.** The module keeps an ordered record of the instances attached to an Activity.
  - **Attach:** it records the instance. `HeadlessTask.destroyBackgroundIsolate()` and `setHeadless(false)` run as today (`:100`, `:103`).
  - **Detach:** it drops the instance. It goes headless only if D1's rule says this was the app's last Activity; under D1 (a), only if no other instance is still attached.
  - **Engine detach of an instance still recorded:** it drops the instance and leaves the flag alone. That is a recreation whose engine is destroyed (`FlutterActivityAndFragmentDelegate.java:787-788`, then `:810-811`).
  - The record must drop an instance on both kinds of detach. Otherwise it keeps a destroyed engine's plugin alive.
- **Nothing in `onAttachedToEngine` or `onDetachedFromEngine` touches the flag.** Every engine registers this plugin, `HeadlessTask`'s included (`HeadlessTask.java:148`).

**Change 2, the channel: on the plugin instance, as 1.6.2 did for the method channel.**
- **Each instance owns an `EventChannel`** on its own engine's messenger, with its own stream handler and its own sink, for the lifetime D2 picks.
  - Its `onCancel` clears its sink. Android's is empty today (`:271-273`); iOS's already clears it (`BackgroundFetchPlugin.m:225-228`).
  - Its teardown removes only its own channel's handler.
- **The module keeps the one `BackgroundFetch.Callback` the SDK holds** (`:152-155`).
  - Its `onFetch` and `onTimeout` deliver to the sink of the instance attached most recently whose Dart side is listening.
  - With none, they log the existing error (`:250`, `:261`), unless D5 (b) lets a detached instance whose engine lives stand in.
- **`mEventChannelTask` (`:53`) and the singleton's `setStreamHandler` calls (`:104-113`) go.**

**Anchors:**
- `// (WO-012)` at each changed method in both files.
- An `[Android]` entry under `CHANGELOG.md`'s `## Unreleased`, carrying `(WO-012)`. For example:

  > * [Android] Fix: when an app restarts itself (`finish()` then `startActivity()`), the old Activity's destroy no longer makes the plugin headless with the new screen up, which stopped the next fetch (`stopOnTerminate: true`) or sent it to the headless task (`false`). Each FlutterEngine now keeps its own fetch event channel. (WO-012)

- `restart-foreground-android.yaml`'s Flutter paragraph (`:34-43`) adds the green sweep. It already names WO-012 (`:35`, since WO-002's 2026-11-16 entry). This is prose only.
- Under D4 (a), the new step's description carries `(WO-012)`.

**Decisions:**

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | When a real detach makes the SDK headless | (a) Only when no other plugin instance is attached to an Activity. (b) Only when the detaching Activity is the one resumed most recently: WO-007's literal rule. ActivityAware has no resume callback, so the record would come from `Application.ActivityLifecycleCallbacks`, or from the `flutter_plugin_android_lifecycle` package (a new dependency for every app). The binding's own `HiddenLifecycleReference` says "DO NOT USE THIS CLASS IN AN APP OR A PLUGIN" (`HiddenLifecycleReference.java:14`). (c) Only when the detaching Activity is the one attached most recently. | (a). It asks what the SDK needs to know: whether any Flutter UI is left to deliver to. It needs only callbacks the plugin already gets. (a) and (b) give the same answer in every ordering the lab or WO-007's probe has measured: a plain finish, a recreation, and a restart at 0.1, 0.3 and 1.5 s (INFERRED for Flutter). (c) turns the lab green too, since the new Activity attaches first. But it fails the two-FlutterActivity back press, because the screen being left was attached last (INFERRED). WO-010 faces the same choice for Cordova; the two need not match. |
| D2 | The instance's channel lifetime | (a) `onAttachedToEngine` to `onDetachedFromEngine`: the Dart isolate's lifetime. (b) `onAttachedToActivity` to `onDetachedFromActivity`: today's span, moved onto the instance. | (a). Under (b), a cached engine that detaches from one Activity and attaches to another loses its fetches. Its Dart listens once per isolate (`background_fetch.dart:442`), so the new channel never gets a `listen` (INFERRED). Today that case works only because the singleton keeps the old sink, and that sink does not check its channel's handler (`EventChannel.java:247-252`; INFERRED, not run). (a) also gives `HeadlessTask`'s engine a channel. That isolate listens only if the app's headless callback calls `configure()`, and it is never a delivery target, since it never attaches to an Activity. |
| D3 | Where it ships | The plugin's next release, whose `## Unreleased` already holds WO-001's two iOS entries (`CHANGELOG.md:3-9`); or an Android-only patch first | Your call. The fix touches no Dart, iOS or native code. It depends on WO-011's D3, which recommends 2.0.0 for that order's change (`WO-011-flutter-stop-returns-int.md:120`). If Chris first decides that `master`'s next release is 2.0.0 (WO-011's second option, `:125`), this fix reaches 1.x apps only through a 1.x release from a branch that excludes WO-011, or waits for 2.0.0 (`:127`): a 2.0.0 does not reach a `^1.x` constraint (pub's documented rule, NOT CHECKED, as WO-011 D3 labels it). If WO-011's branch waits unmerged instead (`:124`), this fix can ship in a 1.x from `master`. |
| D4 | How change 2 is proven | (a) A WO-002 follow-up: a step in `restart-foreground-android` where the new isolate first calls `configure()` only after the old Activity is destroyed, for example a driver knob that holds boot 2's `configure()`; the design is WO-002's. It reads red on change 1 alone (a throwaway build) and green on the fix. (b) An `adb` probe outside labkit, recorded in this Log, as WO-007 did before its scenario existed (`WO-007-…:302-309`). (c) Source only. | (a). Neither existing step can show change 2: in both, the new isolate configured 25–28 ms before the old Activity's `point=onDestroy` (MEASURED), and so listened first (INFERRED, Diagnosis). Until (a) lands, (b). |
| D5 | A fetch when no attached instance is listening, but a detached engine's isolate still is (Scope check) | (a) Drop it, as change 2 states, with the null-sink error. The CHANGELOG says so, as a behaviour change for apps whose FlutterEngine outlives its Activity (a cached engine). What it breaks: those apps' fetches after their Flutter screen closes, until a Flutter Activity attaches again, with their BGTasks left unfinished (Out of scope). (b) Fall back to the instance attached most recently whose engine is alive and whose Dart side is listening. What it costs: the record keeps a detached instance until its `onDetachedFromEngine`; a fetch runs Dart with no Flutter UI while the flag reads not headless, as today; and the fallback must never pick an engine that has not attached to an Activity, such as `HeadlessTask`'s or another plugin's background engine. Allowing only instances that have attached at least once does that. Identifying `HeadlessTask`'s engine instead needs an accessor and a check at delivery time, since `sBackgroundFlutterEngine` is private (`HeadlessTask.java:37`) and is set only after its constructor has registered the plugin (`HeadlessTask.java:148`; `FlutterEngine.java:426-428`). | Your call. Neither is exactly today's behaviour: under D2 (a), a pre-warmed engine that calls `configure()` before it attaches can listen, which it cannot today (Scope check). INFERRED, not run. |

## Scope check

- **Who calls the plugin's hooks** (Flutter 3.47.5):
  - **`onAttachedToActivity` and `onReattachedToActivityForConfigChanges`:** `FlutterEngineConnectionRegistry.attachToActivityInternal` (`:329-374`; the choice is `:366-372`). It runs from every FlutterActivity's or FlutterFragment's `onAttach` (`FlutterActivityAndFragmentDelegate.java:202-214`).
  - **`onDetachedFromActivity`:** `detachFromActivity` (`:396-408`), from a host's `onDetach` (`:784-792`). It also runs in these cases:
    - another Activity takes over the same engine (`FlutterEngineConnectionRegistry.java:316-327` → `FlutterActivity.java:898-910`). The instance is detached, then re-attached to the new Activity;
    - an attached engine is destroyed (`FlutterEngineConnectionRegistry.java:116-126`, `:294-305`);
    - the app attaches a Service, BroadcastReceiver or ContentProvider to an engine still attached to an Activity: `attachToService`, `attachToBroadcastReceiver` and `attachToContentProvider` each call `detachFromAppComponent` (`:524`, `:586`, `:635` → `:294-305`);
    - the app removes the plugin from an engine still attached to an Activity (`remove`, `:231-235`, also reached from `removeAll`, `:287-290`). `onDetachedFromEngine` follows (`:274`). The fix marks the instance detached there, as on any real detach, and `onDetachedFromEngine` then removes it (the instance stays recorded until then: D5 (b)).
  - **`onDetachedFromEngine`:** `remove` (`:274`), from an engine's destroy, after any Activity detach (`:116-126`).
- **Engines with no Activity:**
  - `HeadlessTask`'s (`HeadlessTask.java:148`), and other plugins' background engines. 1.6.2's commit names flutter_background_geolocation's (`7933027`).
  - Under the fix, they never touch the flag and never remove another engine's channel. Today their detach does not touch the event channel either, only the method channel (`BackgroundFetchModule.java:82-90`).
- **Who reads the flag:** `BackgroundFetch.doFetch` (`:272`) and `BGTask.onTimeout` (`native/background-fetch/android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/BGTask.java:207` (399765c)).
- **Who reads the sink:** only the module's callback (`BackgroundFetchModule.java:245-265`). The Dart side listens once per isolate, at its first `configure()` (`background_fetch.dart:442-451`).
- **Which isolate gets a fetch while two UI engines are attached:**
  - Today it is the last engine to listen. Under change 2, it is the engine attached most recently that is listening.
  - **Same answer:** in a restart, before and after the new isolate listens, and on a second screen while it is open.
  - **Different answer:** after a second screen closes. Today the fetch goes to that screen's dead sink; under the fix, to the first screen.
  - **A fetch between a new Activity's attach and its isolate's first `configure()`** goes to the old isolate while that one is still attached. After it detaches, the fetch is lost when the old engine is destroyed with its Activity, as today; an old engine that outlives its detach still gets it under D5 (b) (INFERRED).
- **Which isolate gets a fetch when no instance is attached, but an engine that outlived its Activity still listens** (add-to-app with a `FlutterEngineCache` engine; INFERRED, not run):
  - Its FlutterActivity finishes. The engine survives (`FlutterActivity.java:1081-1092`). The detach sets the flag, today and under D1 (a); today it also removes the channel's handler (`BackgroundFetchPlugin.java:47-50`; `BackgroundFetchModule.java:109-113`).
  - When the app later returns to the foreground on a native Activity, ProcessLifecycleOwner's ON_START clears the flag (`LifecycleManager.java:154` (399765c)), and the next fetch takes the not-headless branch (`BackgroundFetch.java:272-275` (399765c)).
  - **Today** it reaches the cached isolate: the module's sink is still that engine's, and `success` does not check the channel's handler (Diagnosis). That holds only for an isolate that first called `configure()` while attached. The delegate never starts Dart on a cached engine (`FlutterActivityAndFragmentDelegate.java:503-506`): the app runs it before caching, so it can call `configure()` before any Activity attaches. No handler exists on its messenger until `setActivity` (`BackgroundFetchModule.java:104-107`). So one that calls `configure()` before its first attach, for example from `main()`, loses its one listen and gets no fetch today. Under D2 (a) it would listen, and get fetches while attached.
  - **Under change 2,** no instance is attached, so the fetch is dropped with the null-sink error (`:250`, `:261`), even though under D2 (a) that engine's channel and sink are still in place, and its BGTask is left unfinished (Out of scope). D5 decides.
- **API:**
  - The module's lifecycle methods are package-private (`BackgroundFetchModule.java:71`, `:82`, `:92`).
  - `BackgroundFetchPlugin` keeps its public class, its no-arg constructor and its interfaces (`BackgroundFetchPlugin.java:10`, `:15`). So apps that add it to engines of their own are unaffected (INFERRED).
  - No Dart change.
- **Kept:** 1.6.2's `HeadlessTask.destroyBackgroundIsolate()` on attach (`:100`), and its per-messenger method channels (`:47-52`, `:71-90`).
- **What customers will see:**
  - After a restart, fetches keep reaching `configure()`'s callback, and with `stopOnTerminate: true` the job survives.
  - A plain finish, a kill and a recreation behave as before (INFERRED), except for an engine that outlives its Activity: its fetches once ON_START clears the flag are D5's (above).
  - The changelog line says so.

## Platform parity

- **Android, Flutter:** affected. MEASURED (Diagnosis).
- **Android, React Native: not affected.** MEASURED green (sweep `20261113-104449`). PROVEN for an Activity's destroy: the plugin's `onHostDestroy` is reached only for React Native's current Activity (`ReactHostImpl.kt:309-318`, on the path cited in the Diagnosis). That the lab app takes the bridgeless branch is INFERRED (Diagnosis).
- **Android, Capacitor: not affected since WO-007.** MEASURED green on `4d431c1`, in the same sweep. PROVEN at `3fa169a` (`BackgroundFetchPlugin.java:38-65`), whose `android/` is identical to `4d431c1`'s (Citation bases).
- **Android, Cordova: affected. Its own work order is WO-010.**
  - MEASURED red on both steps: `labs/captures/cordova/restart-foreground-android/FIX/`, sweep `20261115-215127`, Lab2, plugin `368eb85`.
  - Its `onDestroy` goes headless on any destroy that is not a recreation (`cordova/background-fetch/src/android/CDVBackgroundFetch.java:160-170`).
- **iOS, change 1: N/A, because iOS has no headless mode.**
  - `stopOnTerminate` and `enableHeadless` are Android-only (`lib/background_fetch.dart:51`, `:60`; `types/background-fetch/src/interfaces.ts:12`, `:39`).
  - The iOS plugin sets no such flag.
  - An iOS app cannot relaunch its own UI the way `finish()` then `startActivity()` does.
- **iOS, change 2: not affected in this shape (PROVEN).**
  - Each registration builds its own instance (`ios/background_fetch/Sources/background_fetch/BackgroundFetchPlugin.m:48`).
  - Each instance has its own channels, on its own registrar's messenger (`:55-61`), and its own `eventSink` (`:26`, `:220-228`).
- **iOS, a related shape: SUSPECTED, not measured.** The native subscriber an instance feeds is keyed process-wide by the fixed `PLUGIN_ID`.
  - Each `configure()` and `start()` re-points it (`:96`, `:114`). The SDK replaces an existing subscriber's callbacks (`native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.m:307-318` (399765c)).
  - Any instance's `detachFromEngineForRegistration:` removes it (`BackgroundFetchPlugin.m:69-71`; `TSBackgroundFetch.m:324-334`).
  - So with two engines, the last `configure()` wins, and either engine's teardown removes the subscriber.
  - The lab runs one engine on iOS: all 32 `pluginLoad` lines in `labs/captures/flutter-ios/` read `n=1`.
  - Out of scope (below).

## Test plan

- **Red before the fix:** already MEASURED (sweep `20261113-183048`). Nothing needs re-running first.
- **On the fix branch, Flutter Android, Lab1 and Lab2.** The lab stages the checkout `lab.yaml` names (`labs/platforms/flutter/lab.yaml:27`), and `run.json` records its commit and dirty flag. WO-011 changes the same file (`BackgroundFetchModule.java:166-170`), and either order can land first. So the Log records each sweep's plugin stamp (`run.json` `plugin_source`) and any other order's code in the checkout.
  - **`restart-foreground-android`: both steps green** on the values the scenario already expects (`:81-127`), with Flutter's `platform_expect` (`:63`):
    - `driverEvents fetch=1`, `driverTally fetch:flutter_background_fetch=1`, `fetchFinished 1`, `stoppingOnTerminate 0`, `headless false`, `jobIds 999`;
    - in `stop-on-terminate-false`, also `headlessEvents.fetch 0`;
    - `restartOrder resumed-first` in both. Without it, the step proves nothing (`:16-18`).
  - **Change 1's own evidence:** the old Activity's `point=onDestroy:afterSuper` reads `headless=false`, and `lifecycleTally` holds no `headless:true`. `lifecycleTally` is recorded, not asserted (`:93`), so it has to be read, as WO-007's D4 (a) did.
  - **Change 2 (D4):**
    - the new step reads red on change 1 alone: `driverEvents -` with `headless false`, and the old engine's `W/FlutterJNI: Tried to send a platform message to Flutter, but FlutterJNI was detached from native C++` on the events channel (MEASURED by the D4 (b) probe; the `MissingPluginException` report this line first predicted did not appear: Log);
    - it reads green on the fix;
    - until the step lands, the probe.
- **Regression: the Flutter Android suite.** Expect 12/13, with `api-smoke` red only for WO-011's finding, or 13/13 if WO-011's fix is in the swept code. These must stay green, with their current values:
  - `headless-activity-destroyed` and `stop-on-terminate/finish`: `headless true`, with `HeadlessMode? true` at the finish;
  - `headless-kill` (`flutterHeadlessInit 1`, as `headless-activity-destroyed` pins it too), `stop-on-terminate/kill` and `termination-calibration`;
  - `recreate-foreground-android`, both steps: `headless false`, and no `HeadlessMode? true`.
  - Run the three ordering cells (`restart-foreground-android`, `recreate-foreground-android`, `headless-activity-destroyed`) on both AVDs, as WO-007 did.
- **Not needed:**
  - **React Native and Capacitor runs.** No code of theirs changes, and the scenario changes only in prose. A D4 step runs on all four bridges as WO-002's work.
  - **A Flutter iOS sweep.** No Dart or iOS file changes.
- **Not checked by the lab:**
  - **D1's difference from (c),** the two-FlutterActivity back press. The host has one Activity class, `singleTop` (`labs/platforms/flutter/app/android/app/src/main/AndroidManifest.xml:12`). Optional: a probe with a second FlutterActivity in a scratch build.
  - **The cached-engine paths.** The lab never reaches them (PROVEN): the host gives each Activity its own engine and caches none (`MainActivity.kt:22-27`; no `FlutterEngineCache` under `labs/`), and FlutterActivity destroys an engine it created (`FlutterActivity.java:1081-1092`). So a recreation reaches `onAttachedToActivity` on a new engine, never the re-attach (`FlutterEngineConnectionRegistry.java:366-372`), and no Activity takes over a live engine (`:316-327` → `FlutterActivity.java:898-910`). The fix's behaviour on them is INFERRED: change 1's `onReattachedToActivityForConfigChanges`, D2 (a)'s move between Activities, and D5.
  - Optional: a scratch build whose Activity uses a `FlutterEngineCache` engine, recorded in this Log like the probe above. A recreation there needs `trigger: bold` (`recreate-foreground-android.yaml:17-18`): the manifest's `configChanges` include `orientation` and `screenSize` (`AndroidManifest.xml:15`), so a rotation recreates nothing. A takeover needs the new Activity to attach while the old one is still attached, as the restart trigger does (`MainActivity.kt:49-63`); a relaunch after the old Activity has detached is a plain attach.
- **Lab changes, and whose they are:**
  - **The description prose** is this order's, as WO-006 and WO-007 did theirs.
  - **D4 (a)'s knob and step** are WO-002's.
  - **No `platform_expect` change.** Flutter's `jobsCancelled 999` is the new engine's reschedule (`run/logcat.txt:468-469`), which the fix keeps.
- **No unit test.**
  - The plugin has no test suite. Its `CLAUDE.md` says "There are no automated unit/integration tests in this repo". There is no `android/src/test`, and `example/test/widget_test.dart` is the template's stale widget test.
  - Building a JVM test harness is out of scope. The lab steps carry the id.

## Out of scope

- **The sibling work orders drafted alongside this one:**
  - **WO-009** (`WO-009-cordova-callback-api.md`). Cordova's JS API is callback-style:
    - `start`, `stop`, `status` and `scheduleTask` return nothing where the types declare Promises;
    - `stop(taskId)` throws on iOS and cancels every task on Android;
    - on iOS, `start`'s native half is unordered against a later `stop`.
  - **WO-010** (`WO-010-cordova-android-restart-ordering.md`). Cordova Android fails `restart-foreground-android`: when the app restarts itself in the foreground, the plugin does not act on the current Activity alone (WO-007's rule). Same scenario, different plugin. This order does not touch Cordova.
  - **WO-011** (`WO-011-flutter-stop-returns-int.md`). Flutter's `stop()` is `Future<int>` and resolves the status, where the types declare a boolean. It is the other Flutter red on the same sweep.
  - **WO-013** (`WO-013-stop-no-arg-scope.md`). `stop()` with no argument means different things on Android. There React Native and Capacitor stop only their own fetch task, while Cordova and Flutter cancel every task, as the types' doc says. On iOS all four bridges cancel every task. Unmeasured; it needs a new lab scenario.
- **Flutter iOS's process-wide subscriber id** (Platform parity). It would get its own work order, if Chris wants one, after a two-engine measurement.
- **A cached engine whose only Activity finishes** still goes headless while its isolate lives, as today (INFERRED): the fix does not change the flag there. What happens to its fetches once ON_START clears the flag does change; that is D5 (Scope check), not out of scope.
- **What a lost delivery leaves behind:** a BGTask the app never finished. WO-007 left this out too.
  - The SDK's 60 s timeout does not finish it. In the not-headless branch it only calls the callback (`BGTask.java:207-211` (399765c)), which delivers to whatever sink is current when it fires: the module's one sink today (`BackgroundFetchModule.java:256-264`), the one change 2 picks under the fix.
  - So the timeout is lost too, unless an isolate has listened since. That isolate's `onTimeout`, by default `finish(taskId)` (`background_fetch.dart:444-449`), would then finish it (INFERRED).
  - A later `configure()` also finishes it, through the reschedule (`BackgroundFetch.java:88-92` → `BGTask.java:124-127` (399765c)).
  - Otherwise JobScheduler's own timeout ends the job, and the BGTask stays in `mTasks` until a later configure, stop or finish (INFERRED, as WO-007 traced for Capacitor: `WO-007-capacitor-android-destroy-not-headless.md:104-109`).
- **Dart's `configure()` keeping its first callbacks per isolate** (`background_fetch.dart:442`).
- **Native headless detection for every bridge.**

## Not an acceptable shortcut

- **Changing the lab to avoid the red.**
  - That includes:
    - a Flutter `platform_expect` or `not_applicable` that relaxes `headless`, `driverEvents`, `stoppingOnTerminate`, `jobIds` or `headlessEvents.fetch`;
    - dropping `flutter` from `platforms` (`:55`);
    - a later relaunch, or another trigger: a destroy that comes before the relaunch resumes passes even on the regressed rule (`:16-18`);
    - the driver or the probe calling `setHeadless`.
  - The scenario says so in advance: "A finding for its own work order, never relaxed here" (`:43`). React Native and Capacitor give these values in sweep `20261113-104449`, on the same native (`4.1.1-master-399765c`).
  - Flutter's existing `platform_expect` (`driverTally`, `jobsCancelled 999`) stays. It records the reschedule, not this defect.
- **Change 1 alone.**
  - Both steps would probably go green: the new isolate's `configure()` ran 25–28 ms before the old detach (MEASURED), and its listen before that (INFERRED).
  - But the old detach would still remove the new engine's channel handler, so an app that configures later loses its fetches (SUSPECTED, Diagnosis).
  - That green would be luck of timing.
- **Change 2 alone.** The flag still goes headless, so both steps stay red (INFERRED from the same path).
- **Dropping `setHeadless(true)` from the detach.**
  - It would pass the restart.
  - But that call is what makes `headless-activity-destroyed` and `stop-on-terminate/finish` green (MEASURED). Without it, Flutter gets WO-007's defect.
- **Going headless only when `isFinishing()`.** A restarting app's old Activity is finishing too (MEASURED: `isFinishing=true`, `run/logcat.txt:539`).
- **Going headless on a real detach only when `LifecycleManager.isBackground()`, or any other `ProcessLifecycleOwner` state, says the app is in the background.** The module already imports the class (`BackgroundFetchModule.java:12`), and the lab's probe prints `background=` beside `headless=` at every `point=onDestroy` (`labs/platforms/flutter/app/lab_probe/android/src/main/kotlin/com/transistorsoft/fetchlab/labprobe/LabProbePlugin.kt:158`).
  - It would pass every ordering cell (INFERRED from MEASURED values). The restart's old destroy reads `background=false` (`run/logcat.txt:539`; `stop-on-terminate-false/logcat.txt:420`). Both plain finishes read `background=true` (`headless-activity-destroyed/FIX/run/logcat.txt:384`; `stop-on-terminate/FIX/finish/logcat.txt:346`; both Lab2). A recreation's detach does not touch the flag.
  - The finishes pass on timing alone. After ON_START, `mIsBackground` turns true again only on ProcessLifecycleOwner's ON_PAUSE or ON_STOP (`LifecycleManager.java:174`, `:181` (399765c)), and ON_PAUSE is sent 700 ms after the last pause (`WO-007-…:153`). In both captures (`headless-activity-destroyed` / `stop-on-terminate/finish`) it arrived about 0.34 s before the destroy, which came about 1.04 s after the finish: `finishNow` 43.395 / 54.667 (`:258` / `:218`), `☯️ onPause` 44.095 / 55.368 (`:375` / `:337`), `point=onDestroy` 44.436 / 55.715 (`:384` / `:346`).
  - Two cases would never go headless, which is WO-007's defect on Flutter (INFERRED): a finish whose destroy comes before that ON_PAUSE (animations off, a fast launcher), and a finish that leaves only a non-Flutter Activity of the app started, since ProcessLifecycleOwner sends neither event while one is. With another FlutterActivity left, staying out of headless is right, as D1 (a) gives.
  - The lab cannot tell this gate from D1's rule, so its greens do not rule it out: read the change.
- **Asking `ActivityManager` whether another of the app's Activities or tasks exists at the detach** (`getRunningTasks`, `getAppTasks`).
  - The SDK dropped its own `getRunningTasks` check (`isMainActivityActive`, with the `GET_TASKS` permission) for `LifecycleManager` in `b331a49`, and the plugin recorded the move in 1.1.1 (`CHANGELOG.md:120`). WO-010 rejects the same route for Cordova.
  - On Flutter it also answers the wrong question: whether any Activity of the app exists, not whether a Flutter engine is still attached to receive the fetch, which is what D1 (a) asks. A native Activity left on screen would answer yes with no Flutter UI to deliver to (INFERRED).
- **Touching the flag in `onAttachedToEngine` or `onDetachedFromEngine`.**
  - Every engine registers the plugin: `HeadlessTask`'s, and other plugins' background engines (`7933027`).
  - A `setHeadless(false)` there would clear the flag when a headless launch starts its engine. `headless-kill`'s next fetch would then go to a `configure()` callback that no isolate listens to (INFERRED).
  - A `setHeadless(true)` there would fire whenever some background engine is destroyed with the UI up.
- **Going headless in `onDetachedFromActivityForConfigChanges`.** That moves WO-003 to Flutter. `recreate-foreground-android` catches it.
- **Debouncing the detach:** setting the flag after a delay unless an Activity attaches. In a restart the new Activity attached before the old one detached (MEASURED: the new `onCreate:afterSuper` at `run/logcat.txt:451` before the old `onDestroy` at `:539`; the attach inside that `onCreate` is PROVEN, `FlutterActivity.java:645`), so no later attach comes to cancel it. A timer also adds a race.
- **Reading the Activity's lifecycle through `HiddenLifecycleReference`.** The class forbids plugin use (`HiddenLifecycleReference.java:14`). If D1 (b) is chosen, use the sanctioned package or `ActivityLifecycleCallbacks`.
- **Keeping one channel and one sink on the module, re-created on each attach.**
  - That is today's shape: the last attach wins, and any detach strips another engine's channel.
  - 1.6.2 already rejected it for the method channel (`BackgroundFetchModule.java:47-51`).
- **Making Dart listen again,** for example by resetting `_eventsFetch` in each `configure()`.
  - A listen still finds no handler once another engine has removed it.
  - It would also change the Dart surface, and the fix needs no Dart change.
- **Fixing it in native `LifecycleManager` for every bridge.**
  - This is the route WO-007 and WO-003 rejected (`WO-007-…:245`).
  - It adds a second writer of the flag, and needs a native release with every bridge's Tier 1 re-proven.
- **Finishing or stopping the task from the detach.**
  - Finishing hides the event from the headless task.
  - A stop cancels the job for `stopOnTerminate: false` apps.

## Verification

- [x] D1–D5 decided
- [x] `restart-foreground-android`, both steps green on the fix, Flutter Android, Lab1 and Lab2. The old Activity's `onDestroy:afterSuper` reads `headless=false`, and `lifecycleTally` holds no `headless:true`
- [x] Change 2 proven as D4 picks: red on change 1 alone, green on the fix
- [x] Flutter Android suite on the fix: 12/13, with `api-smoke` red only for WO-011's finding, or 13/13 if WO-011's fix is in the swept code; the Log records each sweep's plugin stamp and any other order's code in the checkout. `recreate-foreground-android`, `headless-activity-destroyed`, `headless-kill`, `stop-on-terminate` and `termination-calibration` green, with their current values
- [x] Anchors:
  - `// (WO-012)` at the changed methods;
  - the CHANGELOG `## Unreleased` `[Android]` entry carries `(WO-012)`;
  - `restart-foreground-android.yaml`'s Flutter paragraph adds the green sweep beside WO-012's name (prose only)
- [x] Platform parity:
  - iOS change 1 N/A, as cited;
  - iOS change 2 not affected in this shape, as cited, with the subscriber-id note recorded;
  - React Native and Capacitor MEASURED green;
  - Cordova left to WO-010

## Log

- 2026-11-16 — **Created (draft) from WO-002's lab board.**
  - **The red:** Flutter Android sweep `20261113-183048`, the committed captures; the first red was `20261113-175923`.
    - `restart-foreground-android` `run` and `stop-on-terminate-false` FAIL on plugin `11d53b8`, Lab2, native `4.1.1-master-399765c`.
    - Captures: `labs/captures/flutter/restart-foreground-android/FIX/{run,stop-on-terminate-false}`.
  - **The controls:** React Native (`8d9ab19`) and Capacitor (`4d431c1`) pass the same steps in `20261113-104449`.
  - **Checked by hand while writing:**
    - each capture's `summary.txt` and `logcat.txt`, matched to its sweep by `timings.t0`;
    - the Flutter plugin at `11d53b8` and its history (`f03a82c`, `a333243`, `7933027`);
    - native at `399765c`;
    - Capacitor at `3fa169a`, React Native at `8d9ab19`, and React Native 0.88.0's `ReactHostImpl`;
    - Cordova at `368eb85`;
    - Flutter 3.47.5's embedding and `services` source, in the local SDK.
  - **New since WO-002's summary:**
    - The event-channel half is a timing hazard the lab's steps did not exercise: the new isolate configured 25–28 ms before the old detach. So it needs its own proof (D4).
    - On iOS, the sink is already per instance. The native subscriber is keyed process-wide (SUSPECTED, out of scope).
  - **Not done yet:** no branch, no code, nothing run.
  - **Reviewed (three lenses, three skeptics per finding): 22 findings, 18 upheld.** Every upheld finding was re-checked against source and captures before it was applied; none was declined. What changed in substance:
    - New D5: what to do with a fetch when only a detached, still-living engine is listening (a cached engine). Change 2 as drafted drops it, where today it is delivered to an isolate that first listened while attached. The Scope check, Change 2, "What customers will see" and Out of scope now say so.
    - Scope check: two more callers of `onDetachedFromActivity`, the app attaching a Service, BroadcastReceiver or ContentProvider, and the app removing the plugin.
    - Not an acceptable shortcut: an `isBackground()` gate (passes every lab cell on timing alone) and asking `ActivityManager`.
    - Test plan: the cached-engine paths are not reached by the lab; an optional probe (`trigger: bold`, the restart trigger).
    - D3: depends on WO-011's 2.0.0 decision.
    - Out of scope: the SDK's timeout does not finish a lost delivery's BGTask.
    - Evidence labels: the restart's attach order is MEASURED for the Activity callbacks and PROVEN for where the hooks run in them. React Native's path to the plugin is now cited end to end (PROVEN for an Activity's destroy; its bridgeless branch INFERRED). The recreation cites `onDestroy:afterSuper`. The controls' sweep is named, and Capacitor's measured `4d431c1` is tied to the cited `3fa169a`.
  - **Cross-checked with WO-009..WO-013 (2026-11-16):**
    - Out of scope: WO-009's start ordering is iOS only, as its title says (Cordova Android's is ordered, MEASURED). WO-013's split is Android only, with Cordova beside Flutter; on iOS all four bridges cancel every task.
    - Test plan and Verification: the suite reads 12/13 with WO-011's red, or 13/13 if WO-011's fix is in the swept code. The Log records each sweep's plugin stamp and any other order's code in the checkout, as WO-011 does.
    - The scenario's Flutter paragraph already names WO-012 (WO-002's 2026-11-16 entry), so the anchor adds only the green sweep. The header notes that entry assigns WO-002's open item (`:1222`) here. No citation into `api-smoke-ios.yaml`; WO-002's and the scenario's line citations re-checked, unchanged.
- 2026-11-16 — **Approved by Chris** ("approve all WO 9-12"): D1 (a) headless only when no other plugin instance is attached; D2 (a) each instance's channel from `onAttachedToEngine` to `onDetachedFromEngine`; D4 (a) the WO-002 follow-up step, with (b) the adb probe until it lands. The two decisions the draft left open, answered by Chris on 2026-11-16: **D5 (b)**, fall back to the instance attached most recently whose engine is alive and whose Dart side is listening, counting only instances that have attached to an Activity at least once; **D3: decided at release.** Status `approved`. Chris chose to take the orders one at a time: this one follows WO-011.
- 2026-11-16 — **Implemented** on `fix/WO-012-flutter-android-restart-ordering` in `flutter/background_fetch`, from `9ccc569` (the plugin's `master`: `11d53b8` plus WO-011, merged and pushed); uncommitted, for Chris's diff review. Status `in-progress`. D3: Chris has since made `master`'s next release 2.0.0 (WO-011 Log), so this ships in 2.0.0 unless a 1.x branch carries it.
  - **Re-verified before code:** `BackgroundFetchPlugin.java` and the module's lifecycle and event-channel code are unchanged from `11d53b8` (WO-011 touched only `stop`); the Diagnosis' source citations stand. Its capture citations do not: the cited captures have since been replaced (Citation bases).
  - **Change 1 (D1 a).** The module keeps every engine-alive plugin instance (`mInstances`, added in `onAttachedToEngine`, removed in `onDetachedFromEngine`) with its `mAttached` flag and `mAttachSeq` order. `onAttachedToActivity` (also from `onReattachedToActivityForConfigChanges`) keeps today's `destroyBackgroundIsolate()` and `setHeadless(false)` and records the attach. `onDetachedFromActivity(plugin, terminal)` clears its attached mark (the instance stays recorded until `onDetachedFromEngine`, so D5's fallback can still reach an engine that outlives its Activity); a real detach goes headless only when no other instance is attached, else it logs "An Activity detached while another is attached: not headless". `onDetachedFromActivityForConfigChanges` clears the mark and leaves the flag alone (WO-003). `onDetachedFromEngine` never touches the flag.
  - **Change 2 (D2 a).** Each plugin instance owns its `EventChannel` and sink from `onAttachedToEngine` to `onDetachedFromEngine`; `onCancel` clears the sink; teardown removes only its own handler. The module's `mEventChannelTask` and `FetchStreamHandler` are gone; its one SDK callback (`FetchCallback`) delivers to `deliveryTarget()`.
  - **D5 (b), Chris's:** the target is the attached instance attached most recently whose sink is set; with no attached instance listening, the engine-alive instance attached most recently whose sink is set; never one with `mAttachSeq == 0` (never attached: `HeadlessTask`'s engine, another plugin's background engine). With none, the existing "mEventSink is null" error.
  - **CHANGELOG** `[Android]` line (the draft's, plus the late-configure case change 2 fixes).
  - **D4 (b), the probe, MEASURED** (Lab2, `labkit run` of `restart-foreground-android`; the Flutter driver temporarily patched to hold boot 2's `configure()` 1.5 s in `_configureWith`, so the new isolate listens after the old Activity's destroy; driver and plugin restored byte for byte afterwards). A first attempt patched the boot branch, which the scenario does not use (its configure is event-armed): no delay, so both builds passed and proved nothing.

    | Build | `run` | `stop-on-terminate-false` |
    |---|---|---|
    | change 1 alone (a throwaway: `9ccc569` plus an attach counter for the flag, the process-wide channel kept) | FAIL: `driverEvents -`, `eventAssertions configure@0=never-fired`, `headless false`; the fetch went to the old engine's sink: `W/FlutterJNI: Tried to send a platform message to Flutter, but FlutterJNI was detached from native C++. Could not send.` | FAIL, the same |
    | the fix | PASS: `driverEvents fetch=1`, the module's "not headless" line at the old detach | PASS |

    So the Diagnosis' SUSPECTED event-channel defect is MEASURED: with the flag fixed alone, a new screen that configures after the old one is destroyed loses its fetches. WO-002's scenario step (D4 a) is still WO-002's to build.
  - **Lab, MEASURED,** native `4.1.1-master-399765c`, plugin `9ccc569+dirty` (WO-011 merged; this order's tree):

    | Sweep / run | What | Result |
    |---|---|---|
    | `20261116-145336` | Flutter Android, the suite, FIX | **13/13** |
    | `labkit run`, Lab1 and Lab2 | `restart-foreground-android`, `recreate-foreground-android`, `headless-activity-destroyed` | PASS on both (the committed captures of these three are Lab2's runs) |

    - **By hand:** restart, both steps: the module's "not headless" line, then `onDestroy:afterSuper … headless=false`; `lifecycleTally onCreate=1,onResume=1,onStart=1` (no `headless:true`). Recreate, both steps: `headless=false`, no `HeadlessMode? true`. `headless-activity-destroyed` and `stop-on-terminate/finish`: `HeadlessMode? true` at the finish, `headless true`; `headless-kill` `flutterHeadlessInit 1`.
  - **Re-scored:** 208 captures, 26 not PASS, all React Native's WO-004 captures (`spec-changed`). Exactly this order's two reds turned green. selftest passes.
  - **Lab text:** the scenario's Flutter paragraph, in the past tense with WO-012's green and the probe. Three `// (WO-012)` anchors (comments only) were added after the sweeps.
  - **Not measured:** the cached-engine paths (D5's fallback, the re-attach) and the two-FlutterActivity back press: the lab host has one Activity class and no cached engine (Test plan).
  - **Reviewed (four lenses, three skeptics per finding): 21 findings, 17 upheld, all applied.** No code change: comments, the CHANGELOG's wording, lab text and this record.
    - **The probe, recorded in full.** It ran through `labkit run` with a temporarily patched driver, not a bare `adb` probe: a 1.5 s `await` in `_configureWith`'s closure, before `BackgroundFetch.configure`, for boot 2 only. The mutant (`9ccc569` with an attach counter around the detach's `setHeadless(true)`, its `setStreamHandler(null)` on the one process-wide channel kept) and the captures stayed in the session's scratchpad (`wo012/Module.mutant`, `wo012/d4b-*-captures`); they are not committed. The order, from those logcats:

      | Build / step | delayed configure | old `point=onDestroy` | boot 2 `- configure` | fetch | where it went |
      |---|---|---|---|---|---|
      | mutant `run` | 09.640 | 09.686 | 11.166 | 13.925 | old engine: FlutterJNI "detached … Could not send" |
      | mutant `stop-on-terminate-false` | 52.412 | 52.471 | 53.940 | 56.742 | the same |
      | fix `run` | 40.475 | 40.524 | 41.983 | 44.840 | the page, `app=event=fetch` |
      | fix `stop-on-terminate-false` | 23.252 | 23.319 | 24.763 | 27.558 | the page |

    - **Not observed:** the `MissingPluginException` report the Test plan predicted for the new isolate's lost listen did not appear in either mutant logcat. What is MEASURED is the outcome: the module's sink stayed the destroyed engine's. The lost-listen step stays INFERRED, and so does the reason for the missing report (in a debug build Flutter's widget inspector takes over error reporting as structured errors sent to the VM service: read in source, not run). The Test plan's red signature is now the FlutterJNI warning.
    - **Comments:** a detach marks the instance detached; only `onDetachedFromEngine` removes it (the plugin's comment and this Log said "drops"); the fallback applies whenever no attached instance is listening.
    - **The CHANGELOG:** `stopOnTerminate: false` without `enableHeadless` was stopped too, not sent to a headless task.
    - **Lab text left stale by the fix:** `recreate-foreground-android.yaml`'s Flutter paragraph (past tense, `at 11d53b8`, WO-012's green) and the Flutter host's class comment (`MainActivity.kt`); the restart paragraph's citations pinned to `11d53b8`, the red captures' commits named, "24-36 ms" from the committed captures, and the probe described as it ran.
    - **The record:** the Citation bases say the cited captures were replaced twice, and pin lab-file lines to `18e406a`; two Scope-check sentences match D5 (b)'s record. Since WO-011 the plugin has a Dart `test/` suite, but it cannot reach the Java module and there is still no Android JVM harness, so "no unit test" stands for that reason.
    - **Refuted (4),** among them: that the CHANGELOG must list every customer-visible change the Scope check records, and that the shared plugin stamp makes the probe builds indistinguishable.
    - All of these came after the sweeps; the Java edits are comments only.
- 2026-11-16 — **Committed** (Chris: "Commit both"): plugin `4cf0ae2` on `fix/WO-012-flutter-android-restart-ordering` (from `9ccc569`), and fetch-forge's branch of the same name (this Log, the lab text, the captures). Not merged; the `flutter/background_fetch` pointer moves when the plugin branch is merged, and the order is `done` then. It ships in 2.0.0 with WO-011 unless a 1.x branch carries it (D3).
