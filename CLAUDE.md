# Fetch Forge — Background Fetch SDK monorepo

One repo for Transistor Software's Background Fetch SDK ecosystem.
5 git submodules organised by platform, with a central `forge` CLI.

## Repo map

```
native/background-fetch/       Native iOS + Android core — TSBackgroundFetch
                               (CocoaPods podspec + SPM Package.swift + Android Gradle/Maven)
react-native/background-fetch/ React Native SDK
cordova/background-fetch/      Cordova plugin
capacitor/background-fetch/    Capacitor plugin
flutter/background_fetch/      Flutter/Dart SDK
forge                          Unified CLI
```

Every platform directory is a git submodule with its own remote and independent release cycle.

## Submodule workflow

```bash
# Check out a submodule that isn't initialised
git submodule update --init react-native/background-fetch

# Always commit from WITHIN the submodule
cd react-native/background-fetch
git checkout -b feature/my-change
# ... edit, stage, commit, push ...

# The parent repo tracks submodule pointers — update separately
cd ../..
git add react-native/background-fetch
git commit -m "chore: bump react-native submodule"
```

**Never commit submodule content from the parent repo.**

## Native architecture

`native/background-fetch` is the single native library for both platforms:

- **iOS**: Objective-C source → XCFramework → distributed via CocoaPods (`TSBackgroundFetch`) and SPM
- **Android**: Java source → AAR → distributed via Maven Central (`com.transistorsoft:tsbackgroundfetch`)

All platform SDKs (React Native, Cordova, Capacitor, Flutter) are thin bridge layers that
depend on TSBackgroundFetch via CocoaPods/SPM (iOS) and Gradle/Maven (Android).

## Forge CLI

`./forge` at the repo root is the unified CLI.

```bash
# Build
./forge build ios              # XCFramework
./forge build android          # Android AAR
./forge build native           # both
./forge build react-native     # npm run build
./forge build all              # everything

# Publish
./forge publish native ios     # CocoaPods + SPM
./forge publish native android # Maven Central (Sonatype)
./forge publish native android --snapshot  # Sonatype SNAPSHOT
./forge publish react-native   # npm publish
./forge publish cordova        # npm publish
./forge publish capacitor      # npm publish
./forge publish flutter        # dart pub publish

# Local iOS development (link built XCFramework into example apps)
./forge link-ios react-native  # or: flutter, capacitor, cordova, all
./forge unlink-ios all         # restore remote deps before publishing

# Status
./forge status                 # version table + submodule overview
```

## Local iOS development workflow

To test iOS changes without publishing:

1. `./forge build ios` — builds XCFramework to `native/background-fetch/build/Release-Publish/`
2. `./forge link-ios <platform>` — auto-detects CocoaPods vs SPM and links the framework
3. Build and run the example app in Xcode
4. `./forge unlink-ios <platform>` — restore production references before committing or publishing

## Bug fix workflow

Before writing or committing any fix, walk me through your reasoning step by step so I can approve the approach. Specifically:

1. **Diagnosis** — What is the root cause? Show the specific code path that fails and explain why.
2. **Proposed fix** — Describe the change in plain English. Which files, which methods, what the change does.
3. **Scope check** — What else calls this code? Could the fix break existing behavior or other callers?
4. **Platform parity** — Does the same bug exist on the other platform (iOS/Android)? Should both be fixed together?
5. **Test plan** — How will you verify the fix? Existing tests, new tests, or manual steps?
6. **Wait for approval** — Do not write code or commit until I confirm the plan.

**Before every commit:** Show the diff and wait for explicit confirmation. Never commit without asking first.
