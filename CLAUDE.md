# Fetch Forge — Background Fetch SDK monorepo

One repo for Transistor Software's Background Fetch SDK ecosystem.
5 git submodules organised by platform, with a central `forge` CLI.

## Repo map

```
native/background-fetch/       Native iOS + Android core — TSBackgroundFetch
                               (CocoaPods podspec + SPM Package.swift + Android Gradle/Maven)
types/background-fetch/        Shared TypeScript types (@transistorsoft/background-fetch-types)
react-native/background-fetch/ React Native SDK
cordova/background-fetch/      Cordova plugin
capacitor/background-fetch/    Capacitor plugin
flutter/background_fetch/      Flutter/Dart SDK
docforge/                      Documentation site generator (MkDocs Material)
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

### Shared types

`types/background-fetch/` is `@transistorsoft/background-fetch-types` — shared TypeScript
interfaces and type aliases (`BackgroundFetchConfig`, `TaskConfig`, `HeadlessEvent`,
`BackgroundFetchStatus`, `NetworkType`). Used by Capacitor and Cordova plugins to avoid
duplicated type definitions.

### Cordova TypeScript

Cordova's `src/typescript/index.ts` is the canonical source. It compiles to `www/index.js`
(CommonJS, `module.exports = BackgroundFetch`) which Cordova loads via `plugin.xml`'s
`<js-module>` / `<clobbers target="window.BackgroundFetch" />`. The TypeScript source
imports types from `@transistorsoft/background-fetch-types` and calls `cordova/exec` directly.

## Forge CLI

`./forge` at the repo root is the unified CLI.

```bash
# Build
./forge build ios              # XCFramework (signed, zipped, checksummed)
./forge build android          # Android AAR
./forge build native           # both
./forge build react-native     # npm run build
./forge build cordova          # tsc → www/index.js
./forge build all              # everything

# Publish — Native iOS
./forge publish native ios --bump patch              # bump, build, publish
./forge publish native ios --version 4.0.7           # explicit version
./forge publish native ios --push-cocoapods          # include CocoaPods trunk push
./forge publish native ios --create-branch --create-pr  # release branch workflow
./forge publish native ios --dry-run                 # preview without publishing

# Publish — Native Android
./forge publish native android --bump patch          # bump, changelog, test, publish
./forge publish native android --snapshot            # SNAPSHOT (auto-bumps patch)
./forge publish native android --dry-run             # preview
./forge publish native android --skip-tests          # skip unit tests
./forge publish native android --local               # publish to mavenLocal

# Publish — Platform SDKs
./forge publish types          # npm publish (@transistorsoft/background-fetch-types)
./forge publish react-native   # npm publish
./forge publish cordova        # npm publish
./forge publish capacitor      # npm publish
./forge publish flutter        # dart pub publish

# Docs
./forge docs serve             # live-reloading dev server on port 8082
./forge docs site              # build static site
./forge publish docs           # push to gh-pages → fetch.transistorsoft.com

# Local iOS development (link built XCFramework into example apps)
./forge link-ios react-native  # or: flutter, capacitor, cordova, all
./forge unlink-ios all         # restore remote deps before publishing

# Status
./forge status                 # version table + submodule overview
```

## Native versioning

Both platforms use properties files as the single source of truth:

- **iOS**: `native/background-fetch/versioning/TSBackgroundFetch.properties`
  (`MARKETING_VERSION` / `CURRENT_PROJECT_VERSION`)
- **Android**: `native/background-fetch/android/versioning/tsbackgroundfetch.properties`
  (`VERSION_NAME` / `VERSION_CODE`)

The publish scripts manage these automatically via `--bump` / `--version` flags.

## Native iOS build + publish

Scripts live at `native/background-fetch/scripts/`:

- **`build-ios.sh`** — Archives iOS device + simulator (+ optional Catalyst dual-arch),
  creates XCFramework, signs it (`Apple Distribution: 9224-2932 Quebec Inc`),
  zips + computes SPM checksum. Reads version from properties file.
  Flags: `--quiet`, `--verbose`, `--catalyst`, `--version X.Y.Z`

- **`publish-ios.sh`** — Full publish pipeline: version bump, changelog generation
  (opens `$EDITOR`), build, zip, GitHub Release + asset upload, Package.swift +
  podspec update, tag, push, post-upload checksum verification.
  Flags: `--version`, `--bump`, `--push-cocoapods`, `--create-branch`, `--create-pr`,
  `--auto-merge`, `--dry-run`, `--yes`, `--retag`, `--no-build`

## Native Android build + publish

- **`publish-android.sh`** — Bump version (Gradle task), stamp changelog, run tests,
  build, publish to Maven Central (Sonatype Central Portal).
  Flags: `--bump patch|minor|major`, `--snapshot`, `--dry-run`, `--skip-tests`,
  `--local`, `--no-bump`

- Gradle version bump tasks: `./gradlew :tsbackgroundfetch:bumpVersionPatch` (also `Minor`, `Major`)

- Sonatype endpoints: Central Portal API (`ossrh-staging-api.central.sonatype.com`)

## Local iOS development workflow

To test iOS changes without publishing:

1. `./forge build ios` — builds XCFramework to `native/background-fetch/build/Release-Publish/`
2. `./forge link-ios <platform>` — auto-detects CocoaPods vs SPM and links the framework
3. Build and run the example app in Xcode
4. `./forge unlink-ios <platform>` — restore production references before committing or publishing

## Documentation site

The docs site at `fetch.transistorsoft.com` is built with MkDocs Material.

- Source: `docforge/` (generates MkDocs config + content from `db/` YAML files)
- Deploy: `./forge publish docs` (pushes to `gh-pages` branch with CNAME preservation)
- Google Analytics: `G-4NNZKTE395`

## Bug fix workflow

Before writing or committing any fix, walk me through your reasoning step by step so I can approve the approach. Specifically:

1. **Diagnosis** — What is the root cause? Show the specific code path that fails and explain why.
2. **Proposed fix** — Describe the change in plain English. Which files, which methods, what the change does.
3. **Scope check** — What else calls this code? Could the fix break existing behavior or other callers?
4. **Platform parity** — Does the same bug exist on the other platform (iOS/Android)? Should both be fixed together?
5. **Test plan** — How will you verify the fix? Existing tests, new tests, or manual steps?
6. **Wait for approval** — Do not write code or commit until I confirm the plan.

**Before every commit:** Show the diff and wait for explicit confirmation. Never commit without asking first.
