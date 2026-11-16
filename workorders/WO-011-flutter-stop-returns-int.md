---
id: WO-011
title: Flutter's stop() is Future<int> and resolves the background-fetch status, where the shared types declare Promise<boolean> and React Native and Capacitor resolve true
status: approved
created: 2026-11-16
platforms: [android, ios]
submodules: [flutter/background_fetch]
branch: fix/WO-011-flutter-stop-returns-int
issues: []
supersedes: []
---

> **Found by** WO-002's Flutter hosts. The red was predicted from source before the first Flutter sweep. It was then measured on Android and on both iOS channels, and left red on purpose as a finding (WO-002 Log, 2026-11-13 and 2026-11-14).
>
> **Recorded earlier, and set aside.** WO-006 noted the divergence from source on 2026-11-12, before any Flutter sweep: "Flutter's `stop` is `Future<int>` and resolves the status", at the same three lines (`WO-006-capacitor-void-returns.md:134-137`, commit `f495c5c`). It added "The Dart surface is not held to the TypeScript types" (`:137`) and put the point out of scope (`:165`). docforge has carried both types side by side since `c94ac6c` (2026-04-12; `docforge/db/BackgroundFetch.stop.yaml:41-42`). The lab holds Flutter to the types: Chris's 2026-11-13 decision (`WO-002-background-fetch-labkit.md:1193`; `labs/platforms/flutter/lab.yaml:61`), and api-smoke's own premise (`api-smoke.yaml:3-4, :29`). No record shows Chris addressing WO-006's line, so treating it as superseded for the lab is INFERRED. Whether it holds for the plugin is exactly D1.
>
> **Evidence markers:** **MEASURED** = seen in a lab capture; **PROVEN** = read in source; **INFERRED** = reasoning, not run; **NOT CHECKED** = not looked at; **SUSPECTED** = a defect inferred from source that no device run reproduced. Nothing here is SUSPECTED: the red is MEASURED on both OSes.
>
> **Citation bases:**
> - **Source, read 2026-11-16** at the commits the lab measured: Flutter `11d53b8` (1.7.0 plus Unreleased), React Native `8d9ab19`, Capacitor `3fa169a`, Cordova `368eb85`, types `dc4f5b8`. Native: `399765c` (TSBackgroundFetch 4.2.1, lab label `4.1.1-master-399765c`), read with `git -C native/background-fetch show 399765c:<path>` and cited `(399765c)`. Flutter paths are relative to `flutter/background_fetch/`. Its Android module is `android/src/main/java/com/transistorsoft/flutter/backgroundfetch/BackgroundFetchModule.java`, and its iOS plugin is `ios/background_fetch/Sources/background_fetch/BackgroundFetchPlugin.m`.
> - **Lab board (WO-002):**
>   - Flutter Android: sweeps `20261113-175923` and `20261113-183048`.
>   - Flutter iOS: `20261114-114801` and `20261114-115249` (FIX / FIX-SPM), then `20261114-123454` and `20261114-123933`. `ios-stop-start` was swept again in `20261115-195448` and `-200600`.
>   - Controls, React Native and Capacitor: `20261113-175459` (Android), `-175553` (iOS FIX), `-175657` (iOS FIX-SPM) and `20261115-195448`.
> - **Captures carry no sweep id** (`labs/harness/sweep.py:33`). I matched each capture cited here to its sweep by its run.json `timings.t0`, against that sweep's `board.json` in `labs/harness/.claims/` (gitignored). `run.json` and `summary.txt` are tracked. `logcat.txt` exists on disk only.

## Diagnosis

**MEASURED on Flutter Android** (Lab2 emulator-5582, Android 16; sweep `20261113-183048`, plugin `11d53b8`, clean). `labs/captures/flutter/api-smoke/FIX/run/summary.txt` fails on one row only: `apiAssertions stop#5.$=want-boolean-got-number, stop#7.$=want-boolean-got-number`. From that capture's `logcat.txt`:

```
:410  18:30:59.370 D/TSBackgroundFetch(11740): - stop: com.transistorsoft.fetchlab.smoke
:411  18:30:59.370 I/TSBackgroundFetch(11740): - cancel taskId=com.transistorsoft.fetchlab.smoke, jobId=-592898938
:413  18:30:59.371 I/flutter (11740): LAB app=ret id=stop#5 path=$ type=number value=2 parent=1
:417  18:30:59.385 D/TSBackgroundFetch(11740): - stop
:418  18:30:59.385 I/TSBackgroundFetch(11740): - cancel taskId=flutter_background_fetch, jobId=999
:420  18:30:59.386 I/flutter (11740): LAB app=ret id=stop#7 path=$ type=number value=2 parent=1
```

**MEASURED on Flutter iOS** (Simulator iOS 27.2, CocoaPods and Swift Package Manager):
- **api-smoke-ios** (Lab2; sweeps `20261114-123454` FIX and `-123933` FIX-SPM): `labs/captures/flutter-ios/api-smoke-ios/{FIX,FIX-SPM}/run/summary.txt` read the same row, and every other row is `ok`. In `FIX/run/logcat.txt`, `[TSBackgroundFetch stop] com.transistorsoft.fetchlab.task` is at `:36` and `app=ret id=stop#5 … type=number value=2` at `:40`. `[TSBackgroundFetch stop] (null)` is at `:45`, and `stop#7 … value=2` at `:47`.
- **ios-stop-start** (Lab1; sweeps `20261115-195448` FIX and `-200600` FIX-SPM): all four of `labs/captures/flutter-ios/ios-stop-start/{FIX,FIX-SPM}/{stopped,restarted}/summary.txt` read `apiAssertions stop#1.$=want-boolean-got-number`. Every metric of the stop and the restart passes: bgCancels, bgSubmits, driverEvents, springboardResults, and, in `restarted`, driverFinishes, legacyResults and sdkSubscribers. `FIX/stopped/logcat.txt:27` reads `app=ret id=stop#1 path=$ type=number value=2`.
- The four 2026-11-14 sweeps read the same reds in both scenarios (their `board.json`).

- **The SDK did the work. Only the value handed back is wrong.**
  - **Android, per call:** each stop's cancel is logged between its call and its return: `:411` for stop#5 (the smoke task) and `:418` for stop#7 (the fetch task). `jobsRegistered -592898938,999` passes, but it shows only that the SDK asked for the two jobs stop then cancels (`api-smoke.yaml:68-71`). `jobIds -` passes, but it cannot tell one stop from the other (`api-smoke.yaml:63`).
  - **iOS, per call** (`flutter-ios/api-smoke-ios/FIX/run/logcat.txt`; FIX-SPM has the same lines at the same numbers): stop#5's `[TSBGTask destroy] com.transistorsoft.fetchlab.task` and `cancelTaskRequestWithIdentifier: com.transistorsoft.fetchlab.task` are at `:37-38`, after its `[TSBackgroundFetch stop]` (`:36`). stop#7's `cancelTaskRequestWithIdentifier: com.transistorsoft.fetch` is at `:44`, just before the SDK's `[TSBackgroundFetch stop] (null)` (`:45`). That cancel comes from the plugin's listener removal (`BackgroundFetchPlugin.m:133-136`), which reaches `cancelBGAppRefresh` (`native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.m:324-333, :141-144 (399765c)`).
  - **iOS, end of step only:** `bgCancels com.transistorsoft.fetch=1,com.transistorsoft.fetchlab.task=1`, `sdkTasks -` and `sdkSubscribers -` pass on both channels. They confirm the step's end and cannot say which stop did it (`api-smoke-ios.yaml:37-40`).
- **The controls resolve `true` on the same native label** (MEASURED, `type=boolean value=true`):
  - React Native: Android `labs/captures/react-native/api-smoke/FIX/run/logcat.txt:647, :654`; iOS `react-native-ios/api-smoke-ios/FIX/run/logcat.txt:39, :46`.
  - Capacitor: Android `capacitor/api-smoke/FIX/run/logcat.txt:758, :773`; iOS `capacitor-ios/api-smoke-ios/FIX/run/logcat.txt:41, :48`.
  - ios-stop-start `stop#1`: React Native `react-native-ios/ios-stop-start/FIX/stopped/logcat.txt:26`; Capacitor `capacitor-ios/ios-stop-start/FIX/stopped/logcat.txt:28`.

**The code path (PROVEN):**
1. **Dart.** `lib/background_fetch.dart:524-527`: `static Future<int> stop([String? taskId])` awaits `_methodChannel.invokeMethod('stop', taskId)` into an `int` and returns it. The doc comment (`:492-523`) says what stop() halts and says nothing about the value.
2. **Android.** The dispatcher (`BackgroundFetchModule.java:125-126`) calls `stop` (`:166-170`). That calls the SDK's void `stop(taskId)` and answers `result.success(adapter.status())` (`:169`). The SDK's `status()` always returns `STATUS_AVAILABLE`, 2 (`native/background-fetch/android/tsbackgroundfetch/src/main/java/com/transistorsoft/tsbackgroundfetch/BackgroundFetch.java:213-217 (399765c)`). So on Android, stop() resolves the constant 2 whatever happened.
3. **iOS.** The dispatcher (`BackgroundFetchPlugin.m:78-79`) calls `stop:result:` (`:131-140`). That removes the plugin's listener when there is no taskId (`:133-136`), calls the SDK's void `stop:` (`:138`), then calls `[self status:result]` (`:139`). That answers `@(status)` from the SDK's asynchronous `status:` (`:142-146`). The value is UIApplication's `backgroundRefreshStatus`, read on the main queue (`native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.m:244-249 (399765c)`), which is the value `BackgroundFetch.status` returns.
4. **The SDK has nothing else to report.** `stop` is void on both OSes: `BackgroundFetch.java:140 (399765c)`, and `native/background-fetch/ios/TSBackgroundFetch/TSBackgroundFetch/TSBackgroundFetch.h:45 (399765c)`, implemented synchronously at `TSBackgroundFetch.m:355-366 (399765c)`.

So the int never describes the stop. On Android it is a constant. On iOS it is the authorization status, which the `status` getter (`lib/background_fetch.dart:534-537`) already gives.

**The contract (PROVEN):**
- **The types:** `types/background-fetch/src/BackgroundFetch.ts:203` declares `static stop(taskId?: string): Promise<boolean>;`, with "@returns A `Promise` resolving to `true` on success" (`:192`). The file is unchanged from `5c9685d`, the commit api-smoke cites, to `dc4f5b8`: `git log 5c9685d..dc4f5b8 -- src/BackgroundFetch.ts` is empty.
- **The lab's vocabulary:** `labs/harness/vocabulary.yaml:46` has `stop: {args: ["taskId?: string"], returns: boolean}`.
- **React Native** resolves `true` on both OSes (`RNBackgroundFetchModule.java:73`; `RNBackgroundFetch.mm:96`). Its TurboModule spec declares `Promise<boolean>` (`src/NativeBackgroundFetch.ts:7`), and its `index.d.ts:1-8` re-exports the types' class.
- **Capacitor, since WO-006,** declares `Promise<boolean>` (`src/index.ts:334`) and resolves `true` (`:339`).
- **Cordova is callback-style.** `stop(success?, failure?)` returns void (`src/typescript/index.ts:79-81`). Its native success sends the string `"OK"` on Android and null on iOS, never `true`: Android's bare `success()` (`CDVBackgroundFetch.java:88`) is cordova-android 15.0.0's `PluginResult(Status.OK)`, whose message is `"OK"`; iOS sends OK with a nil message (`CDVBackgroundFetch.m:106-107`), which arrives as null (WO-009). That is WO-009's scope.

**What Flutter's own docs promise is the int (PROVEN):**
- **docforge.** `docforge/db/BackgroundFetch.stop.yaml:42` has `dart: "static Future<int> stop([String? taskId])"`, on the line after the TypeScript `Promise<boolean>` (`:41`). The Flutter pages use the `dart` signature: Flutter's `code_lang` is `dart` (`docforge/scripts/generate_mkdocs.py:39`), and a non-native page shows `signatures[code_lang]`, falling back to `ts` only when it is missing (`:301`). That the live page shows it is INFERRED: I did not render it. The entry's description (`:3-9`) states no return value, and its Dart example ignores it (`:22-27`).
- **The package's `example/example.md`** types the value as an int. `example/example.md:99-101` has `BackgroundFetch.stop().then((int status) { print('[BackgroundFetch] stop success: $status'); });`.
- **The example app** prints the value without a type (`example/lib/main.dart:177-179`).
- **Nothing else mentions it.** docforge's Flutter examples page ignores the value (`docforge/src/static/flutter/examples.md:133-136`). The README links to the docs site and shows no stop() (`README.md:30-36`). No CHANGELOG entry mentions stop()'s return type (`grep -i stop CHANGELOG.md`).

**Not a regression (PROVEN).** `static Future<int> stop()`, resolving the native status, is in the plugin's first working commit, `b23280c` (2018-11-07). `65c3742` (2020-02-03) added the optional `taskId` and kept `Future<int>`.

**The scope is stop() alone (MEASURED, and PROVEN where the Simulator cannot measure).** In the same captures, every other Flutter hook passes:
- **status#2 and start#3** read `type=number value=2` on Android (`logcat.txt:368, :391`) and on iOS (`api-smoke-ios/FIX/run/logcat.txt:27, :30`). Dart's `Future<int>` (`:534`, `:478`) is the types' `BackgroundFetchStatus` (`BackgroundFetch.ts:183, :247`; vocabulary `:44-45`, wire `number`).
- **scheduleTask#4** reads `type=boolean value=true` on Android (`:409`). On the Simulator it rejects (`logcat.txt:34-35`), which is what api-smoke-ios asserts (`api-smoke-ios.yaml:78-80`). Its iOS resolve is PROVEN only: `result(@(YES))` (`BackgroundFetchPlugin.m:172`) under Dart's `Future<bool>` (`:575-577`). Android sends `result.success(true)` (`BackgroundFetchModule.java:188`).
- **configure#0 and registerHeadlessTask#1** are probes and read `function`. **finish#6** reads null.

**Chris's decision (2026-11-13).** WO-002's Log records it as "(1) api-smoke `stop#5`/`stop#7` stay red on Flutter, a finding: Dart's `stop()` is `Future<int>` (background_fetch.dart:524)." (`WO-002-background-fetch-labkit.md:1193`). On iOS, the reds were "Left red (Chris's Android decision)" (`:1241`). These are the Log's words. No record has Chris's own words in quotation marks: not the Log, `api-smoke.yaml:45` ("left red (Chris, 2026-11-13)"), `labs/platforms/flutter/lab.yaml:61`, nor commit `6b8fcbc`.

## Proposed fix

D1 decides whether there is a code fix at all.

**Under D1 (a) and D2 (b),** in the Flutter submodule:
- **`lib/background_fetch.dart`, `stop` (`:524-527`).** Declare `Future<bool>` and return the channel's value, as `scheduleTask` does (`:575-577`). Extend the doc comment (`:492-523`):
  - the value is `true` once the stop is made;
  - it does not say whether a task was stopped, which is WO-006 D2's meaning, approved 2026-11-13;
  - read `status` for the authorization status.
- **`BackgroundFetchModule.java`, `stop` (`:166-170`).** Answer `result.success(true)`, as `scheduleTask` does (`:188`).
- **`BackgroundFetchPlugin.m`, `stop:result:` (`:131-140`).** Answer `@(YES)` after the SDK's `stop:`, as `scheduleTask:` does (`:172`) and as React Native does (`RNBackgroundFetch.mm:95-96`). The status round trip is dropped.
- **The examples.** `example/example.md:99` and `example/lib/main.dart:177` read the value as a `bool`.
- **Anchors and limits.** Each change carries `// (WO-011)`. Nothing about what stop() stops changes (WO-013).

**In fetch-forge itself, not the submodule:**
- **docforge.** `docforge/db/BackgroundFetch.stop.yaml:42` becomes `static Future<bool> stop([String? taskId])`. Publish it when the release D3 picks is published, not before: docforge carries one Dart signature for every version.
- **Lab text:** see the Test plan. No assertion changes.

**The CHANGELOG entry** goes in the release D3 picks, with an OLD/NEW block as 0.7.0's breaking change has (`CHANGELOG.md:185`), for example:

> * [Changed] **Breaking:** `BackgroundFetch.stop()` returns `Future<bool>` (was `Future<int>`) and resolves `true`, as `@transistorsoft/background-fetch-types` declares and the React Native and Capacitor plugins do. The `int` was the background-fetch status, never a result of the stop: on Android it was always `2`. Read `BackgroundFetch.status` for the status. (WO-011)

Not "as the other Background Fetch SDKs do": Cordova's stop() returns void, and its native success sends the string `"OK"` on Android and null on iOS, never `true` (WO-009); the native SDKs' stop is void. "As the Capacitor plugin does" is true for customers only once Capacitor's WO-006 change is published: at `3fa169a` it sits under `## Unreleased` (`capacitor/background-fetch/CHANGELOG.md:9`), and `package.json` is still 8.1.4.

**Under D1 (b),** no code changes:
- `stop`'s doc comment says it resolves the status (iOS: `backgroundRefreshStatus`; Android: always `STATUS_AVAILABLE`) and nothing about the stop.
- `BackgroundFetch.stop.yaml` gains a Flutter `platform_description`. A platform's description replaces the shared one whole (`generate_mkdocs.py:287-289`), and the Dart example appears only where `@example example-1` sits in it (`stop.yaml:11`). So the Flutter entry repeats the shared description in full (`stop.yaml:3-11`, including `@example example-1`) and adds the sentence on the status, as the existing entries repeat then add (`AbstractConfig.enableHeadless.yaml:11-17`).
- A `[Docs] … (WO-011)` line goes in the CHANGELOG.
- The order's status becomes `wont-fix`, with Chris's reason in the Log.

**Decisions:**

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | stop()'s Dart return type | (a) `Future<bool>`, resolving `true`. It breaks Dart code that reads the int (see the Scope check). (b) Keep `Future<int>`, say so in the doc comment and docforge, and close `wont-fix`. That breaks nothing. Flutter's stop reds stay on the board for good (api-smoke and api-smoke-ios `stop#5`/`stop#7`, ios-stop-start `stop#1`), and the docs entry keeps giving Dart and TypeScript different types (`stop.yaml:41-42`). (b) is WO-006's earlier position, "The Dart surface is not held to the TypeScript types" (`WO-006-capacitor-void-returns.md:137`), which the lab no longer follows (see Found by). | (a). The int tells a caller nothing that `status` doesn't: it is a constant on Android and the authorization status on iOS. React Native and Capacitor resolve `true` on both OSes (MEASURED). |
| D2 | Under D1 (a), where the `true` comes from | (a) Dart returns `true` after the channel call. The natives keep sending the status, and Dart drops it. One file. (b) Both natives answer `true`, and Dart declares `Future<bool>` and forwards the value. Three files. Its cost: a customer's test double that mocks the method channel and answers `'stop'` with an int, as the natives do today, then fails with a TypeError at run time, where under (a) it keeps working (INFERRED; see the Scope check). | (b). That is the plugin's own pattern for `scheduleTask` (Dart `:575-577`, Android `:188`, iOS `:172`) and React Native's for `stop`. Under (a), `status` calls whose answer nothing reads would stay in Java and Objective-C. Dart and native ship in one pub package (`pubspec.yaml:13-20`), so the plugin's own halves cannot drift apart. A customer's double that stands in for the native half can, and (b) breaks it at run time. |
| D3 | Under D1 (a), which release carries it | (a) 2.0.0, with a Breaking section and OLD/NEW. (b) The next minor, 1.8.0, with a `[Changed]` line, as WO-006 D3 did for Capacitor. (c) A patch. | (a). WO-006 D3's grounds do not hold here: Capacitor's docs already promised `boolean`, and Flutter's promise `int` (docforge `:42`, `example/example.md:99`). Under pub's caret rule, `^1.7.0` means `<2.0.0` (pub's documented rule, not checked here). The documented install is `flutter pub add background_fetch` (`docforge/src/static/flutter/setup.md:8`), which writes a caret constraint such as `^1.7.0` (pub's documented behaviour, NOT CHECKED). So a 1.8.0 would reach every customer who installed that way on their next `pub upgrade`. In 1.6.0 a typedef kept code using the old name compiling (`CHANGELOG.md:23`). A return type has no such bridge. The next version number is not decided anywhere I found: Unreleased holds WO-001's iOS 15 requirement (`CHANGELOG.md:3-9`). See also "Under D3 (a), where the code waits" below the table. |
| D4 | A Dart unit test | (a) Add the plugin's first tests, under `test/`, with `flutter_test` as a dev_dependency (see the Test plan). (b) The lab only. | (a). The plugin has no tests (`CLAUDE.md:48`), and nothing compiles its signatures against the contract: Capacitor's preflight guard (WO-006 D4) has no Flutter equivalent. A dev_dependency does not reach customers (INFERRED from pub's rule). |

**Under D3 (a), where the code waits.** WO-006 and WO-008 were merged into their plugin's `master` as soon as they were verified (WO-006 Log, 2026-11-13 "Merged and pushed", `WO-006-capacitor-void-returns.md:225`; `WO-008-capacitor-ios-start-callback.md:200`). Flutter releases are cut from `master` on a `release-X.Y.Z` branch that is merged back (`39ec643` "Merge branch 'release-1.7.0'", `a998627` "Merge branch 'release-1.6.2'", both on `master`'s first-parent line). Merged the same way under `## Unreleased`, WO-011 would go out in whatever release is cut next, whatever its number: the route Not an acceptable shortcut forbids. So one of these, Chris's call:
- the fix branch stays unmerged until Chris decides the 2.0.0 release;
- or Chris first decides that `master`'s next release is 2.0.0, recorded as his decision or a CHANGELOG heading (the `pubspec.yaml` version moves only at release).

The choice decides what else can ship in a 1.x: WO-001's iOS 15 change, already on `master` (`0712e28`), and WO-012's fix (its D3 offers "an Android-only patch first", WO-012's Proposed fix). Under the first, they can go out in a 1.x from `master` while WO-011 waits on its branch. Under the second, they can ship in a 1.x only from a branch that excludes WO-011, or wait for 2.0.0.

## Scope check

- **Inside the plugin, only the method channel calls stop()** (PROVEN, `git grep`).
  - `lib/` never calls Dart's `stop`; it appears only in the doc comment (`:516`, `:521`).
  - The native handlers are reached only from their dispatchers (`BackgroundFetchModule.java:125-126`; `BackgroundFetchPlugin.m:78-79`).
  - On Android, a headless Dart task's stop() reaches the same handler: every engine's channel is handed to the module (`BackgroundFetchModule.java:71-79`).
- **Callers in the repo that read the value:**
  - `example/example.md:99` types it `int`. Under D1 (a) that line no longer compiles (INFERRED: `then` takes a `FutureOr<R> Function(bool)`, and `(int) => …` is not one).
  - `example/lib/main.dart:177-179` prints it without a type. It compiles either way (INFERRED).
  - The lab's driver passes the Future through and reports what it resolves (`labs/platforms/flutter/app/lib/main.dart:933`). Its code needs no change.
  - docforge's Dart example and its Flutter examples page ignore the value.
- **Customer code, under D1 (a).** INFERRED from Dart's typing, not compiled (see the Test plan).
  - **Stops compiling:**
    - `int s = await BackgroundFetch.stop();`
    - `.then((int s) …)`
    - assigning the value to an `int` field
    - `Future<int> f = BackgroundFetch.stop();`
    - passing `BackgroundFetch.stop` where a `Future<int> Function([String?])` is expected.
  - **Changes silently:** an untyped value compared with a `STATUS_*` int (`s == BackgroundFetch.STATUS_AVAILABLE`) compiles and is always false. Whether a customer's lint set flags it (`unrelated_type_equality_checks`) is NOT CHECKED.
  - **Breaks at run time, under D2 (b) only:** a test double that mocks the method channel (`com.transistorsoft/flutter_background_fetch/methods`, `lib/background_fetch.dart:7-8`) and answers `'stop'` with an int, as the natives do today. Dart then forwards that int into `Future<bool>`, as `scheduleTask` does (`:575-577`), and the downcast throws a TypeError. Under D2 (a) Dart ignores the channel's value, so the double keeps working. Any double that works today must answer an int, since `int status = await …` (`:525`) throws on anything else. `BackgroundFetch` has only static members, so mocking the channel is the usual way to double it (INFERRED, not run).
  - **Unaffected:**
    - `await BackgroundFetch.stop();`
    - a call whose result is never read
    - a `.then` whose untyped parameter is only printed.
- **bg-forge's Flutter Background Geolocation example** depends on `background_fetch: ^1.6.2` (`bg-forge/flutter/background_geolocation/example/pubspec.yaml:18`, at `97b1987`). It calls neither stop() nor start() (`git grep`). A 1.x minor would reach it; a 2.0.0 would not until its constraint moves.
- **Unchanged:**
  - what stop() stops, including the iOS listener removal (`:133-136`);
  - every other member;
  - the lab's scenarios and its driver code.

## Platform parity

- **Android:** affected, MEASURED (api-smoke, `20261113-183048`). The native answer is `BackgroundFetchModule.java:169`, and its value is the constant 2 (`BackgroundFetch.java:213-217 (399765c)`).
- **iOS:** affected, MEASURED on CocoaPods and Swift Package Manager: api-smoke-ios in `20261114-123454` and `-123933`, ios-stop-start in `20261115-195448` and `-200600`. The native answer is `BackgroundFetchPlugin.m:139`, whose value is set at `:144`.
- **Both OSes share the Dart declaration** (`:524`). Under D2 (b), the Java and Objective-C change in the same commit as the Dart. Under D2 (a), the one Dart change covers both. Either way, both OSes are swept before the order moves to `verify`.
- **The other bridges, for the record only:**
  - React Native conforms: MEASURED on both OSes.
  - Capacitor conforms since WO-006: MEASURED on both OSes.
  - Cordova is WO-009's.

## Test plan

- **The lab builds what the `flutter/background_fetch` checkout holds** (`labs/platforms/flutter/lab.yaml:25-27`; build.sh records its HEAD, dirty flag and content hash, `:65`, and run.py stamps them). Sweep with the fix branch checked out there.
  - WO-012 changes the same file (`BackgroundFetchModule.java`: WO-011 at `:166-170`, WO-012 at `setActivity`, `:92-115`). WO-006's green sweep ran with WO-007's tree under it (WO-006 Log, 2026-11-13). So the Log records each sweep's plugin stamp and any other order's tree in the checkout during it, as WO-006's Log did.
- **Android, variant FIX:**
  - api-smoke: `stop#5` and `stop#7` must read `type=boolean value=true`. `jobsRegistered -592898938,999`, `jobIds -` and every other row must be unchanged.
  - The rest of the Flutter Android suite must be unchanged. That is 12 of 13, with restart-foreground-android still red, if WO-012's fix is not in the swept tree; 13 of 13 if it is (WO-012's Test plan states the mirror).
- **iOS, FIX and FIX-SPM:**
  - api-smoke-ios: `stop#5` and `stop#7` must read `true`. bgCancels, sdkTasks and sdkSubscribers must be unchanged, and scheduleTask#4 must still reject.
  - ios-stop-start, `stopped` and `restarted`: `stop#1` must read `true`, and every metric must read as in `20261115-195448` / `-200600`.
  - Result: Flutter iOS 7 of 7 on each channel.
- **No scenario assertion changes, and none may** (see Not an acceptable shortcut). The lab change is text only, and it belongs to this work order, as WO-006's description edit did (its Verification). It is committed in fetch-forge, not in the submodule:
  - `api-smoke.yaml:42-47`, `labs/platforms/flutter/lab.yaml:61` and the driver comments `labs/platforms/flutter/app/lib/main.dart:930-932` (stop's) and `:940` (finish's, which ends "as it reads stop()'s int") name WO-011 and its red and green sweeps;
  - `main.dart:930-932` then says `Future<bool>`, not `Future<int>`, and drops "api-smoke reads it red on purpose". Its native clause becomes "both natives answer stop with `true`" under D2 (b); under D2 (a) the natives still answer the status, which Dart drops. `:940` no longer cites stop() as a red;
  - `api-smoke-ios.yaml` and `ios-stop-start.yaml` get a dated Flutter line. Today they do not record this red; only WO-002's Log does.
- **Unit tests (D4 a):** two files, each with a group named `WO-011 stop()`, so that the guard's compile error at `11d53b8` does not stop the mock test's file from loading and hide its red:
  - `test/background_fetch_test.dart`: with the method channel mocked to answer `true`, `stop()` and `stop('id')` resolve `true`, and send `'stop'` with `null` and with `'id'`. At `11d53b8` it should fail with a TypeError, `true` into the `int` (`:525`) (INFERRED; record it).
  - `test/stop_signature_test.dart`, a static guard: assign `BackgroundFetch.stop` to a `Future<bool> Function([String?])`. It should fail to compile at `11d53b8` (INFERRED; record it).

  Record both reds at `11d53b8` first, then the green on the fix, with `flutter test` run in the plugin. Under D2 (b) the mock checks only the Dart half; the lab checks the natives.
- **`flutter analyze`: record the baseline at `11d53b8` first.** `analysis_options.yaml` includes `package:lints/core.yaml`, but `pubspec.yaml` declares no `lints` and no dev_dependencies at all (`:9-11`), so the analyzer may already report the include as not found (INFERRED; the baseline is NOT CHECKED). The fix must add no new issue to that baseline. Making the analysis clean is not this order's work.
- **Customer patterns.** Compile the Scope check's patterns on scratch files against the fix, to replace INFERRED with MEASURED. Record which ones fail, and whether the `==` compiles and is linted. Also compile, for the shortcut bullet on `Future<dynamic>` / `Future<Object>`, `int s = await stop()`, `.then((int s) …)`, `Future<int> f = stop()` and the tear-off against each declaration, and run `int s = await stop()` under `Future<dynamic>` with the channel answering `true`. Under D2 (b), also run with `flutter test` a scratch test whose channel mock answers `'stop'` with an int, to measure the run-time break the Scope check predicts.
- **Native unit tests:** neither native side has a suite, and this order adds none.

## Out of scope

- **WO-009** (`WO-009-cordova-callback-api.md`): Cordova's JS API is callback-style. start, stop, status and scheduleTask return nothing where the types declare Promises. `stop(taskId)` throws on iOS and cancels every task on Android. On iOS, start's native half is unordered against a later stop.
- **WO-010** (`WO-010-cordova-android-restart-ordering.md`): Cordova Android fails restart-foreground-android. When the app restarts itself, the old Activity's destroy arrives after the new one resumes, and the plugin goes headless with the new page on screen: it does not act on the current Activity alone (WO-007's rule).
- **WO-012** (`WO-012-flutter-android-restart-ordering.md`): Flutter Android fails restart-foreground-android. The plugin does not act on the current Activity alone, and its EventChannel is held process-wide rather than on the plugin instance.
- **WO-013** (`WO-013-stop-no-arg-scope.md`): stop() with no argument means different things on Android. There React Native and Capacitor stop only their own fetch task, while Cordova and Flutter cancel every task, as the types' doc says. On iOS all four bridges cancel every task. **This order must not change what stop() stops, with or without a taskId.**
- **finish():** Dart declares `Future<void>` (`:585`), though both natives answer `true` (`BackgroundFetchModule.java:182`; `BackgroundFetchPlugin.m:151`). A typed caller is handed nothing, so it conforms. Not changed.
- **registerHeadlessTask():** Dart declares `Future<bool>` (`:629`) where the types declare `void` (`BackgroundFetch.ts:283`). A value where void is declared breaks no caller, and the lab only probes it. Not changed.
- **status:** a getter that takes no callback (`:534`), where the types declare `status(callback?)` (`BackgroundFetch.ts:247`). The driver aliases it (`main.dart:934-935`). Not changed.
- **Having the SDK report what stop() stopped** (WO-006 D2's option b) would need both natives to report it, and every bridge to follow.
- **The example's stale tests:** `example/test/widget_test.dart` and `example/test_driver/main_test.dart` are template tests that exercise nothing of the plugin.

## Not an acceptable shortcut

- **A Flutter `platform_expect` that accepts the int.** Before D1, it is a shortcut: it would turn a finding green with nothing fixed. The lab refuses it anyway, on three counts:
  - labkit refuses `apiAssertions` in any `expect` or `platform_expect` block ("a scenario may not restate or relax it", `labs/harness/labkit.py:3004-3008`);
  - `platform_expect` overrides metric rows only, never hooks (`expectations()`, `:4968-4974`);
  - a `returns` type that the types do not declare is refused ("would launder a divergence; write a work order instead", `:1270-1284`).

  Under D1 (b) it is still not the right outcome. The red is then the true reading: Flutter diverges from the contract by decision. It stays, and `api-smoke.yaml` names WO-011 `wont-fix`. If the standing red costs too much on the board, WO-002 could add a new marker for an accepted divergence, one that pins a value. The lab's existing per-platform marker, a dated `not_applicable` (next bullet), is not it: it excuses the whole scenario and pins nothing. The new marker would have to pin Flutter's exact value (`number`, one of 0/1/2), so that a further drift still reads red. That is lab design for WO-002 after D1, not a way to close this order.
- **`not_applicable: flutter`, with flutter dropped from `platforms`,** on api-smoke, api-smoke-ios or ios-stop-start. labkit checks only that an excusal accounts for the platform, does not overlap `platforms`, names a known platform or OS and opens with a date (`labs/harness/labkit.py:3017-3040`); `applicability()` then reads the whole scenario n/a (`:3178-3182`). The lab did exactly this for Flutter on api-smoke-ios and ios-stop-start until its iOS slice (`6b8fcbc`). It turns a finding into n/a, not green, and it stops scoring every other Flutter hook and metric in those scenarios: status, start, scheduleTask and finish; jobsRegistered and jobIds; bgSubmits, taskSubmitErrors, bgCancels and sdkTasks; and ios-stop-start's whole stop and restart reading (springboardResults, driverEvents, legacyResults, and Flutter's `sdkSubscribers` platform_expect, `ios-stop-start.yaml:80`). The sibling orders name the same shortcut (WO-007, WO-009, WO-010 and WO-012, each under Not an acceptable shortcut).
- **`outcome: resolved` on the stop hooks.** labkit allows it (`:1186-1189`), but hooks have no per-platform key except `absent_on` (`HOOK_KEYS_API`, `:109-111`). It would therefore drop the boolean check on every bridge, including the lab's check on WO-006's Capacitor stop fix (`api-smoke.yaml:25-29`). When Chris made the status anchors neutral, he kept ios-stop-start's stop and start return checks (`WO-002-background-fetch-labkit.md:1278`). api-smoke holds every bridge to the types (`api-smoke.yaml:29`).
- **`absent_on: flutter`.** stop() is present, and `absent_on` reads `absent` there and anything else red (`api-smoke.yaml:15-16`). So it would not even turn green, and it would claim Flutter has no stop().
- **Mapping the int to `true` in the lab's driver** (`main.dart:933`). The lab would then measure its own driver. The driver reports what the published Dart surface hands a caller (`lab.yaml:61`; `main.dart:930-932`).
- **Changing the types** to `Promise<boolean | number>`, or to a per-platform type. React Native is typed by them and resolves `true`, and so does Capacitor.
- **Changing the Dart declaration and forwarding the channel's value while the natives still send the int.** The implicit `int` to `bool` downcast throws, so stop() fails with a TypeError on both OSes (INFERRED). Under D2 (b), the three files land in one commit. Under D2 (a), Dart returns `true` and reads nothing from the channel.
- **Declaring `Future<dynamic>` or `Future<Object>`.** The plugin requires Dart 3 (`pubspec.yaml:7`, `sdk: ^3.6.0`), whose null-safe typing allows an implicit downcast only from `dynamic`. Both are INFERRED from that rule, not compiled (see the Test plan's Customer patterns):
  - **`Future<dynamic>`:** `int s = await stop()` still compiles, and once the natives send `true` it throws a TypeError at run time. `.then((int s) …)` (the `example/example.md:99` pattern), `Future<int> f = stop()` and the tear-off still fail to compile. So it hides part of the break and moves part of it, the await-into-`int` pattern alone, from compile time to run time, and api-smoke would go green on a surface that states no type.
  - **`Future<Object>`:** `int s = await stop()` fails to compile, as every other int-typed caller does, just as under D1 (a). It breaks the same callers `Future<bool>` breaks, and states `Object` where the types declare a boolean.
- **Resolving `status == STATUS_AVAILABLE` as the boolean.** That mixes up authorization with the stop. It is always `true` on Android, and on iOS it is `false` after a successful stop whenever Background App Refresh is off. No other bridge reports that.
- **Resolving `false` when nothing was stopped.** The SDK cannot say whether anything was (see Out of scope).
- **A new boolean method beside the int `stop()`.** api-smoke calls `stop`, so it stays red, and the Dart surface gains a member the types do not have.
- **`@Deprecated` on `stop()` to announce the change.** The annotation marks the member, not its return type, so it would tell customers to stop calling stop().
- **Shipping D1 (a) in a minor or a patch as "a bug fix".** Flutter's docs and the package's `example/example.md` promise `int`, so typed callers break at compile time. That is D3, Chris's call. Under D3 (a), merging the fix into the plugin's `master` under `## Unreleased` before the 2.0.0 release is decided reaches the same result (see "Under D3 (a), where the code waits").
- **Publishing docforge's new Dart signature before the release ships.** Readers on 1.x would see a signature their package does not have.
- **Fixing Android first because api-smoke is Android's.** Both OSes read red, and api-smoke-ios and ios-stop-start must go green on both channels in the same change.

## Verification

- [x] D1–D4 decided
- [ ] (D4 a) both `(WO-011)` test files red at `11d53b8` (the mock test's TypeError and the guard's compile error, each recorded), green on the fix; `flutter test` passes; `flutter analyze` adds no issue to the baseline recorded at `11d53b8`
- [ ] Flutter Android `api-smoke` green on the fix: `stop#5` and `stop#7` read boolean `true`; `jobsRegistered` and `jobIds` unchanged; the suite otherwise 12/13 (restart-foreground-android is WO-012's), or 13/13 if WO-012's fix is in the swept tree; the Log records the plugin stamp and any other order's tree under each sweep
- [ ] Flutter iOS `api-smoke-ios` green on FIX and FIX-SPM; `stop#5` and `stop#7` read `true`; the BGTaskScheduler rows unchanged
- [ ] Flutter iOS `ios-stop-start` green on FIX and FIX-SPM: `stop#1` reads `true` in `stopped` and `restarted`; every metric unchanged
- [ ] Platform parity: both OSes changed in one commit (D2) and both measured green; React Native and Capacitor untouched
- [ ] Customer patterns from the Scope check compiled against the fix, with the `Future<dynamic>` / `Future<Object>` checks and (D2 b) the int-answering channel mock run; results recorded in the Log
- [ ] `example/example.md:99` and `example/lib/main.dart:177` read a `bool`
- [ ] docforge `BackgroundFetch.stop.yaml:42` updated when the D3 release is published
- [ ] Lab text names WO-011, with the red and green sweeps: `api-smoke.yaml:42-47`, `lab.yaml:61`, `main.dart:930-932` and `:940`, and dated Flutter lines in `api-smoke-ios.yaml` and `ios-stop-start.yaml`
- [ ] CHANGELOG entry carries `(WO-011)`, in the release D3 picks; under D3 (a), the branch merged only as Chris decided (where the code waits)
- [ ] Under D1 (b) instead: doc comment, docforge `platform_description` (the shared description in full, with `@example example-1`, plus the status sentence) and a `[Docs] (WO-011)` line; status `wont-fix` with Chris's reason in the Log; the reds left in place, with their descriptions naming WO-011

## Log

- 2026-11-16 — **Created (draft)** from WO-002's lab board.
  - **Measured (read from the captures):**
    - Flutter Android: api-smoke `stop#5` and `stop#7` read `want-boolean-got-number`, in `labs/captures/flutter/api-smoke/FIX/run` (sweep `20261113-183048`; the first sweep, `20261113-175923`, read the same).
    - Flutter iOS: api-smoke-ios, the same two hooks, in `labs/captures/flutter-ios/api-smoke-ios/{FIX,FIX-SPM}/run` (`20261114-123454`, `-123933`).
    - Flutter iOS: ios-stop-start `stop#1`, in `labs/captures/flutter-ios/ios-stop-start/{FIX,FIX-SPM}/{stopped,restarted}` (`20261115-195448`, `-200600`; the 2026-11-14 sweeps read the same).
    - Controls: React Native and Capacitor `true` (`20261113-175459`, `-175553`, `-175657`, `20261115-195448`).
    - Each capture was matched to its sweep by `timings.t0` in `board.json`.
  - **Read while writing:**
    - the Dart, Java and Objective-C stop paths at `11d53b8`, and the SDK's `status` and `stop` at `399765c`;
    - the types, the vocabulary and the React Native, Capacitor and Cordova stops;
    - docforge, the example app, the CHANGELOG and stop()'s history to `b23280c`;
    - labkit's refusal rules for `apiAssertions` and `returns`.
  - **Not found:** Chris's decision in his own quoted words. The Log's wording is quoted instead.
  - **Not done yet:** no branch and no code. The customer patterns have not been compiled. Nothing run.
  - **Reviewed (three lenses, three skeptics per finding): 23 findings, 21 upheld.** All 21 applied; nothing compiled or run for them. The substantive changes:
    - WO-006's 2026-11-12 source record of the divergence, and its "not held to the TypeScript types" line, cited in Found by and as D1 (b)'s earlier position. Superseded for the lab is INFERRED, not claimed as Chris's.
    - iOS stop evidence is now per call (`logcat.txt:36-38`, `:44-45`); the tallies only confirm the step's end, and `jobsRegistered` shows registration only.
    - The `Future<dynamic>` / `Future<Object>` shortcut split: `Object` fails to compile like D1 (a); `dynamic` moves only the await-into-`int` pattern to run time. Both go into the compile check.
    - A new shortcut: a dated `not_applicable: flutter`, which makes every Flutter cell n/a; D1 (b)'s value-pinning marker is distinguished from it.
    - D3 (a) now says where the code waits until 2.0.0, and what that means for WO-001 and WO-012 shipping in a 1.x. Chris's call.
    - D2 (b)'s cost: a customer's channel mock that answers `'stop'` with an int breaks at run time.
    - D1 and the CHANGELOG line narrowed to React Native and Capacitor, with Capacitor's `true` still unreleased.
    - D1 (b)'s docforge entry repeats the shared description in full, since a `platform_description` replaces it.
    - Test plan: 12/13 or 13/13 depending on WO-012's tree, with sweep stamps logged; `main.dart:940` added, with `:930-932`'s wording per D2; the guard and the mock test in separate files; `flutter analyze` judged against a baseline at `11d53b8`; the caret claim relabelled NOT CHECKED.
  - **Cross-checked with WO-009..WO-013 (2026-11-16):**
    - Out of scope: WO-010 is the self-restart (the old destroy after the new resume), not a recreation, which is green on Cordova; WO-009's unordered start is iOS-only; WO-013's split is Android-only, and on iOS all four bridges cancel every task.
    - Cordova's native stop success sends `"OK"` on Android and null on iOS, not "no value" (cordova-android 15.0.0 `CallbackContext.success()`; WO-009).
    - Sibling citations name the section, not a line (those orders are being edited). The release-branch flow rests on the merges `39ec643` and `a998627`; the plugin's `CLAUDE.md:72-76` is only the checklist and is no longer cited.
    - `api-smoke-ios.yaml`'s scheduleTask hook is now `:78-80` (one comment line added above it). No step here pointed WO-002's open items at this order or corrected its sweep citations: WO-002's 2026-11-16 entry does that.
- 2026-11-16 — **Approved by Chris** ("approve all WO 9-12"), with the recommended decisions: D1 (a) `Future<bool>` resolving `true`; D2 (b) both natives answer `true` and Dart forwards it; D3 (a) 2.0.0, with a Breaking section; D4 (a) the plugin's first Dart unit tests. Status `approved`. Chris chose to take the orders one at a time: this one follows WO-010.
