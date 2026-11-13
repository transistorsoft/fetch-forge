---
id: WO-008
title: Capacitor iOS start() resolves undefined, never the BackgroundFetchStatus, and cannot reject — the 7.1.0 Swift rewrite declared it CAPPluginReturnCallback
status: done
created: 2026-11-13
platforms: [ios]
submodules: [capacitor/background-fetch]
branch: fix/WO-008-capacitor-ios-start-promise
issues: []
supersedes: []
---

> **Found by** WO-002's Capacitor iOS host on its first sweeps. This is WO-002 suspected defect 1
> (`WO-002-background-fetch-labkit.md:61`), predicted from source before the host existed and now measured red on
> both iOS channels.
>
> **Evidence markers:** **MEASURED** = seen in a capture; **PROVEN** = read in source; **INFERRED** = reasoning, not
> run; **NOT CHECKED** = not looked at.
>
> **Citation bases:**
> - **Plugin:** Capacitor `4d431c1` (fetch-forge `master`'s pointer; 8.1.4 plus Unreleased).
> - **Capacitor runtime:** `@capacitor/core` and `@capacitor/ios` 8.5.3, the lab host's copies
>   (`labs/platforms/capacitor/app/node_modules/@capacitor/`).
> - **Types:** `types/background-fetch` `dc4f5b8`.
> - **Captures:** sweeps `20261113-122423` (FIX, CocoaPods) and `20261113-122851` (FIX-SPM), Simulator iOS 27.2,
>   native label `4.1.1-master-399765c` (TSBackgroundFetch 4.2.1).

## Diagnosis

**MEASURED on Capacitor iOS, both channels.** Two hooks read red, and only these:
- `api-smoke-ios` `run`: `apiAssertions start#3.$=want-number-got-undefined`.
- `ios-stop-start` `restarted`: `apiAssertions start#2.$=want-number-got-undefined`.

The capture lines (`labs/captures/capacitor-ios/api-smoke-ios/FIX/run/logcat.txt:28-30`):

```
12:27:15.181 I/TSBackgroundFetch( 1865): [TSBackgroundFetch start] (null)
12:27:15.181 I/LAB     ( 1865): app=api id=start#3 outcome=ok
12:27:15.181 I/LAB     ( 1865): app=ret id=start#3 path=$ type=undefined parent=1
```

- **The promise resolves, with undefined.** It does not reject.
- **It resolves before native has started.** In `ios-stop-start/FIX/restarted/logcat.txt:29-32`, `app=ret id=start#2 …
  type=undefined` is at 21.966 and `[TSBackgroundFetch start]` at 21.967.
- **The native start still runs.** In the `restarted` step every other row passes on both channels:
  - `bgSubmits appRefresh=3` and `sdkSubscribers capacitor-background-fetch`;
  - the delivery `app=event=fetch` (`:49`) and the finish (`:57`);
  - `legacyFetchComplete result=newData` (`:55`) and `RetrievedNewData`.
- **React Native iOS reads a number on the same sweeps**: `app=ret id=start#3 … type=number value=2`
  (`labs/captures/react-native-ios/api-smoke-ios/FIX*/run/logcat.txt`).
- **Capacitor Android reads a number too** (`labs/captures/capacitor/api-smoke/FIX/run/logcat.txt`:
  `start#3 … type=number value=2`).

**The code path (PROVEN):**
1. **The declaration.** `BackgroundFetchPlugin.swift:13` declares
   `CAPPluginMethod(name: "start", returnType: CAPPluginReturnCallback)`. Every other method is
   `CAPPluginReturnPromise` (`:12`, `:14-17`).
2. **What @capacitor/core does with it.** For a method whose header's `rtype` is not `promise`, it builds
   `(options, callback) => cap.nativeCallback(…)` (`core/dist/index.js:93-97`). `nativeCallback` returns
   `cap.toNative(…, {callback})` (`ios/…/assets/native-bridge.js:991-998`). With no callback function, `toNative`
   stores nothing and returns the string callbackId `'-1'` (`:891-913`).
3. **Why it resolves.** Every plugin method is wrapped: `loadPluginImplementation().then(… return fn(...args))`
   (`core/dist/index.js:111-128`). So `NativeModule.start()` is a Promise of `'-1'`.
4. **The wrapper.** `src/index.ts:299-307` does `NativeModule.start().then((result) => resolve(result.status))`.
   `'-1'.status` is undefined, so it resolves undefined, as soon as the call is posted.
5. **Native's answer is lost.** `start(_:)` (`BackgroundFetchPlugin.swift:67-81`) was written for a promise:
   - success: `call.resolve(["status": status.rawValue])` (`:77`);
   - failure: `call.reject(…)` when the SDK's start errors (`:75`) or the status is not available (`:81`).

   With callbackId `'-1'` there is no stored callback, so neither ever reaches JavaScript. INFERRED for the reject
   path: the Simulator's status is always `available`, so no capture rejects.

**What the types declare (PROVEN):** `types/background-fetch/src/BackgroundFetch.ts:183`,
`static start(): Promise<BackgroundFetchStatus>`. Capacitor's own `src/index.ts:299` says the same.

**A regression, from 7.1.0 (PROVEN).** Before the Swift rewrite, the Objective-C plugin declared
`CAP_PLUGIN_METHOD(start, CAPPluginReturnPromise)` (`06cf615^:ios/Plugin/BackgroundFetchPlugin.m:8`).
- `06cf615` "[iOS] Implement Swift Package Manager (SPM)" (2025-07-31) replaced it with the Swift `pluginMethods`
  array, where `start` became `CAPPluginReturnCallback`.
- `git log -S CAPPluginReturnCallback` names only that commit.
- It shipped in 7.1.0 (`CHANGELOG.md:34-35`) and is in every 8.x release. 7.0.1 and earlier are not affected.
- Why the rewrite chose the callback type is not recorded. Nothing in `start(_:)` uses callback semantics (no
  `keepAlive`, a single resolve), so it reads as a slip (INFERRED).

**Who it reaches (INFERRED).**
- Any iOS app that reads `start()`'s status gets undefined.
- Any app that relies on `start()` rejecting, when Background App Refresh is off (denied or restricted), gets a
  resolve with undefined instead. Its `catch` never runs.
- An `await BackgroundFetch.start()` with nothing read after it, as in the plugin's own example
  (`example/src/app/home/home.page.ts:88`), sees no difference, except that it returns before native has started.

## Proposed fix

Change the declaration at `BackgroundFetchPlugin.swift:13` to `CAPPluginReturnPromise`, as it was before 7.1.0 and as
every other method is.
- `start(_:)` already resolves `{status}` and rejects: no other change.
- `src/index.ts` needs no change. It already reads `result.status` from a promise, as it does on Android.
- Anchor `// (WO-008)` at the declaration.
- An `[iOS]` CHANGELOG entry carrying `(WO-008)`, for example:

> * [iOS] `start()` resolves the `BackgroundFetchStatus` again, and rejects when the status is not available, as
>   before 7.1.0; since 7.1.0 it resolved `undefined` and could not reject. (WO-008)

**Decisions:**

| | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Where to fix | (a) The Swift declaration (`:13`) → `CAPPluginReturnPromise`. (b) The JS wrapper passes a callback to the callback-type call. | (a). One word, restoring 7.0.x and matching Android and every other method. (b) keeps a callback-type method where the types promise one, and rewrites the wrapper for one platform. |
| D2 | The rejection | (a) Restore the native reject as it is (`:75`, `:81`: the status's raw value or the SDK's message, which the wrapper passes on as `error.message`). (b) Make iOS resolve the status even when it is not available, as Android always resolves. | (a) for this order: it is what 7.0.x did and what the Swift code already says. Whether iOS and Android should agree on rejecting is its own question: Android's `start` never rejects (`BackgroundFetchPlugin.java:102-110`). |
| D3 | Where it ships | (a) The plugin's next release, beside WO-001, WO-006 and WO-007. (b) A patch first. | (a), unless you want it sooner: the Unreleased section already carries the other three. |

## Scope check

- **Callers of the plugin's `start`:** only the JS wrapper (`src/index.ts:299-307`), which already expects a promise.
  The plugin's example awaits it and ignores the value (`example/src/app/home/home.page.ts:88`).
- **Customers:** an app that has handled `start()` resolving undefined is unaffected; one that reads the status
  starts getting it. An app on iOS with Background App Refresh off now sees `start()` REJECT, where since 7.1.0 it
  resolved: a `try`-less `await BackgroundFetch.start()` would throw there. That is 7.0.x's behaviour and the types'
  contract, and the CHANGELOG line should say so.
- **The native SDK and the other methods:** unchanged.
- **The lab:** `api-smoke-ios` and `ios-stop-start` `restarted` expect a number in `[0, 1, 2]`; nothing else reads
  `start`'s value on iOS.

## Platform parity

- **iOS (Capacitor): affected.** MEASURED on both channels (above); `BackgroundFetchPlugin.swift:13`.
- **Android (Capacitor): not affected.** `@PluginMethod public void start` (`BackgroundFetchPlugin.java:102-110`)
  has the default promise return type and resolves `{status}`. MEASURED: Capacitor Android `api-smoke` `start#3`
  reads `type=number value=2` (sweep `20261113-095342`).
- **The other bridges, for the record, not in scope:**
  - **React Native iOS: not affected.** `RCT_EXPORT_METHOD(start:…)` resolves `@(status)`
    (`ios/RNBackgroundFetch/RNBackgroundFetch.mm:65-75`). MEASURED: `start#3 … type=number value=2` on FIX and
    FIX-SPM.
  - **Cordova:** a callback API (`start(success?, failure?)`), so the question does not arise in this form. NOT
    CHECKED beyond that.
  - **Flutter:** iOS `start` NOT CHECKED.

## Test plan

- **Red before the fix:** already MEASURED (sweeps `20261113-122423`, `20261113-122851`).
- **Lab, on the fix branch, Capacitor iOS, FIX and FIX-SPM:**
  - `api-smoke-ios`: `start#3` reads `type=number`, value 2; every other row unchanged.
  - `ios-stop-start` `restarted`: `start#2` reads a number; the delivery rows unchanged.
  - The whole iOS set: Capacitor 7/7 on both channels, React Native 7/7 unchanged.
- **The ordering:** with a promise, the `ret` line comes after `[TSBackgroundFetch start]`, since the resolve is native's.
- **The reject path:** not testable on the Simulator (its status is always available). On a device with Background
  App Refresh turned off: `start()` rejects with the status's raw value. NOT YET DESIGNED as a lab step; optional.
- **No JS unit test:** the plugin has no JS test runner (as WO-006 found).

## Out of scope

- **Android's `start()` after `stop()`** doing nothing (WO-002 suspected defect 2).
- **Whether iOS and Android should agree** on `start()` rejecting when fetch is unavailable (D2's option b).
- **The wrapper's rejection with `error.message`,** a string, not an Error (`src/index.ts:304`; the same in every
  method, as WO-006 noted).
- **The plugin's `deinit` calling `removeListener`** (`BackgroundFetchPlugin.swift:37-39`), which deletes the persisted
  subscriber if a background scene disconnect frees the bridge (INFERRED, WO-002 Log 2026-11-13).
- **The iOS docs note** that code after `await BackgroundFetch.finish()` may not run until the next wake (WO-002 Log).

## Not an acceptable shortcut

- **Relaxing the lab:** a Capacitor `platform_expect`, `absent_on`, or `outcome: resolved` on `start`. `api-smoke-ios`
  holds every bridge to the types; React Native and Capacitor Android meet them.
- **Calling `status()` in the JS wrapper after `start()` and resolving that.**
  - It fabricates the value.
  - It still resolves before native has started.
  - It still never rejects.
  - It leaves the wrong declaration in place.
- **Changing the types** to `Promise<BackgroundFetchStatus | void>` or similar. React Native and Android return the
  status, and so did Capacitor iOS before 7.1.0.
- **Keeping the callback type and passing a callback from the wrapper** (D1 b). It works, but keeps the one
  callback-type method among promises, for no behaviour the code needs.
- **`CAPPluginReturnNone`.** It would drop the resolve altogether.

## Verification

- [x] D1–D3 decided (2026-11-13: all three as recommended)
- [x] Capacitor iOS `api-smoke-ios` and `ios-stop-start` green on the fix, FIX and FIX-SPM; the `start` ret is a number (sweeps `20261113-124007`, `20261113-124436`)
- [x] Capacitor iOS 7/7 on both channels; React Native iOS 7/7 unchanged (React Native untouched; its 7/7 is sweeps `20261113-114636`, `-115848`)
- [x] `// (WO-008)` at the declaration; the CHANGELOG `[iOS]` entry carries `(WO-008)` and says that start() rejects
  again when unavailable
- [x] `WO-002-background-fetch-labkit.md:61` (suspected defect 1) points to WO-008
- [x] Platform parity: Android not affected (MEASURED); React Native iOS not affected (MEASURED)

## Log

- 2026-11-13 — **Created (draft)** from WO-002's Capacitor iOS host.
  - **Measured:** the reds in sweeps `20261113-122423` (FIX) and `20261113-122851` (FIX-SPM).
  - **Read while writing:** the mechanism, in @capacitor/core 8.5.3's `createPluginMethod` and
    `createPluginMethodWrapper`, and native-bridge.js's `nativeCallback`/`toNative`.
  - **Dated:** the regression, to `06cf615` / 7.1.0, from the Objective-C declaration before it.
  - **Not done yet:** no branch, no code; the reject path not measured.
- 2026-11-13 — **Approved by Chris:** D1 (a) the Swift declaration, D2 (a) restore the native reject, D3 (a) the next release. Status `in-progress`. Re-verified before code: the plugin's `origin/master` is still `4d431c1`, and `BackgroundFetchPlugin.swift:13` still declares `start` `CAPPluginReturnCallback`. Branch `fix/WO-008-capacitor-ios-start-promise` in the Capacitor submodule, from `4d431c1`.
- 2026-11-13 — **Fixed and proven (uncommitted).** `BackgroundFetchPlugin.swift:13`: `start` is `CAPPluginReturnPromise`, with the `// (WO-008)` anchor; an `[iOS]` CHANGELOG entry under Unreleased.
  - **Sweeps `20261113-124007` (FIX) and `20261113-124436` (FIX-SPM), plugin `4d431c1+dirty`: Capacitor iOS 7/7 on both.**
  - `api-smoke-ios` `start#3` and `ios-stop-start` `restarted` `start#2` read `type=number value=2`.
  - The order is now native's: `[TSBackgroundFetch start] (null)`, then `app=api id=start#… outcome=ok`, then the `ret`.
  - The reject path stays unmeasured: the Simulator's status is always available.
- 2026-11-13 — **Committed** `af7deec` on `fix/WO-008-capacitor-ios-start-promise` (Capacitor plugin, from `4d431c1`; not merged or pushed). Every Verification box is ticked. Status `verify` until the branch merges into the plugin's `master`, then `done`.
- 2026-11-13 — **Merged and pushed.** The plugin's `master` is `3fa169a`: WO-008's merge, `--no-ff`, pushed to GitHub (`4d431c1..3fa169a`). fetch-forge's capacitor pointer moves `4d431c1` → `3fa169a`. Every Verification box is ticked, so the status is `done`. It ships in the plugin's next release, beside WO-001, WO-006 and WO-007 (D3). The device check of the reject path stays optional.
