# Fetch Forge

Monorepo for Transistor Software's Background Fetch SDK ecosystem.
5 git submodules organised by platform, with a central `forge` CLI.

## Repo map

```
native/background-fetch/      Native iOS + Android core (TSBackgroundFetch)
react-native/background-fetch/ React Native SDK
cordova/background-fetch/      Cordova plugin
capacitor/background-fetch/    Capacitor plugin
flutter/background_fetch/      Flutter/Dart SDK
forge                          Unified CLI
```

## Quick start

```bash
git clone --recurse-submodules git@github.com:transistorsoft/fetch-forge.git
cd fetch-forge
./setup
```

Or, to initialise only the submodules you need:

```bash
git clone git@github.com:transistorsoft/fetch-forge.git
cd fetch-forge
git submodule update --init native/background-fetch react-native/background-fetch
```

## Submodule workflow

All platform directories are git submodules with independent remotes.

```bash
# Always commit from WITHIN the submodule
cd react-native/background-fetch
git checkout -b feature/my-change
# ... edit, stage, commit, push ...

# Then update the parent repo's pointer
cd ../..
git add react-native/background-fetch
git commit -m "chore: bump react-native submodule"
```

**Never commit submodule content from the parent repo.**

## Forge CLI

```
forge build <target>           Build a platform SDK
forge publish native ios       Publish iOS (CocoaPods + SPM)
forge publish native android   Publish Android to Maven Central
forge publish <platform>       Publish platform SDK to npm/pub.dev
forge link-ios <target>        Use local XCFramework in example app
forge unlink-ios <target>      Undo link-ios
forge status                   Version + submodule overview
forge help                     Full usage
```

Run `forge help <domain>` for details on any command.

## Platform summary

| Platform | Package | Languages |
|---|---|---|
| native (iOS) | `TSBackgroundFetch` (CocoaPods/SPM) | Objective-C |
| native (Android) | `com.transistorsoft:tsbackgroundfetch` (Maven) | Java |
| react-native | `react-native-background-fetch` | TS/JS + Java + ObjC |
| cordova | `cordova-plugin-background-fetch` | JS + Java + ObjC |
| capacitor | `@transistorsoft/capacitor-background-fetch` | TS + Java + Swift |
| flutter | `flutter_background_fetch` | Dart + Java + ObjC |
