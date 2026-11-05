---
id: WO-001
title: iOS distribution moves to SwiftPM-first. CocoaPods Trunk freezes on 2026-12-02, and every CocoaPods consumer is pinned below TSBackgroundFetch 4.2.0
status: draft
created: 2026-11-05
platforms: [ios, tooling]
submodules: [native/background-fetch, react-native/background-fetch, cordova/background-fetch, capacitor/background-fetch, flutter/background_fetch]
branch: per phase (see "Proposed fix"); e.g. feat/WO-001-rn-spm, feat/WO-001-cordova-spm
issues: []
supersedes: []
---

> **Cross-repo.** The TSLocationManager parts live in
> `/Users/chris/workspace/background-geolocation/bg-forge` and need their own work order
> there (see Out of scope). They are listed here because the release order depends on them.

**Evidence markers used below:**
- **PROVEN:** observed on this machine: a real build or run, a command's output, or a source file read.
- **DOCS:** taken from upstream documentation or release notes, not observed here.
- **INFERRED:** reasoning, not observed.

All experiments ran on 2026-11-05 with Xcode 27.2 (27B52), the iOS 27.2 simulator (iPhone 17), CocoaPods 1.16.2 and Node 24.2.0.

## Diagnosis

### 1. CocoaPods Trunk stops accepting new versions on 2026-12-02

- **PROVEN:** `pod trunk push` of TSBackgroundFetch 4.2.0 was rejected on 2026-11-05 with "CocoaPods Trunk is in its read-only test week, so new Podspecs are not being accepted until Friday November 6 at 12:00pm ET… ahead of Trunk permanently becoming read-only on December 2nd 2026".
- **DOCS:** <https://blog.cocoapods.org/CocoaPods-Specs-Repo/> says existing specs and the CDN keep working after the freeze, and that teams with their own spec repos or vendored dependencies are unaffected. The dates are "not set in stone".
- **Consequence (INFERRED):** the last chance to put anything on Trunk runs from **Fri 2026-11-06 12:00pm ET through 2026-12-01**. After that, CocoaPods users are frozen at whatever versions are on Trunk.

### 2. State of the TSBackgroundFetch 4.2.0 release (PROVEN)

| Channel | State |
|---|---|
| GitHub Release `ios/4.2.0` | Published with `TSBackgroundFetch.xcframework.zip`. `publish-ios.sh` downloaded the uploaded zip again and confirmed its checksum. |
| Tags | `ios/4.2.0` and bare `4.2.0`, both at `5de5242` |
| SwiftPM | A fresh `from: "4.1.0"` resolves 4.2.0 and builds, using both the `TSBackgroundFetch` (Objective-C) and `BackgroundFetch` (Swift API) products |
| CocoaPods Trunk | Newest version is 4.1.1; the 4.2.0 spec on the CDN returns 404 |
| `native/background-fetch` `master` | 3 release commits ahead of `origin/master` and not pushed (as of 2026-11-05) |

4.2.0 raised the minimum iOS from 12.0 to 15.0: `TSBackgroundFetch.podspec:21` and `Package.swift:7` at the tag.

### 3. Every CocoaPods consumer pins `TSBackgroundFetch ~> 4.1.0`, which excludes 4.2.0 (PROVEN, file reads)

**Fetch plugins (this repo):**

| Consumer | Pin | iOS minimum | SwiftPM manifest |
|---|---|---|---|
| React Native: `RNBackgroundFetch.podspec:23` | `~> 4.1.0` | `:19` → 13.4 | none |
| Cordova: `plugin.xml:46-54` (`<pod name="TSBackgroundFetch" spec="~> 4.1.0"/>` at `:52`) | `~> 4.1.0` | cordova-ios writes Podfiles at 13.0 by default | none |
| Capacitor: `TransistorsoftCapacitorBackgroundFetch.podspec:16` | `~> 4.1.0` | `:14` → 15.0 | `Package.swift:18`, `from: "4.1.0"`, iOS 15 |
| Flutter: `ios/background_fetch.podspec:20` | `~> 4.1.0` | `:21` → 13.0 | `ios/background_fetch/Package.swift`, `from: "4.1.0"`, `.iOS("13.0")` |

**TSLocationManager (bg-forge):**
- **Published 4.8.3** (`native/background-geolocation` `origin/master`): `TSLocationManager.podspec:29` pins `~> 4.1.0` with minimum iOS 13.0 (`:22`). `Package.swift:20-23` uses `upToNextMinor(from: "4.1.0")` with `.iOS(.v13)`.
- **It is the only consumer that is itself published on Trunk.** Every geolocation plugin gets TSBackgroundFetch through it. The plugin pods themselves are local `:path` pods from `node_modules` or the pub cache, not Trunk pods.
- **Templates:** `native/ios/Package.swift.erb:12` already says `.v15`, and the podspec template takes its minimum from `DEPLOYMENT_TARGET_IOS` (default 15.0). But the TSBackgroundFetch range is **hardcoded** at `TSLocationManager.podspec.erb:29` and `Package.swift.erb:20-23`. The `COCOAPODS_DEP_RANGE_TSBF` and SPM version variables that `scripts/publish-ios.sh:569-571` and `:586-588` pass are never read.

**Geolocation plugins (bg-forge):** they pull TSBackgroundFetch indirectly through TSLocationManager. This was cited by the audit agent and not re-read by me.
- `RNBackgroundGeolocation.podspec:29-30`
- cordova `plugin.xml:46-49`
- Capacitor: podspec `:16-17`; `Package.swift:15-16` (iOS 14)
- Flutter: podspec `:21-22`; `Package.swift:19-20` (iOS 13)

**So pushing 4.2.0 to Trunk reaches no one by itself.** Every consumer needs a release that widens its pin.

### 4. On Xcode 27, Trunk 4.1.1 already breaks CocoaPods builds unless the host raises pod deployment targets (PROVEN)

- **The floor:** Xcode 27 refuses any deployment target below iOS 15.0. The Trunk TSBackgroundFetch 4.1.1 pod declares 12.0, and TSLocationManager 4.8.3 declares 13.0.
- **Experiment D (bare app):** the app built only with a `post_install` hook forcing `IPHONEOS_DEPLOYMENT_TARGET = '15.0'`.
- **Experiment C (Cordova):** a bare cordova-ios 8.1.1 app fails on Xcode 27.2 unless `<preference name="deployment-target" value="15.0"/>` is set.
- **Experiment B (React Native):** not affected, because `react_native_post_install` rewrites pod deployment targets.
- **Flutter and Capacitor:** whether their CocoaPods hooks raise targets: **NOT CHECKED**.
- **What fixes it (INFERRED):** this is a second reason the Trunk window matters. Getting 4.2.0 (iOS 15) onto Trunk *and* widening the pins removes the need for a manual hook for fetch users. TSLocationManager needs its own 15.0 release on Trunk for the same reason.

### 5. SwiftPM readiness by host framework

**Flutter**
- **Default (PROVEN, source):** SwiftPM has been on by default on stable since Flutter 3.44 (`flutter_tools/lib/src/features.dart:233-240`; commit `429eea7a114`, "Enable SPM by default on Stable").
- **Plugin selection (PROVEN, source):** a plugin that has `ios/<name>/Package.swift` goes into the SwiftPM package, and its podspec is ignored. Flutter falls back to CocoaPods per plugin, with a warning that this "will become an error in a future version of Flutter".
- **iOS minimum (PROVEN):** the iOS 15.0 template floor arrived in 3.47.
- **Plugin manifest (PROVEN):** it needs `FlutterFramework` (since 3.44). This repo's manifest already has it.
- **Future (DOCS):** CocoaPods is in "maintenance mode" (flutter.dev blog, 2026-04-30).

**Capacitor**
- **Defaults (PROVEN by reading the CLI 8.3.0 source, not by running it):** Capacitor 8 creates SwiftPM apps by default (`ios/App/CapApp-SPM`), and `--packagemanager CocoaPods` is still supported. A plugin ships both a podspec and a root `Package.swift`. In an SPM app, plugins without a `Package.swift` are skipped with only a warning; there is no CocoaPods fallback.
- **Requirements (DOCS):** Capacitor 8 requires iOS 15 and Xcode 26+. Capacitor 9 (beta; GA forecast end of November 2026) requires iOS 16 and Xcode 27, needs `capacitor-swift-pm` 9, and drops the `Cordova` product.

**React Native**
- **Status (DOCS):** SwiftPM app mode is **experimental and opt-in** in 0.87 and 0.88. The 0.87 release post says "Do not use it in production yet", and CocoaPods is the default and fully supported path. The 0.89 plan is SwiftPM as the default, with an opt-out to CocoaPods.
- **Tooling (PROVEN, source):** 0.88.1 ships `npx react-native spm` (`scripts/setup-apple-spm.js`, with the actions add/update/deinit/scaffold/sync). Autolinking honours a library's own `ios/Package.swift`, which it reports as "Self-managed".
- **Older route:** since 0.75, a podspec can call `spm_dependency` (`scripts/cocoapods/spm.rb`).

**Cordova**
- **Swift-package plugins (PROVEN, source and docs):** cordova-ios 8.x (latest 8.1.1) supports them with `<platform name="ios" package="swift">` plus a `Package.swift` (`lib/SwiftPackage.js`). A `<pod … nospm="true">` is skipped in SPM mode (`lib/Api.js:394-397`; documented by apache/cordova-docs#1482, merged 2026-03-07).
- **cordova-ios 7 (PROVEN):** it cannot produce a working app on Xcode 27. Its apps crash at launch with "UIScene life cycle is required for apps built with this SDK".

### 6. Mixing CocoaPods and SwiftPM copies of TSBackgroundFetch in one app

This is the transition hazard. Picture an app that combines a fetch plugin on SwiftPM with a geolocation plugin whose TSLocationManager pod pulls TSBackgroundFetch 4.1.1 from Trunk.

**Both copies end up in the same binary image** (static pods, or `use_frameworks!` with `static_framework = true` pods, as every geolocation podspec sets): **PROVEN, experiment D.**
- **No diagnostics:** it builds and runs with no error, warning or duplicate classes.
- **SwiftPM wins by search-path order, not by version:** `-F$BUILT_PRODUCTS_DIR` comes first. The link map and the embedded version string `PROJECT:TSBackgroundFetch-4200` confirm the 4.2.0 copy ran.
- **The lockfile misreports:** `Podfile.lock` still says 4.1.1, and TSLocationManager's `~> 4.1.0` is never enforced.
- **Header skew:** pod-side code compiles against the 4.1.1 headers but runs 4.2.0. That's harmless today only because the 4.1.1 and 4.2.0 headers are byte-identical.

**A dynamic pod framework statically absorbs the SwiftPM copy, so a second image contains it:** **PROVEN, experiment B.** The setup was React Native with `USE_FRAMEWORKS=dynamic` and `spm_dependency`, plus a Trunk TSBackgroundFetch pod.
- **Result:** a **launch crash**. objc reports "Class TSBackgroundFetch is implemented in both RNBackgroundFetch.framework and RNPodsApp.debug.dylib", then `NSInternalInconsistencyException: Launch handler for task with identifier com.transistorsoft.fetch has already been registered`.
- **Two pods each declaring `spm_dependency`** under dynamic linkage crash the same way.

**Rule (INFERRED from both):** an app must get TSBackgroundFetch from **one channel**. During the transition, every Transistor plugin in an app must switch channel together.

## Proposed fix

**SwiftPM-first, with CocoaPods frozen rather than cut off.** Chris decided this on 2026-11-05: abandon CocoaPods eventually, but not immediately.

| Channel | Plan |
|---|---|
| SwiftPM | Carries every future version |
| CocoaPods | Gets a last, consistent set of versions before 2026-12-02, then stays frozen |

### Phase 0: now (before the Trunk window opens)

- [ ] Push the 3 release commits on `native/background-fetch` `master` (Chris's call; it's an outward-facing action).
- [ ] Do **not** re-run `./forge publish native ios` for 4.2.0 (see "Not an acceptable shortcut").

### Phase 1: last CocoaPods window (2026-11-06 12:00pm ET to 2026-12-01)

1. **Push TSBackgroundFetch 4.2.0 to Trunk** from the tagged commit, pod only:
   `cd native/background-fetch && git checkout 5de5242 && pod trunk push TSBackgroundFetch.podspec --skip-import-validation --allow-warnings`.
   Run `pod spec lint` first as a rehearsal.
2. **Release TSLocationManager to Trunk (bg-forge WO).** Changes:
   - TSBackgroundFetch range widened to accept 4.2: `~> 4.1` on CocoaPods and `from: "4.1.0"` on SwiftPM.
   - Minimum iOS 15.0 in both manifests.
   - The templates read the range from the variables `publish-ios.sh` already passes.

   This is the only Trunk push that unblocks apps combining geolocation with fetch. It has to land before 2026-12-01.
3. **Bridge releases** (npm and pub; no Trunk deadline, but they need steps 1 and 2 first for their CocoaPods users):
   - **Recommended pin: `~> 4.2`.** It guarantees the iOS 15 podspec, which fixes Diagnosis §4 for fetch.
   - **Minimum iOS 15.0** in every podspec and manifest: RN 13.4 → 15.0, Flutter 13.0 → 15.0. Cordova docs must require the deployment-target 15 preference.
   - **DECISION D1:** `~> 4.2` (recommended) or `~> 4.1`. `~> 4.1` would let pre-15 apps keep resolving 4.1.1, but 4.1.1 needs a manual Xcode 27 hook.

### Phase 2: SwiftPM support in each plugin (can start now; no Trunk dependency)

**Flutter background_fetch.** Its manifest already exists.
- Raise `ios/background_fetch/Package.swift` to `.iOS("15.0")`.
- Keep the podspec for CocoaPods-mode apps, with the Phase 1 pin.
- *Verify end to end* (see Verification).

**Capacitor background-fetch.** Already SwiftPM-ready (iOS 15, `from: "4.1.0"`). Only the podspec pin changes. Capacitor 9 changes are a separate work order.

**React Native background-fetch.** Both modes are needed through 0.88, because SwiftPM mode is experimental there and 0.89 is planned to make it the default.
- **SwiftPM app mode: ship `ios/Package.swift` (PROVEN in experiment A).** Debug and Release built, the app launched, and `configure()` resolved to 2. Use the manifest in Appendix A. Required with it:
  - **Delete the vendored `ios/RNBackgroundFetch/TSBackgroundFetch.xcframework`.** It is tracked in git and ships in the npm 4.4.2 tarball. While it is present, SwiftPM fails with "invalid header layout", and `exclude:` doesn't help. The podspec never used it.
  - **Add `"swiftpmConfig": { "name": "RNBackgroundFetch" }`** to `package.json`.
  - **Keep the `RCT_NEW_ARCH_ENABLED` defines.** Without them the module falls back to the legacy interop path.
- **CocoaPods mode: keep the Trunk dependency, `s.dependency 'TSBackgroundFetch', '~> 4.2'` (DECISION D2, recommended).**
  - **Why (PROVEN in experiment B):** `spm_dependency` as written fails to link under React Native's **default** static linkage (`Undefined symbols … _OBJC_CLASS_$_TSBackgroundFetch`) even though `pod install` exits 0. It works only with an extra `user_target_xcconfig` line gated on `ENV['USE_FRAMEWORKS']`. That gate is unreliable when `use_frameworks!` is hard-coded or set through Expo, which is untested. Under dynamic linkage, any second copy crashes at launch (Diagnosis §6).
  - **Trade-off:** React Native CocoaPods-mode users are frozen at 4.2.x after 2026-12-02 until they move to SwiftPM mode.
- **Never do the following:** `npx react-native spm scaffold` cannot generate the manifest. It drops the TSBackgroundFetch dependency, and its output fails "invalid header layout".

**Cordova background-fetch.** Convert it to a Swift-package plugin (PROVEN in experiment C on cordova-ios 8.1.1: built, launched, and `configure` returned 2).
- **Changes:**
  - `<platform name="ios" package="swift">`
  - a root `Package.swift` (Appendix B)
  - `publicHeadersPath: "."`
  - the library product named after the plugin id
  - `.product(name: "Cordova", package: "cordova-ios")`, with the `.package(url: …cordova-ios…)` call on its own line, because cordova-ios rewrites it with a greedy regex
- **Objective-C change required:** `#import "AppDelegate.h"` is not visible from a Swift Package target. Replace `@implementation AppDelegate(AppDelegate)` with a category on `CDVAppDelegate`.
  - This changes behaviour. It was verified only by compiling. The `application:performFetchWithCompletionHandler:` path never ran, and an app delegate that implements that method would shadow the category.
- **Keep `<pod name="TSBackgroundFetch" spec="~> 4.2" nospm="true"/>` for now (DECISION D3).**
  - **Cost (PROVEN):** while any `<podspec>` block exists, cordova-ios 8 still writes a Podfile and runs `pod install`, so SPM mode is not CocoaPods-free.
  - **Benefit:** the fallback still serves cordova-ios 7 on Xcode 26.
  - **INFERRED:** that window lasts until Apple requires the iOS 27 SDK for App Store submissions. After that, cordova-ios 7 apps can't run (Diagnosis §5), so drop the `<podspec>` block in the following major release.
- **Plugin `engines`:** document `deployment-target` ≥ 15.0.

**Geolocation plugins (bg-forge).** They need the same per-platform work. Coordinate so that fetch and geolocation switch channel **together** on each platform (Diagnosis §6). That gets its own bg-forge work order.

**Mac Catalyst (DECISION D4).**
- **The problem (PROVEN):** the 4.2.0 XCFramework has no Catalyst slice, and the cordova-ios 8 template sets `SUPPORTS_MACCATALYST = YES`, so Catalyst builds fail.
- **The fix:** rebuild with `build-ios.sh --catalyst` for the next 4.2.x, or document turning Catalyst off.

### Phase 3: publish tooling (after Phase 1 completes)

**`native/background-fetch/scripts/publish-ios.sh`:**
- **Turn off CocoaPods by default:** after 2026-12-02, make `PUSH_COCOAPODS=0` the default (`:113`).
- **Fix the startup check:** stop `require_cocoapods_trunk` (`:179`) from running unconditionally, or it will fail fast for no reason.
- **Fix the stale comment** "iOS 13 required by the Swift overlay" in the `Package.swift` template (`:736`).
- **DECISION D5:** keep rewriting `TSBackgroundFetch.podspec` in git after the freeze, which costs nothing and serves `:git` / `:podspec =>` users, or retire it.

**`forge`:**
- Update the "Publish XCFramework to CocoaPods + SPM" text (`:155`, `:166`).
- Teach `link-ios`/`unlink-ios` (`:320-363`) about the new React Native `ios/Package.swift` and the Cordova `Package.swift`. Today it rewrites only the native and Capacitor manifests (`:115-116`, `:358`).

**bg-forge:** the same changes in `native/ios/scripts/publish-ios.sh` (`:120`, `:789`) and its templates. Covered by the bg-forge work order.

### Phase 4: communication

- **Install docs (docforge and each plugin README):** SwiftPM first, with CocoaPods described as "frozen at 4.2.x after Dec 2, 2026".
- **One-channel rule:** never mix SwiftPM and CocoaPods copies of TSBackgroundFetch in one app; all Transistor plugins move together.
- **Upgrade notes:**
  - Cordova needs the `deployment-target` 15 preference.
  - CocoaPods users on 4.1.1 with Xcode 27 need a `post_install` deployment-target hook until they upgrade.
  - Existing CocoaPods apps whose `Podfile.lock` pins 4.1.1 need `pod update TSBackgroundFetch`; a plain `pod install` won't move.
- **Support tip:** in a mixed app, read the real version from `TSBackgroundFetchVersionString` (it reads `PROJECT:TSBackgroundFetch-4200` for 4.2.0), not from `Podfile.lock`.

### Release order

1. ✅ TSBackgroundFetch 4.2.0 on GitHub and SwiftPM (done 2026-11-05).
2. TSBackgroundFetch 4.2.0 to Trunk (from 2026-11-06 12:00pm ET).
3. TSLocationManager release to Trunk: widened range and iOS 15 (bg-forge; before 2026-12-01).
4. Fetch-plugin releases (Phase 1 pins plus Phase 2 manifests), then geolocation-plugin releases, both channels per platform together.
5. After 2026-12-02: Phase 3 tooling switch-off.

## Scope check

**Everything that consumes TSBackgroundFetch on iOS** is listed in Diagnosis §3. That includes the published TSLocationManager manifests in bg-forge and four geolocation plugins.

**Example apps** pin through their lockfiles; every one of them is locked at 4.1.1 today:
- `react-native/background-fetch/example/react-native/ios/Podfile.lock`
- `cordova/background-fetch/example/app/platforms/ios/Podfile.lock`
- `capacitor/background-fetch/example/…/Package.resolved`

**The two internal demo apps don't depend on any channel:**
- `native/background-fetch/example/ios/BackgroundFetchExample` uses a local package path to this repo's `Package.swift`.
- The DemoApp builds from source.

**Not affected:** the `forge link-ios`/`unlink-ios` local-dev flow keeps working for the native and Capacitor manifests. It does not yet know about the new React Native and Cordova manifests (Phase 3).

**Docs site:** `docforge/` install pages mention CocoaPods. Exact pages are **NOT CHECKED**.

## Platform parity

**iOS:** everything above.

**Android: N/A.** Android distribution doesn't touch CocoaPods or SwiftPM.
- **TSBackgroundFetch** publishes to Maven Central through the Sonatype Central Portal (`scripts/publish-android.sh`, tag `android/${VERSION}` at `:349`).
- **Every bridge** depends on `com.transistorsoft:tsbackgroundfetch` through Gradle:
  - `react-native/background-fetch/android/build.gradle:49`
  - `cordova/background-fetch/src/android/build.gradle:6`
  - `capacitor/background-fetch/android/build.gradle:59`
  - `flutter/background_fetch/android/build.gradle:42`
- **Android was not built in any experiment**, and the converted Cordova plugin copy's Android side was untouched.

## Test plan

**Already run (2026-11-05, scratch only; scratch evidence is temporary).** Appendices A and B keep the manifests.

| # | Experiment | Result |
|---|---|---|
| A | RN 0.88.1 SwiftPM app mode, plugin shipping its own `ios/Package.swift` | **works**: Debug and Release built; launched; `[TSBackgroundFetch load]`; `configure` → 2 |
| B | RN 0.88.1 CocoaPods mode with `spm_dependency` | **partial**: default static link fails; works with a `user_target_xcconfig` workaround or `USE_FRAMEWORKS=dynamic`; dynamic plus a second copy crashes at launch |
| C | cordova-ios 8.1.1 with the plugin converted to a Swift package | **works**, with the Objective-C delegate-category change and the deployment-target 15 preference; Catalyst fails; cordova-ios 7.1.1 crashes on Xcode 27 |
| D | One app with CocoaPods 4.1.1 (via TSLocationManager 4.8.3) and SwiftPM 4.2.0 | **builds and runs silently**; SwiftPM copy wins by search path; `Podfile.lock` is wrong |
| — | iOS 13-declared package against 4.2.0 on Xcode 27.2 | builds; Xcode raises it to `ios15.0` |
| — | SwiftPM consumer `from: "4.1.0"` resolves 4.2.0 from GitHub and builds both products | works |

**Still to do.** The critic's list, in priority order. Each carries `WO-001` in its scratch-app or test name.

1. **Cold-cache SwiftPM resolution** of 4.2.0: a fresh `-clonedSourcePackagesDirPath` and an empty `~/Library/Caches/org.swift.swiftpm`. Every experiment had the zip cached already.
2. **The exact React Native deliverable**: `ios/Package.swift`, plus the podspec with the `~> 4.2` pin and platform 15.0, with the vendored XCFramework removed, installed from `npm pack`. Test it in both SwiftPM mode and CocoaPods mode, under default static linkage, `USE_FRAMEWORKS=static`, `USE_FRAMEWORKS=dynamic` (env) and a hard-coded `use_frameworks! :linkage => :dynamic`.
3. **Real coexistence:** react-native-background-geolocation (pod) with SwiftPM RNBackgroundFetch, under static and dynamic linkage. Then cordova-background-geolocation (pod) with the SwiftPM Cordova fetch plugin.
4. **Flutter 3.47.5 end to end, without changing global config** (use the pubspec `enable-swift-package-manager` setting or the `FLUTTER_SWIFT_PACKAGE_MANAGER` env var):
   - background_fetch via SwiftPM;
   - plus flutter_background_geolocation via SwiftPM, to check version unification;
   - plus a geolocation plugin forced onto CocoaPods (the mixed-images case).
5. **Capacitor 8.6 end to end:** `npx cap sync`, build and run in a SwiftPM app and in a `--packagemanager CocoaPods` app.
6. **Archive and local export** (no upload) for SwiftPM mode, React Native CocoaPods static, Cordova 8 SwiftPM, and the mixed app. Inspect the IPA for duplicate classes, the privacy manifest and the stub framework. In experiment B's dynamic mode, `PrivacyInfo.xcprivacy` was **not** copied into the app (PROVEN); React Native's aggregated manifest covered it, but non-RN hosts would not get that.
7. **Physical device:** real BGAppRefresh delivery through the React Native and Cordova bridges built against SwiftPM 4.2.0, using LLDB `_simulateLaunchForTaskWithIdentifier:@"com.transistorsoft.fetch"`. This especially covers the Cordova `CDVAppDelegate` category.
8. **`pod spec lint` of the 4.2.0 podspec,** and a local install against a TSLocationManager podspec edited to `~> 4.1` and iOS 15, to rehearse the Trunk pushes.
9. **Xcode 26 regression check** (needs Xcode 26 installed; only 27.2 is here): do `from: "4.1.0"` consumers that declare iOS 13 or 14 (Flutter's manifest, Capacitor geolocation at 14) fail to resolve now that 4.2.0 is iOS 15? **Unknown whether 4.2.0 already broke existing Xcode 26 users.**

## Out of scope

- **TSLocationManager and the geolocation plugins:** a separate work order in bg-forge. Only the release-order dependency is recorded here.
- **The Capacitor 9 migration:** iOS 16, `capacitor-swift-pm` 9, dropping the `Cordova` product. Separate work order.
- **A self-hosted CocoaPods spec repo or `:podspec =>` distribution after the freeze.** Verified to work, but not chosen (see the next section).
- **Android.**
- **Rewriting TSBackgroundFetch's legacy background-fetch fallback.** That's the cause of the `setMinimumBackgroundFetchInterval` deprecation warnings.

## Not an acceptable shortcut

- **Re-running `./forge publish native ios --version 4.2.0` to retry Trunk.**
  - It rebuilds the XCFramework, so the new zip's checksum won't match the uploaded asset, and the script stops ("Remote asset differs").
  - With `--retag` it **replaces the uploaded zip**. That breaks the `Package.swift` checksum for everyone who has already resolved 4.2.0.
  - Push the existing podspec from the tagged commit instead.
- **Vendoring the XCFramework inside the plugin packages** (npm or pub).
  - It fails fatally next to any Trunk TSBackgroundFetch pod: `[!] The 'Pods-App' target has frameworks with conflicting names: tsbackgroundfetch.xcframework`. That includes the one TSLocationManager pulls in.
  - React Native SwiftPM mode rejects the vendored copy outright ("invalid header layout").
  - The existing vendored copy in the React Native repo must be **removed**, not reused.
- **Downloading the framework from a `:path` pod's `prepare_command`.**
  - It runs only when the pod is added or changed.
  - When it doesn't run, the framework silently drops out of the link flags (PROVEN).
  - `s.source :http` is ignored for `:path` pods.
- **Pointing a podspec's `s.dependency` at a non-Trunk source.**
  - CocoaPods rejects `:source`/`:podspec` ("Unsupported version requirements") and `:git`/`:path` ("Podspecs cannot specify the source of dependencies").
  - The hack of injecting sources into `Pod::Config` from the podspec's Ruby works for a plain `pod install` but fails under `pod update` and `--repo-update`.
- **`npx react-native spm scaffold` for RNBackgroundFetch.** It drops the TSBackgroundFetch dependency ("Unknown dependency … not wired"), and its manifest fails to resolve. Hand-write the manifest (Appendix A).
- **`spm_dependency` as the React Native CocoaPods-mode default.**
  - It fails to link under React Native's default static linkage.
  - The fix depends on a fragile env-var gate.
  - Under dynamic linkage it **crashes at launch** next to any other copy.
  - Because `defined?(spm_dependency)` is true for every React Native ≥ 0.75, it would move all modern React Native CocoaPods users onto the mixing hazard by default.
- **Widening only the fetch plugins' pins.** Apps that also use geolocation fail CocoaPods resolution against TSLocationManager's `~> 4.1.0`. TSLocationManager must widen first, on Trunk, before 2026-12-01.
- **Treating "mixing builds and runs" (experiment D) as safe.**
  - Which copy runs is decided by search-path order.
  - `Podfile.lock` is wrong, and version ranges go unenforced.
  - Header skew becomes a runtime-only failure with no diagnostics as soon as the headers diverge.
  - The two-image variant crashes.
  - Design for one channel per app.
- **Leaving manifests at iOS 13 or 14 because "Xcode 27 raises them".** That only works on Xcode 27, and older Xcode is untested. Declare 15.0 everywhere that depends on 4.2.
- **Counting on cordova-ios 7 as a supported path.** It can't produce a working app on Xcode 27 (UIScene crash), whatever the plugin does.
- **Trusting the dry-run framework version.** `--dry-run` builds with the old properties version (4.1.1) because it skips the version-file update. That isn't a bug; the real run bumps first.
- **A self-hosted spec repo or `pod … :podspec =>` as the strategy.** Both are PROVEN to work, but they need an edit in every app's Podfile (except Cordova, which can inject sources). Chris chose SwiftPM-first. Revisit only if a post-freeze fix must reach CocoaPods users.
- **Cutting CocoaPods off immediately.** Chris rejected this on 2026-11-05. CocoaPods gets a last consistent set and then freezes.

## Verification

- [ ] Phase 0: `native/background-fetch` `master` pushed
- [ ] Phase 1: TSBackgroundFetch 4.2.0 on Trunk (the CDN index `all_pods_versions_c_4_1.txt` lists 4.2.0)
- [ ] Phase 1: TSLocationManager on Trunk with the widened range and iOS 15 (bg-forge work order)
- [ ] Decisions D1–D5 recorded in the Log
- [ ] Test-plan items 1–9 done, or explicitly waived in the Log
- [ ] Each plugin release built and run in SwiftPM mode and in CocoaPods mode on the simulator, and on a device for the BGTask path
- [ ] Phase 3 tooling switched after 2026-12-02
- [ ] Docs and changelogs carry the one-channel rule and the CocoaPods freeze notice

## Log

- 2026-11-05 — created.
  - TSBackgroundFetch 4.2.0 published to GitHub and SwiftPM; the bare-tag fix (`5b8e0ea`) was confirmed working in production.
  - Trunk push rejected because of the test week.
  - Chris: "abandon cocoapods and go all-in on SPM", then "we don't need to abandon cocoapods immediately", so the plan is SPM-first with CocoaPods frozen.
  - Experiments A–D and the Flutter/Capacitor/React Native status research ran in scratch; a critic review produced the remaining test list.

## Appendix A: React Native `ios/Package.swift` (PROVEN in experiment A on RN 0.88.1)

The React Native paths are relative to the autolinking symlink `<app>/ios/build/generated/autolinking/libs/RNBackgroundFetch`, not to the file's real location; react-native-safe-area-context 5.11.1 uses the same pattern. The package name and product name must equal the SwiftName (`swiftpmConfig.name`). React Native's remote SwiftPM mode (`RN_SPM_REMOTE_URL`) is expected to break these paths: **NOT TESTED**.

```swift
// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "RNBackgroundFetch",
    platforms: [.iOS(.v15)],
    products: [
        .library(name: "RNBackgroundFetch", targets: ["RNBackgroundFetch"]),
    ],
    dependencies: [
        .package(name: "ReactNative", path: "../../../../xcframeworks"),
        .package(name: "React-GeneratedCode", path: "../../../ios"),
        .package(url: "https://github.com/transistorsoft/transistor-background-fetch.git", from: "4.2.0"),
    ],
    targets: [
        .target(
            name: "RNBackgroundFetch",
            dependencies: [
                .product(name: "ReactHeaders", package: "ReactNative"),
                .product(name: "ReactNativeHeaders", package: "ReactNative"),
                .product(name: "ReactNativeDependenciesHeaders", package: "ReactNative"),
                .product(name: "ReactAppHeaders", package: "React-GeneratedCode"),
                .product(name: "TSBackgroundFetch", package: "transistor-background-fetch"),
            ],
            path: "RNBackgroundFetch",
            exclude: ["RNBackgroundFetch+AppDelegate.m"],
            sources: ["RNBackgroundFetch.mm"],
            publicHeadersPath: ".",
            cSettings: [.define("RCT_NEW_ARCH_ENABLED", to: "1")],
            cxxSettings: [
                .define("RCT_NEW_ARCH_ENABLED", to: "1"),
                .define("DEBUG", .when(configuration: .debug)),
                .define("NDEBUG", .when(configuration: .release)),
            ],
            linkerSettings: [.linkedFramework("UIKit"), .linkedFramework("Foundation"), .linkedFramework("BackgroundTasks")]
        ),
    ],
    cxxLanguageStandard: .cxx20
)
```

**Other gotchas from experiment A:**
- **`spm add --deintegrate` breaks hybrid Podfiles.** It strips only the `use_native_modules!` and `use_react_native!` lines, leaving orphaned arguments, so `ruby -c ios/Podfile` reports a syntax error. That matters for hybrid apps.
- **The zero-arg `npx react-native spm`** refuses to run unless the template's `pbxproj` and Podfile are tracked in git and unmodified.
- **The npm package has no `files` allowlist,** so `ios/Package.swift` ships. Keep it that way, or add the file to the allowlist if one is introduced later.

## Appendix B: Cordova Swift-package plugin (PROVEN in experiment C on cordova-ios 8.1.1)

```swift
// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "cordova-plugin-background-fetch",
    platforms: [.iOS(.v15)],
    products: [
        .library(name: "cordova-plugin-background-fetch", targets: ["CDVBackgroundFetch"])
    ],
    dependencies: [
        // cordova-ios rewrites this line to a local path at `cordova plugin add` time; keep it on its own line.
        .package(url: "https://github.com/apache/cordova-ios.git", from: "8.0.0"),
        .package(url: "https://github.com/transistorsoft/transistor-background-fetch.git", from: "4.2.0")
    ],
    targets: [
        .target(
            name: "CDVBackgroundFetch",
            dependencies: [
                .product(name: "Cordova", package: "cordova-ios"),
                .product(name: "TSBackgroundFetch", package: "transistor-background-fetch")
            ],
            path: "src/ios",
            publicHeadersPath: "."
        )
    ]
)
```

`plugin.xml`: change the iOS element to `<platform name="ios" package="swift">` and add `nospm="true"` to the `<pod>`.

Source changes in `src/ios`:

```diff
--- CDVBackgroundFetch.h
 #import <Cordova/CDVPlugin.h>
-#import "AppDelegate.h"
--- CDVBackgroundFetch.m
-#import "AppDelegate.h"
+#import <Cordova/CDVAppDelegate.h>
...
-@implementation AppDelegate(AppDelegate)
+@implementation CDVAppDelegate(CDVBackgroundFetch)
```

These same sources also compiled on cordova-ios 7.1.1 (PROVEN). As noted above, the category's runtime behaviour is NOT VERIFIED.
