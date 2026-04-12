# Fetch Forge

Monorepo for Transistor Software's **Background Fetch SDK** ecosystem — native core plus four platform plugins, all orchestrated by a single `forge` CLI.

## Repo map

```
native/background-fetch/        Native iOS + Android core (TSBackgroundFetch)
react-native/background-fetch/  react-native-background-fetch
cordova/background-fetch/       cordova-plugin-background-fetch
capacitor/background-fetch/     @transistorsoft/capacitor-background-fetch
flutter/background_fetch/       flutter_background_fetch (pub.dev)
forge                           Unified CLI
setup                           First-time setup script
```

Every platform directory is an independent git repository tracked here as a submodule.

## Platform summary

| Submodule | Package | Version | Languages |
|---|---|---|---|
| `native/background-fetch` | `TSBackgroundFetch` (CocoaPods/SPM) | 4.0.6 | ObjC (iOS) · Java (Android) |
| `native/background-fetch` | `com.transistorsoft:tsbackgroundfetch` (Maven) | 4.0.0 | Java |
| `react-native/background-fetch` | `react-native-background-fetch` (npm) | 4.2.8 | TS · Java · ObjC |
| `cordova/background-fetch` | `cordova-plugin-background-fetch` (npm) | 7.2.4 | JS · Java · ObjC |
| `capacitor/background-fetch` | `@transistorsoft/capacitor-background-fetch` (npm) | 8.0.1 | TS · Java · Swift |
| `flutter/background_fetch` | `background_fetch` (pub.dev) | 1.5.1 | Dart · Java · ObjC |

---

## Quick start

```bash
# Full clone (all submodules)
git clone --recurse-submodules git@github.com:transistorsoft/fetch-forge.git
cd fetch-forge
./setup

# Partial clone (only the platforms you need)
git clone git@github.com:transistorsoft/fetch-forge.git
cd fetch-forge
git submodule update --init native/background-fetch react-native/background-fetch
```

`./setup` initialises all submodules and installs npm dependencies for the JS platforms.

---

## Forge CLI

```bash
./forge help                     # command reference
./forge status                   # version table + submodule overview
```

### Build

```bash
./forge build ios                # XCFramework → native/background-fetch/build/Release-Publish/
./forge build android            # assembleRelease AAR
./forge build native             # both ios + android
./forge build react-native       # npm run build (Expo plugin tsc)
./forge build capacitor          # npm run build (tsc + rollup)
./forge build cordova            # npm run build (tsc)
./forge build flutter            # (no-op — pub package)
./forge build all                # everything
```

### Publish

```bash
./forge publish native ios                 # CocoaPods + SPM release
./forge publish native android             # Maven Central (Sonatype)
./forge publish native android --snapshot  # Sonatype SNAPSHOT
./forge publish react-native               # npm publish
./forge publish capacitor                  # npm publish
./forge publish cordova                    # npm publish
./forge publish flutter                    # dart pub publish
```

> **Note:** `forge publish` will fail if any local iOS links are active. Run `forge unlink-ios all` first.

### Local iOS development

```bash
./forge build ios                  # build XCFramework locally first
./forge link-ios react-native      # symlink framework into example app Pods
./forge link-ios flutter           # same for Flutter
./forge link-ios capacitor         # same for Capacitor
./forge link-ios cordova           # same for Cordova
./forge link-ios all               # all of the above

./forge unlink-ios all             # restore remote deps before committing or publishing
```

Auto-detects CocoaPods vs SPM per target. For CocoaPods it symlinks into `Pods/TSBackgroundFetch/`; for SPM it rewrites `Package.swift` to use a local `binaryTarget` path.

---

## Submodule workflow

Changes to platform SDKs must be committed **inside** the submodule, then the parent repo's pointer is updated separately.

```bash
# 1. Work inside the submodule
cd react-native/background-fetch
git checkout -b fix/my-bug
# ... edit, stage, commit, push ...

# 2. Update the parent pointer
cd ../..
git add react-native/background-fetch
git commit -m "chore: bump react-native submodule"
```

To initialise a submodule that wasn't checked out:

```bash
git submodule update --init react-native/background-fetch
```

**Never `git add` or commit files inside a submodule from the parent repo.**

---

## Native iOS — build & release

The native XCFramework supports iOS device, iOS Simulator, and Mac Catalyst.

### Build

```bash
./forge build ios
# Output: native/background-fetch/build/Release-Publish/TSBackgroundFetch_XCFramework_<VERSION>/TSBackgroundFetch.xcframework
```

Options are forwarded to `build-ios.sh`:

```bash
./forge build ios --version 4.1.0    # override version
./forge build ios --no-catalyst      # skip Mac Catalyst slice
```

### Test locally

```bash
./forge build ios
./forge link-ios react-native        # or flutter / capacitor / cordova / all
# → open the example app in Xcode and build
./forge unlink-ios all               # restore before committing
```

| Platform | Example Xcode project |
|---|---|
| React Native | `react-native/background-fetch/example/ios/FetchDemo.xcworkspace` |
| Capacitor | `capacitor/background-fetch/example/ios/App/App.xcworkspace` |
| Flutter | `flutter/background_fetch/example/ios/Runner.xcworkspace` |

### Release

```bash
./forge publish native ios
```

This runs `native/background-fetch/scripts/publish-ios.sh`, which:
1. Builds the XCFramework
2. Packages and uploads to GitHub Releases
3. Updates `Package.swift` checksum
4. Pushes the CocoaPods podspec

---

## Native Android — build & release

### Build

```bash
./forge build android
# Runs: cd native/background-fetch/android && ./gradlew :tsbackgroundfetch:assembleRelease
```

### Release

```bash
./forge publish native android             # release to Maven Central
./forge publish native android --snapshot  # publish SNAPSHOT to Sonatype
```

The version is controlled in `native/background-fetch/android/versioning/tsbackgroundfetch.properties`.

---

## Platform plugins — build & release

### React Native

```bash
cd react-native/background-fetch
yarn install
./forge build react-native     # compiles Expo plugin TypeScript
./forge publish react-native   # npm publish
```

### Cordova

```bash
cd cordova/background-fetch
npm install
./forge build cordova          # tsc
./forge publish cordova        # npm publish
```

### Capacitor

```bash
cd capacitor/background-fetch
npm install
./forge build capacitor        # tsc + rollup → dist/
./forge publish capacitor      # npm publish
```

### Flutter

Flutter is a pure Dart package — no build step.

```bash
./forge publish flutter        # dart pub publish
```

---

## Versioning

Each platform maintains its own version independently:

| Platform | Version source |
|---|---|
| iOS | `native/background-fetch/TSBackgroundFetch.podspec` → `s.version` |
| Android | `native/background-fetch/android/versioning/tsbackgroundfetch.properties` → `VERSION_NAME` |
| React Native | `react-native/background-fetch/package.json` → `version` |
| Cordova | `cordova/background-fetch/package.json` → `version` |
| Capacitor | `capacitor/background-fetch/package.json` → `version` |
| Flutter | `flutter/background_fetch/pubspec.yaml` → `version` |

Check all versions at once:

```bash
./forge status
```
