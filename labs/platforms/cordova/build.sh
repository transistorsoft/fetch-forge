#!/usr/bin/env bash
# Cordova fetch-lab build (WO-002).  usage: build.sh <native label> <plugin checkout|default>
#
# Modelled on bg-forge's labs/platforms/cordova/build.sh (addc50501), with the fetch hosts' staging and native-pin
# proofs (labs/platforms/capacitor/build.sh, labs/platforms/flutter/build.sh).  LAB_OS (run.py sets it from the
# platform view's OS) picks the branch: android (the default) or ios, the Simulator app, on Swift Package Manager only
# (Chris, 2026-11-15: on cordova-ios 8 the plugin builds from its Package.swift alone).
#
#   <native label>   Android: a tsbackgroundfetch snapshot published into labs/m2 (labs/m2/.provenance/<label>.txt).
#                    The app resolves com.transistorsoft:tsbackgroundfetch:<label> from labs/m2 ONLY, at that version
#                    whatever the plugin asks for (app/lab-native.gradle), and this script refuses an APK whose resolved
#                    version is not the label or whose AAR is not byte-identical to labs/m2's.
#                    iOS: the Simulator snapshot of the same label in LAB_XCFRAMEWORKS, resolved through a SwiftPM mirror
#                    of the label (the other iOS hosts' recipe); this script refuses an app whose linked slice is not it.
#   <plugin>         `default` (lab.yaml default_checkout: <repo>/cordova/background-fetch) or a path to another checkout
#                    of the Cordova plugin.
#
# Cordova is the one host whose buildable project is not in the checkout: `cordova platform add` writes
# app/platforms/ and `cordova plugin add` writes app/plugins/ (both gitignored, and skipped by run.py's source stamp).
# bg-forge's doctrine, kept: no decision goes through `cordova build` or prepare.  The project is generated when its
# inputs change (platforms/android/.lab-host: the cordova-android installed from the committed lock, the CLI, config.xml);
# the plugin under test is re-added on EVERY build (a stale one would build in silence); the lab's native sources and the
# driver are copied in by hand; ./gradlew builds.  The one `cordova build` bootstraps the Gradle wrapper on a fresh
# platform (cordova-android ships none), before anything this script proves is in place.
#
# The plugin is STAGED, never added from the checkout: its working-tree files (git ls-files -co --exclude-standard, minus
# example/) are copied into ../.stage/cordova-background-fetch (gitignored), and THAT is what `cordova plugin add` takes
# (npm 11 links a directory dependency: adding the checkout would let npm and cordova write into the submodule).  The
# stage records what it was copied from (.lab-source); the build refuses a stage that does not name the checkout.
set -euo pipefail
native="${1:?usage: build.sh <native label> <plugin checkout|default>}"; plugin="${2:-default}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LABS="$(cd "$HERE/../.." && pwd)"
REPO="$(cd "$LABS/.." && pwd)"
APP="$HERE/app"
PKG="com.transistorsoft.fetchlab.cordova"
CORDOVA_ANDROID="15.0.0"            # app/package.json's exact devDependencies, installed from the committed lock
CORDOVA_IOS="8.1.1"
die() { echo "[lab] $*" >&2; exit 1; }

OS="${LAB_OS:-android}"
case "$OS" in
  android|ios) ;;
  *) die "LAB_OS=$OS: this lab builds android or ios" ;;
esac

# --- the native snapshot -------------------------------------------------------------------------------------------

if [ "$OS" = android ]; then
  M2="$LABS/m2"
  PROV="$M2/.provenance/$native.txt"
  MOD="$M2/com/transistorsoft/tsbackgroundfetch/$native"
  AAR="$MOD/tsbackgroundfetch-$native.aar"
  [ -f "$PROV" ] || die "no Android snapshot labelled '$native': $PROV is missing — publish it into labs/m2 first"
  [ -f "$AAR" ] && [ -f "$MOD/tsbackgroundfetch-$native.pom" ] \
    || die "labs/m2 has provenance for '$native' but no tsbackgroundfetch-$native.aar/.pom under $MOD"
  aar_want="$(shasum -a 256 "$AAR" | cut -d' ' -f1)"
else
  # (WO-002, iOS) The labelled Simulator snapshot labs/harness/publish_native_fetch_ios.sh wrote (the other iOS hosts'
  # store).  SwiftPM only (Chris, 2026-11-15): a variant with no `channel:` (FIX) is every host's default build, and
  # this host's one build is SwiftPM, so FIX and FIX-SPM build the same app (lab.yaml's notes).
  BUNDLE="${LAB_BUNDLE:?set LAB_BUNDLE to the bundle id lab.yaml names}"
  XCFW="${LAB_XCFRAMEWORKS:?set LAB_XCFRAMEWORKS to the labelled xcframework store}"
  CHANNEL="${LAB_CHANNEL:-spm}"
  SNAPSHOT="$XCFW/$native"
  PROV="$XCFW/.provenance/$native.txt"
  [ -d "$SNAPSHOT/TSBackgroundFetch.xcframework" ] && [ -f "$SNAPSHOT/Package.swift" ] && [ -f "$PROV" ] \
    || die "no iOS snapshot labelled '$native' under $XCFW — publish it: labs/harness/publish_native_fetch_ios.sh $native"
  [ "$CHANNEL" = spm ] || die "LAB_CHANNEL=$CHANNEL: the Cordova iOS host builds spm only (lab.yaml's ios build.channels)"
fi
prov() { sed -n "s/^$1:[[:space:]]*//p" "$PROV" | head -1; }

# --- the toolchain -------------------------------------------------------------------------------------------------

command -v cordova >/dev/null || die "the cordova CLI is not on PATH (npm i -g cordova)"
cli="$(cordova --version 2>/dev/null | head -1 | tr -d '[:space:]')"
[ "${cli%%.*}" -ge 13 ] 2>/dev/null || die "cordova CLI '${cli:-unknown}' is too old for cordova-android $CORDOVA_ANDROID (need >= 13)"
if [ "$OS" = android ]; then
  if [ -z "${ANDROID_HOME:-}" ]; then
    for candidate in "${ANDROID_SDK_ROOT:-}" "$HOME/Library/Android/sdk" "$HOME/Android/Sdk"; do
      if [ -n "$candidate" ] && [ -d "$candidate" ]; then ANDROID_HOME="$candidate"; break; fi
    done
  fi
  [ -n "${ANDROID_HOME:-}" ] || die "set ANDROID_HOME: cordova platform add needs it"
  export ANDROID_HOME ANDROID_SDK_ROOT="$ANDROID_HOME"
else
  # cordova-ios 8 still checks for CocoaPods >= 1.16 and runs `pod install` on every plugin add and rm, even in
  # SwiftPM mode (the plugin's <podspec> carries a <source>; only its <pod> is nospm): the customer's path, kept.  LANG:
  # cordova shells out to pod with no locale, and CocoaPods then dies in UnicodeNormalize (bg-forge MEASURED).
  command -v pod >/dev/null || die "CocoaPods is not installed: cordova-ios 8 requires it (>= 1.16) even for a SwiftPM plugin"
  export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
  pod_v="$(pod --version 2>/dev/null | tail -1)"
  python3 -c 'import sys; v=[int(x) for x in sys.argv[1].split(".")[:2]]; sys.exit(0 if v >= [1, 16] else 1)' "$pod_v" \
    || die "CocoaPods $pod_v: cordova-ios $CORDOVA_IOS needs >= 1.16"
fi

# --- the plugin checkout, staged -----------------------------------------------------------------------------------

if [ "$plugin" = "default" ] || [ -z "$plugin" ]; then
  want="$REPO/cordova/background-fetch"      # lab.yaml default_checkout
else
  want="$plugin"
fi
want_real="$(cd "$want" 2>/dev/null && pwd -P)" || die "no plugin checkout at $want"
git -C "$want_real" rev-parse --show-toplevel >/dev/null 2>&1 || die "$want_real is not a git checkout"
grep -q 'id="cordova-plugin-background-fetch"' "$want_real/plugin.xml" 2>/dev/null \
  || die "$want_real is not the cordova-plugin-background-fetch Cordova plugin"

STAGE_ROOT="$HERE/.stage"
STAGE="$STAGE_ROOT/cordova-background-fetch"
mkdir -p "$STAGE"

# stage <checkout> <stage>: copy the checkout's working-tree files in, drop what it no longer has, and write
# .lab-source.  verify <checkout> <stage>: the stage still holds exactly those bytes and names that checkout.  Read-only
# on the checkout: GIT_OPTIONAL_LOCKS=0 keeps `git status` from refreshing its index.  (The other hosts' stage_py.)
stage_py() {
  GIT_OPTIONAL_LOCKS=0 python3 - "$@" <<'PY'
import hashlib, os, shutil, subprocess, sys
mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
KEEP = ("node_modules/",)                                                                 # only the stage ever has these
SKIP = ("example/",)                                                                      # the demo app is not the plugin

def git(*a):
    return subprocess.run(["git", "-C", src] + list(a), capture_output=True, check=True).stdout.decode()

names = sorted(n for n in git("ls-files", "-co", "--exclude-standard", "-z").split("\0")
               if n and not n.startswith(SKIP) and os.path.isfile(os.path.join(src, n))
               and not os.path.islink(os.path.join(src, n)))

def digest(root):
    h = hashlib.sha1()
    for n in names:
        with open(os.path.join(root, n), "rb") as f:
            h.update(n.encode() + b"\0" + hashlib.sha1(f.read()).digest())
    return h.hexdigest()

head = git("rev-parse", "HEAD").strip()
dirty = "yes" if git("status", "--porcelain").strip() else "no"
content = digest(src)
source = "checkout=%s head=%s dirty=%s files=%d content=%s\n" % (src, head, dirty, len(names), content)

if mode == "stage":
    copied = removed = 0
    for n in names:
        s, d = os.path.join(src, n), os.path.join(dst, n)
        with open(s, "rb") as f:
            data = f.read()
        if os.path.isfile(d) and not os.path.islink(d):
            with open(d, "rb") as f:
                if f.read() == data:
                    continue
        os.makedirs(os.path.dirname(d), exist_ok=True)
        if os.path.lexists(d):
            os.remove(d)
        shutil.copy2(s, d)
        copied += 1
    keep = set(names) | {".lab-source"}
    for root, dirs, files in os.walk(dst, topdown=True):
        rel = os.path.relpath(root, dst)
        rel = "" if rel == "." else rel + "/"
        dirs[:] = [d for d in dirs if not (rel + d + "/").startswith(KEEP)]
        for f in files:
            if rel + f not in keep:
                os.remove(os.path.join(root, f))
                removed += 1
    for root, dirs, files in os.walk(dst, topdown=False):
        if root != dst and not os.listdir(root):
            os.rmdir(root)
    with open(os.path.join(dst, ".lab-source"), "w") as f:
        f.write(source)
    print("[lab] staged %s (%d files, %d copied, %d removed) head=%s dirty=%s content=%s"
          % (src, len(names), copied, removed, head[:12], dirty, content[:12]))
else:
    recorded = open(os.path.join(dst, ".lab-source")).read() if os.path.exists(os.path.join(dst, ".lab-source")) else ""
    if recorded != source:
        sys.exit("[lab] the stage's .lab-source (%s) does not name %s as it is now (%s)"
                 % (recorded.strip() or "none", src, source.strip()))
    missing = [n for n in names if not os.path.isfile(os.path.join(dst, n))]
    if missing or digest(dst) != content:
        sys.exit("[lab] the stage's files are not %s's (%s)" % (src, ", ".join(missing[:5]) or "content differs"))
    print("[lab] plugin %s head=%s dirty=%s content=%s" % (src, head[:12], dirty, content[:12]))
PY
}
stage_py stage "$want_real" "$STAGE"
[ -f "$STAGE/plugin.xml" ] && [ -f "$STAGE/www/index.js" ] || die "the stage has no plugin.xml or www/index.js"
if [ "$OS" = android ]; then
  [ -f "$STAGE/src/android/CDVBackgroundFetch.java" ] || die "the stage has no src/android/CDVBackgroundFetch.java"
else
  [ -f "$STAGE/Package.swift" ] && [ -f "$STAGE/src/ios/CDVBackgroundFetch.m" ] && [ -f "$STAGE/src/ios/CDVBackgroundFetch.h" ] \
    || die "the stage has no Package.swift or src/ios/CDVBackgroundFetch.{h,m}"
fi

# --- the project: cordova-android from the lock, generated when its inputs change ---------------------------------

cd "$APP"
# What cordova must never rewrite (each is tracked and hashed into run.py's stamp), by content: checked after the plugin
# adds below, committed or not.
tracked_sums() { shasum package.json package-lock.json config.xml; }
before="$(tracked_sums)"
npm ci --prefer-offline --no-audit --no-fund > "$STAGE_ROOT/npm-ci.log" 2>&1 \
  || { tail -20 "$STAGE_ROOT/npm-ci.log"; die "npm ci failed ($STAGE_ROOT/npm-ci.log)"; }

# --- iOS (WO-002): a generated Cordova project of its own, on Swift Package Manager ----------------------------------
#
# A separate project, app/build/ios-spm (gitignored, under a directory run.py's source stamp skips), not a second
# platform in app/: `cordova plugin add/rm` act on EVERY installed platform, so a shared project would put every Android
# build through the iOS plugin install and its `pod install`.  It sits under app/ so cordova-fetch takes the cordova-ios
# that `npm ci` installed into app/node_modules from the committed lock.  Cordova takes the first ancestor holding
# www + config.xml + platforms/ for the project root, so platforms/ is made before any cordova command.  Regenerated with
# rm -rf (everything goes, the generated project's own state included), never `platform rm`.  Derived data, the cloned
# packages and the per-label mirror sit outside platforms/ios: regeneration deletes it, and every plugin add globs
# **/*Info.plist across it.

if [ "$OS" = ios ]; then
  PB=/usr/libexec/PlistBuddy
  marketing="$(prov marketing)"; build_no="$(prov build)"; sha_want="$(prov sha256_sim)"
  [ -n "$marketing" ] && [ -n "$build_no" ] && [ -n "$sha_want" ] || die "$PROV lacks marketing/build/sha256_sim"
  cio="$(node -p "require('cordova-ios/package.json').version" 2>/dev/null || true)"
  [ "$cio" = "$CORDOVA_IOS" ] || die "app/node_modules has cordova-ios '${cio:-none}', not $CORDOVA_IOS (app/package.json, the lock)"
  H="$APP/build/ios-spm"
  IOS="$H/platforms/ios"
  DD="$APP/build/ios-spm-dd"
  LOGS="$APP/build/ios-spm-logs"
  mkdir -p "$LOGS"

  host="cordova-ios=$cio cordova-cli=$cli config=$(shasum -a 1 config.xml | cut -d' ' -f1)"
  if [ "$(cat "$IOS/.lab-host" 2>/dev/null)" != "$host" ]; then
    echo "[lab] cordova platform add ios ($host)"
    rm -rf "${H:?}" "${DD:?}"
    mkdir -p "$H/platforms" "$H/www"
    cp config.xml "$H/config.xml"
    cp www/index.html "$H/www/index.html"
    # No devDependencies: cordova-fetch npm-installs a plugin into this project, and npm would then reify any platform
    # package listed here from the registry; cordova-ios resolves from app/node_modules instead.
    printf '{\n  "name": "%s",\n  "version": "1.0.0",\n  "private": true,\n  "cordova": {"platforms": [], "plugins": {}}\n}\n' \
      "$PKG" > "$H/package.json"
    tree="$(shasum node_modules/.package-lock.json | cut -d' ' -f1)"
    (cd "$H" && cordova platform add "ios@$CORDOVA_IOS" --nosave) > "$LOGS/platform-add.log" 2>&1 \
      || { tail -20 "$LOGS/platform-add.log"; die "cordova platform add ios failed ($LOGS/platform-add.log)"; }
    [ "$(shasum node_modules/.package-lock.json | cut -d' ' -f1)" = "$tree" ] \
      || die "cordova platform add ios changed app/node_modules"
    # cordova-fetch installs a platform it cannot resolve into the PROJECT's node_modules ($H, whose package.json makes
    # it npm's prefix), so that is where a self-installed cordova-ios would be; and the platform names its version.
    [ ! -e "$H/node_modules/cordova-ios" ] \
      || die "cordova-fetch installed cordova-ios into $H/node_modules itself, not the lock's app/node_modules copy"
    grep -q "PLATFORM_VERSION_BUILD_LABEL = '$CORDOVA_IOS'" "$IOS/platform_www/cordova.js" \
      || die "the generated platform is not cordova-ios $CORDOVA_IOS ($IOS/platform_www/cordova.js)"
    [ -d "$IOS/App.xcworkspace" ] || die "cordova platform add ios made no $IOS/App.xcworkspace (did it take app/ for the root?)"
    [ ! -d "$APP/platforms/ios" ] || die "cordova generated app/platforms/ios: it took app/ for the project root"
    echo "$host" > "$IOS/.lab-host"
  fi

  # The plugins, re-added every build (`plugin rm` failures are not fatal: there may be nothing to remove; the install
  # lines below are what prove the adds).
  for id in cordova-plugin-background-fetch lab-plugin; do
    (cd "$H" && cordova plugin rm "$id" --nosave --force) > /dev/null 2>&1 || true
  done
  (cd "$H" && cordova plugin add "$STAGE" --nosave && cordova plugin add "$APP/lab-plugin" --nosave) \
    > "$LOGS/plugin-add.log" 2>&1 || { tail -20 "$LOGS/plugin-add.log"; die "cordova plugin add (ios) failed ($LOGS/plugin-add.log)"; }
  [ "$(tracked_sums)" = "$before" ] \
    || die "cordova rewrote app/package.json, package-lock.json or config.xml (tracked, and hashed into run.py's stamp)"
  for id in cordova-plugin-background-fetch lab-plugin; do
    grep -q "Installing \"$id\" for ios" "$LOGS/plugin-add.log" \
      || die "cordova did not install $id for ios in this build ($LOGS/plugin-add.log)"
  done

  # --- what cordova installed: the plugin as a Swift package (the stage's), the lab plugin in the App target ----------
  P="$IOS/packages/cordova-plugin-background-fetch"
  for f in src/ios/CDVBackgroundFetch.h src/ios/CDVBackgroundFetch.m; do
    cmp -s "$STAGE/$f" "$P/$f" || die "the installed package's $f is not the stage's"
  done
  # Its Package.swift is the stage's but for the one line cordova-ios rewrites (SwiftPackage.js: the cordova-ios URL
  # dependency becomes the local CordovaLib copy).
  python3 - "$STAGE/Package.swift" "$P/Package.swift" <<'PY'
import sys
a, b = open(sys.argv[1]).read().splitlines(), open(sys.argv[2]).read().splitlines()
diff = [(x, y) for x, y in zip(a, b) if x != y]
if len(a) != len(b) or len(diff) != 1 or diff[0][1].strip() != '.package(name: "cordova-ios", path: "../cordova-ios"),':
    sys.exit("[lab] the installed package's Package.swift is not the stage's with cordova-ios's one rewrite: %r" % diff[:3])
PY
  CP="$IOS/packages/cordova-ios-plugins/Package.swift"
  grep -q 'package.dependencies.append(.package(name: "cordova-plugin-background-fetch", path: "../cordova-plugin-background-fetch"))' "$CP" \
    && grep -q '\.iOS("15.0")\|\.iOS(.v15)' "$CP" \
    || die "the CordovaPlugins package does not name the plugin at iOS 15 ($CP): config.xml's deployment-target did not land"
  [ ! -e "$IOS/App/Plugins/cordova-plugin-background-fetch" ] \
    || die "cordova installed the plugin's sources into the App target: it was not taken as a Swift package"
  cmp -s "$APP/lab-plugin/src/apple/LabPlugin.m" "$IOS/App/Plugins/lab-plugin/LabPlugin.m" \
    || die "the installed LabPlugin.m is not app/lab-plugin/src/apple/LabPlugin.m"
  # CocoaPods ran (cordova-ios runs it) but installed nothing: no pod, and no TSBackgroundFetch from it.
  ! grep -q "^[[:space:]]*pod '" "$IOS/Podfile" 2>/dev/null || die "the generated Podfile names a pod: $(grep "pod '" "$IOS/Podfile" | head -1)"
  ! grep -q "TSBackgroundFetch" "$IOS/Podfile.lock" 2>/dev/null || die "CocoaPods installed TSBackgroundFetch (Podfile.lock)"

  # --- the overlay, the plist, the driver (LAST: a prepare would put the project's www back) -------------------------
  cp native-ios/AppDelegate.swift "$IOS/App/AppDelegate.swift"
  PLIST="$IOS/App/App-Info.plist"
  [ -f "$PLIST" ] || die "no $PLIST — cordova-ios changed its project layout"
  modes="$($PB -c "Print :UIBackgroundModes" "$PLIST" 2>/dev/null || true)"
  for m in fetch processing; do
    grep -qx "[[:space:]]*$m" <<<"$modes" || die "UIBackgroundModes has no '$m' — the plugin's own Info.plist munge did not run"
  done
  ids="$($PB -c "Print :BGTaskSchedulerPermittedIdentifiers" "$PLIST" 2>/dev/null || true)"
  grep -qx "[[:space:]]*com.transistorsoft.fetch" <<<"$ids" || die "BGTaskSchedulerPermittedIdentifiers lacks the plugin's com.transistorsoft.fetch"
  grep -qx "[[:space:]]*com.transistorsoft.fetchlab.task" <<<"$ids" || die "BGTaskSchedulerPermittedIdentifiers lacks config.xml's com.transistorsoft.fetchlab.task"
  cp www/index.html "$IOS/www/index.html"
  for f in labapi.js labevents.js logo.js; do cp "$LABS/platforms/_shared/$f" "$IOS/www/$f"; done

  # --- the label as the package the plugin asks for (the other iOS hosts' mirror) -----------------------------------
  # The plugin's Package.swift wants .package(url: "https://github.com/transistorsoft/transistor-background-fetch.git",
  # from: "4.2.0"): a SwiftPM mirror maps that URL to a local git repo of the label, tagged with its iOS version, so the
  # plugin's manifest is never edited and its own requirement is what resolves.  One mirror per label, its commit made
  # with fixed dates.  mirrors.json in both containers (the Flutter host's MEASURED recipe); the build is the
  # workspace's, cordova's own `-workspace App.xcworkspace -scheme App`.
  SRCPKG="$DD/SourcePackages"
  RES_WS="$IOS/App.xcworkspace/xcshareddata/swiftpm/Package.resolved"
  RES_PJ="$IOS/App.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
  MIRROR="$APP/build/lab-mirror/$native/transistor-background-fetch"
  mirror_source="label=$native commit=$(prov commit) sha256_sim=$sha_want marketing=$marketing"
  if [ "$(cat "$MIRROR.lab-source" 2>/dev/null)" != "$mirror_source" ] \
     || ! git -C "$MIRROR" rev-parse -q --verify "refs/tags/$marketing" >/dev/null 2>&1; then
    echo "[lab] SwiftPM mirror of $native (tag $marketing)"
    rm -rf "${MIRROR:?}" "$MIRROR.lab-source" "${SRCPKG:?}" "$RES_WS" "$RES_PJ"
    mkdir -p "$MIRROR"
    (cd "$SNAPSHOT" && tar --exclude .swiftpm --exclude .build -cf - .) | tar -x -C "$MIRROR"
    git -C "$MIRROR" init -q
    git -C "$MIRROR" add -A
    GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000" GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000" \
      git -C "$MIRROR" -c user.email=lab@fetch-forge.invalid -c user.name=labkit -c commit.gpgsign=false commit -qm "$mirror_source"
    git -C "$MIRROR" -c tag.gpgsign=false tag "$marketing"
    echo "$mirror_source" > "$MIRROR.lab-source"
  fi
  mirrors="$(printf '{"object":[{"mirror":"file://%s","original":"https://github.com/transistorsoft/transistor-background-fetch.git"}],"version":1}' \
    "$(cd "$MIRROR" && pwd -P)")"
  # Another label's mirror, or another stage content: the checkouts and pins go.
  spm_inputs="$mirrors $(sed -n 's/.* content=\([0-9a-f]*\).*/\1/p' "$STAGE/.lab-source")"
  if [ "$(cat "$DD/.lab-spm-inputs" 2>/dev/null)" != "$spm_inputs" ]; then
    rm -rf "${SRCPKG:?}" "$RES_WS" "$RES_PJ"
  fi
  for cfg in "$IOS/App.xcworkspace/xcshareddata/swiftpm/configuration" \
             "$IOS/App.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/configuration"; do
    mkdir -p "$cfg"
    printf '%s\n' "$mirrors" > "$cfg/mirrors.json"
  done

  # --- build ---------------------------------------------------------------------------------------------------------
  # The app lands where the template's DSTROOT puts it (platforms/ios/build/Debug-iphonesimulator, its own
  # DEPLOYMENT_LOCATION settings); derived data holds a symlink to it.  Cleared first, so a failed build leaves no app.
  OUT="$APP/build/lab-ios/FetchCordovaLab.app"
  rm -f "$OUT.lab-stamp"
  rm -rf "$IOS/build/Debug-iphonesimulator"
  mkdir -p "$DD"
  echo "[lab] xcodebuild (Swift Package Manager) — $LOGS/xcodebuild.log"
  xcodebuild -workspace "$IOS/App.xcworkspace" -scheme App -configuration Debug -sdk iphonesimulator -arch "$(uname -m)" \
    -derivedDataPath "$DD" -clonedSourcePackagesDirPath "$SRCPKG" build > "$LOGS/xcodebuild.log" 2>&1 \
    || { grep -E 'error:|BUILD FAILED' "$LOGS/xcodebuild.log" | tail -30 || true; die "xcodebuild failed ($LOGS/xcodebuild.log)"; }
  ! grep -q "Fetching from https://github.com/transistorsoft" "$LOGS/xcodebuild.log" \
    || die "xcodebuild fetched transistor-background-fetch from GitHub, not the mirror"

  # --- prove WHICH native SDK, plugin and driver the app carries, from the artifact, never the label ----------------
  want_pin="transistor-background-fetch $marketing $(git -C "$MIRROR" rev-parse HEAD)"
  [ -f "$RES_WS" ] || die "the build wrote no $RES_WS"
  for res in "$RES_WS" "$RES_PJ"; do
    [ -f "$res" ] || continue      # the project's: written only by a build of the project container
    pin="$(python3 - "$res" <<'PY'
import json, sys
pins = {p.get("location"): p for p in json.load(open(sys.argv[1])).get("pins", [])}
p = pins.get("https://github.com/transistorsoft/transistor-background-fetch.git")
print("%s %s %s" % (p["identity"], p["state"].get("version") or "-", p["state"].get("revision") or "-") if p else "none")
PY
)"
    [ "$pin" = "$want_pin" ] || die "$res pins transistor-background-fetch as '$pin', not the mirror of '$native' ($want_pin)"
  done
  printf '%s\n' "$spm_inputs" > "$DD/.lab-spm-inputs"
  APPB="$IOS/build/Debug-iphonesimulator/FetchCordovaLab.app"
  [ -d "$APPB" ] || die "no $APPB after the build"
  [ ! -L "$APPB" ] || die "$APPB is a symlink — cordova-ios moved its DSTROOT; the scans below would read nothing"
  slices="$(find "$DD/Build/Products/Debug-iphonesimulator" -maxdepth 1 -name TSBackgroundFetch.framework 2>/dev/null)"
  [ "$(printf '%s\n' "$slices" | grep -c .)" -eq 1 ] || die "expected one TSBackgroundFetch.framework slice in $DD, found: ${slices:-none}"
  SLICE="$slices"
  sha_got="$(shasum -a 256 "$SLICE/TSBackgroundFetch" | cut -d' ' -f1)"
  [ "$sha_got" = "$sha_want" ] || die "the slice the build linked (sha256 $sha_got) is not the one labelled '$native' ($sha_want)"
  mk_got="$($PB -c 'Print CFBundleShortVersionString' "$SLICE/Info.plist")"
  [ "$mk_got" = "$marketing" ] || die "the linked TSBackgroundFetch says $mk_got, the provenance of '$native' says $marketing"
  # Exactly ONE binary in the app carries the SDK's version string, and it is the one the harness reads (exe.debug.dylib,
  # else exe).  (grep -c after a pipe: under pipefail an early-exiting grep -q SIGPIPEs strings.)
  carriers="$(find "$APPB" -type f \( -perm -u+x -o -name '*.dylib' \) | sort | while read -r f; do
      [ "$(strings - "$f" 2>/dev/null | grep -cE "PROJECT:TSBackgroundFetch-[0-9]")" -gt 0 ] && echo "$f"; done || true)"
  [ "$(printf '%s\n' "$carriers" | grep -c .)" -eq 1 ] \
    || die "expected exactly one binary carrying TSBackgroundFetch's version string, found: ${carriers:-none}"
  exe="$($PB -c 'Print CFBundleExecutable' "$APPB/Info.plist")"
  case "$carriers" in "$APPB/$exe.debug.dylib"|"$APPB/$exe") ;;
    *) die "TSBackgroundFetch's version string is in $carriers, not the app executable the harness reads" ;; esac
  [ "$(strings - "$carriers" | grep -c "PROJECT:TSBackgroundFetch-$build_no")" -gt 0 ] \
    || die "$carriers carries no PROJECT:TSBackgroundFetch-$build_no — the app does not link the '$native' snapshot"
  defines() { [ "$(nm -U "$1" 2>/dev/null | grep -c "_OBJC_CLASS_\$_$2\$")" -gt 0 ]; }
  for cls in TSBackgroundFetch CDVBackgroundFetch LabPlugin; do
    defines "$carriers" "$cls" || die "$carriers does not define $cls"
  done
  # The plugin's legacy-fetch forward, which the overlay's `super` reaches.
  [ "$(nm "$carriers" 2>/dev/null | grep -c 'CDVAppDelegate(CDVBackgroundFetch) application:performFetchWithCompletionHandler:')" -gt 0 ] \
    || die "$carriers has no -[CDVAppDelegate(CDVBackgroundFetch) application:performFetchWithCompletionHandler:]: the overlay's super would raise"
  sdk_code="$(find "$APPB" -type f \( -perm -u+x -o -name '*.dylib' \) | sort | while read -r f; do
      defines "$f" TSBackgroundFetch && echo "$f"; done || true)"
  [ "$sdk_code" = "$carriers" ] || die "TSBackgroundFetch is defined in [${sdk_code:-nothing}], its version string in [$carriers]"
  for mark in "LAB point=legacyFetch" "LAB point=pluginLoad"; do
    [ "$(strings - "$carriers" | grep -cF "$mark")" -gt 0 ] || die "$carriers lacks '$mark' (app/native-ios, app/lab-plugin)"
  done
  id_got="$($PB -c 'Print CFBundleIdentifier' "$APPB/Info.plist")"
  [ "$id_got" = "$BUNDLE" ] || die "the built app is $id_got, lab.yaml names $BUNDLE"
  grep -q 'name="BackgroundFetch"' "$APPB/config.xml" && grep -q 'name="Lab"' "$APPB/config.xml" \
    || die "the app's config.xml does not register both BackgroundFetch and Lab"
  cmp -s www/index.html "$APPB/www/index.html" || die "the app does not carry app/www/index.html as it is"
  for f in labapi.js labevents.js logo.js; do
    cmp -s "$LABS/platforms/_shared/$f" "$APPB/www/$f" || die "the app does not carry labs/platforms/_shared/$f as it is"
  done
  python3 - "$STAGE/www/index.js" "$APPB/www/plugins/cordova-plugin-background-fetch/www/index.js" \
           "$APP/lab-plugin/www/lab.js" "$APPB/www/plugins/lab-plugin/www/lab.js" <<'PY'
import sys
for src, packed in zip(sys.argv[1::2], sys.argv[2::2]):
    try:
        body, wrapped = open(src, "rb").read().strip(), open(packed, "rb").read()
    except OSError as e:
        sys.exit("[lab] %s" % e)
    if body not in wrapped:
        sys.exit("[lab] the app's %s does not carry %s" % (packed.split(".app/")[1], src))
PY
  stage_py verify "$want_real" "$STAGE"

  # One artifact (lab.yaml's ios build.app); run.py's stamp beside it goes with a replaced app.
  rm -rf "$OUT" "$OUT.lab-stamp" && mkdir -p "$(dirname "$OUT")" && ditto "$APPB" "$OUT"
  echo "[lab] built ios channel=spm native=$native ($marketing/$build_no, commit $(prov commit | cut -c1-12))" \
       "plugin=$want_real cordova-ios=$cio cordova-cli=$cli app=$OUT"
  exit 0
fi

ca="$(node -p "require('cordova-android/package.json').version" 2>/dev/null || true)"
[ "$ca" = "$CORDOVA_ANDROID" ] || die "app/node_modules has cordova-android '${ca:-none}', not $CORDOVA_ANDROID (app/package.json, the lock)"
# platforms/android is generated from these, and run.py's stamp hashes none of what is inside it: an input that changed
# regenerates it, rather than a new cordova-android or config.xml building against an old project.
host="cordova-android=$ca cordova-cli=$cli config=$(shasum -a 1 config.xml | cut -d' ' -f1)"
if [ "$(cat platforms/android/.lab-host 2>/dev/null)" != "$host" ]; then
  echo "[lab] cordova platform add android ($host)"
  cordova platform rm android --nosave > /dev/null 2>&1 || true
  rm -rf platforms/android
  # `platform rm` npm-uninstalls cordova-android, whatever installed it (cordova-lib platform/remove.js), so the lock's
  # install goes back before the add: cordova-fetch then takes the installed package that satisfies the exact version,
  # instead of installing one from the registry (MEASURED in npm's logs, the 2026-11-15 review).  Not a path spec:
  # addHelper hands a missing directory to cordova-fetch, which is how the first pilot failed.
  npm ci --prefer-offline --no-audit --no-fund >> "$STAGE_ROOT/npm-ci.log" 2>&1 \
    || { tail -20 "$STAGE_ROOT/npm-ci.log"; die "npm ci failed ($STAGE_ROOT/npm-ci.log)"; }
  tree="$(shasum node_modules/.package-lock.json | cut -d' ' -f1)"
  cordova platform add "android@$CORDOVA_ANDROID" --nosave > "$STAGE_ROOT/platform-add.log" 2>&1 \
    || { tail -20 "$STAGE_ROOT/platform-add.log"; die "cordova platform add failed ($STAGE_ROOT/platform-add.log)"; }
  # The tree npm ci installed from the lock is the one the platform was generated from: cordova-fetch reused it.
  [ "$(shasum node_modules/.package-lock.json | cut -d' ' -f1)" = "$tree" ] \
    || die "cordova platform add changed app/node_modules (cordova-fetch installed cordova-android itself, not the lock's)"
  echo "$host" > platforms/android/.lab-host
fi
AND="$APP/platforms/android"

# --- the plugins, re-added every build ----------------------------------------------------------------------------

# --nosave throughout: a saved local plugin writes a machine-absolute file: path into package.json (hashed), and a
# plugin listed there would be re-fetched from npm by the next prepare — the published plugin, not the one under test.
for id in cordova-plugin-background-fetch lab-plugin; do
  cordova plugin rm "$id" --nosave --force > /dev/null 2>&1 || true
done
cordova plugin add "$STAGE" --nosave > "$STAGE_ROOT/plugin-add.log" 2>&1 \
  || { tail -20 "$STAGE_ROOT/plugin-add.log"; die "cordova plugin add (the stage) failed ($STAGE_ROOT/plugin-add.log)"; }
cordova plugin add ./lab-plugin --nosave >> "$STAGE_ROOT/plugin-add.log" 2>&1 \
  || { tail -20 "$STAGE_ROOT/plugin-add.log"; die "cordova plugin add ./lab-plugin failed ($STAGE_ROOT/plugin-add.log)"; }
[ "$(tracked_sums)" = "$before" ] \
  || die "cordova rewrote app/package.json, package-lock.json or config.xml (tracked, and hashed into run.py's stamp)"
# Both plugins were really installed FOR ANDROID by this build: `plugin rm`'s failures are not fatal (there may be nothing
# to remove), but a plugin `add` then finds still registered answers "already installed" and touches nothing
# (plugman/install.js) — so cordova's own install line is required for each, and every source file each plugin's
# plugin.xml installs is compared with what is in the platform.
for id in cordova-plugin-background-fetch lab-plugin; do
  grep -q "Installing \"$id\" for android" "$STAGE_ROOT/plugin-add.log" \
    || die "cordova did not install $id for android in this build ($STAGE_ROOT/plugin-add.log)"
done
JAVA="$AND/app/src/main/java"
for pair in "$STAGE/src/android/CDVBackgroundFetch.java:$JAVA/com/transistorsoft/cordova/backgroundfetch/CDVBackgroundFetch.java" \
            "$STAGE/src/android/HeadlessTask.java:$JAVA/com/transistorsoft/cordova/backgroundfetch/HeadlessTask.java" \
            "$APP/lab-plugin/src/android/LabPlugin.java:$JAVA/com/transistorsoft/fetchlab/cordova/LabPlugin.java"; do
  cmp -s "${pair%%:*}" "${pair#*:}" || die "the installed ${pair#*:} is not ${pair%%:*}"
done
[ "$(grep -c '<source-file ' "$STAGE/plugin.xml")" -eq 3 ] \
  || die "the stage's plugin.xml installs $(grep -c '<source-file ' "$STAGE/plugin.xml") source files, not the 3 (2 Java, 1 iOS) this build compares"

# --- the native pin, the Gradle wrapper ---------------------------------------------------------------------------

# app/lab-native.gradle (tracked), applied from the GENERATED root build.gradle: exclusiveContent on labs/m2 and the
# label forced over the plugin's hard-coded 4.1.+.  Relative, so the platform stays where it was generated.
ROOT_GRADLE="$AND/build.gradle"
pin="apply from: '../../lab-native.gradle'   // LAB (WO-002): the native SDK under test"
grep -qF "$pin" "$ROOT_GRADLE" || printf '\n%s\n' "$pin" >> "$ROOT_GRADLE"
# The lab's Java sources, by hand, BEFORE the bootstrap: the lab plugin's LabPlugin calls into the lab's MainActivity, so
# the template's would not compile (bg-forge's config.xml resource-files put its overlays in at platform add).  Prepare
# leaves app/src/main/java alone.
SRC="$AND/app/src/main/java"
mkdir -p "$SRC/com/transistorsoft/fetchlab/cordova" "$SRC/com/transistorsoft/cordova/backgroundfetch"
cp native/MainActivity.java native/LabApplication.java "$SRC/com/transistorsoft/fetchlab/cordova/"
# The headless task at the class name CDVBackgroundFetch hands the SDK (its HEADLESS_JOB_SERVICE_CLASS): the plugin
# installs none, so a customer adds theirs (a config.xml resource-file).
cp native/BackgroundFetchHeadlessTask.java "$SRC/com/transistorsoft/cordova/backgroundfetch/"

# cordova-android ships no gradlew: `cordova build` makes one the first time.  The only `cordova build` here: every
# build after it drives ./gradlew.
if [ ! -x "$AND/gradlew" ]; then
  echo "[lab] bootstrapping the Gradle wrapper (cordova build android)"
  cordova build android --debug -- --gradleArg=-PlabNativeVersion="$native" --gradleArg=-PlabMavenRepo="$M2" \
      --gradleArg=--no-configuration-cache > "$STAGE_ROOT/bootstrap.log" 2>&1 \
    || { tail -30 "$STAGE_ROOT/bootstrap.log"; die "the bootstrap cordova build failed ($STAGE_ROOT/bootstrap.log)"; }
  [ -x "$AND/gradlew" ] || die "cordova build left no $AND/gradlew"
fi

# --- the driver and the manifest, by hand (after the bootstrap: its prepare rewrites both) --------------------------

WWW="$AND/app/src/main/assets/www"
cp www/index.html "$WWW/index.html"
for f in labapi.js labevents.js logo.js; do cp "$LABS/platforms/_shared/$f" "$WWW/$f"; done

# The template's AndroidManifest.xml names no Application: the lab's, so every process logs point=appCreate.  One that
# names another would be a different host, so it is refused, not overwritten.  And its android:configChanges is what
# makes lab.yaml's triggers real: fontScale and fontWeightAdjustment absent (bold and fontscale recreate the Activity).
MANIFEST="$AND/app/src/main/AndroidManifest.xml"
if ! grep -q "android:name=\"$PKG.LabApplication\"" "$MANIFEST"; then
  grep -q '<application[^>]*android:name=' "$MANIFEST" && die "the generated <application> already names an Application in $MANIFEST"
  sed -i '' "s|<application |<application android:name=\"$PKG.LabApplication\" |" "$MANIFEST"
fi
grep -q "android:name=\"$PKG.LabApplication\"" "$MANIFEST" || die "no LabApplication in $MANIFEST"
cfg="$(sed -n 's/.*android:configChanges="\([^"]*\)".*/\1/p' "$MANIFEST" | head -1)"
[ -n "$cfg" ] || die "no android:configChanges in $MANIFEST"
case "$cfg" in
  *fontScale*|*fontWeightAdjustment*)
    die "the generated manifest now lists fontScale/fontWeightAdjustment (configChanges=\"$cfg\"): lab.yaml's bold and
fontscale triggers would recreate nothing, and recreate-foreground-android would read a recreation that never happened" ;;
esac

# --- build ---------------------------------------------------------------------------------------------------------

# run.py's stamp sits beside the APK and vouches for it.  This build replaces the APK before any proof below runs, so
# the stamp goes first (the Flutter host's rule).  --no-configuration-cache: labNativeResolved reads the resolved graph.
APK="$AND/app/build/outputs/apk/debug/app-debug.apk"
rm -f "$APK.lab-stamp"
LOG="$STAGE_ROOT/gradle-build.log"
(cd "$AND" && ./gradlew :app:assembleDebug --no-configuration-cache \
    -PlabNativeVersion="$native" -PlabMavenRepo="$M2") > "$LOG" 2>&1 \
  || { grep -E 'error:|FAILURE|What went wrong' -A3 "$LOG" | tail -30 || true; die "gradle assembleDebug failed ($LOG)"; }

# --- prove WHICH native SDK, plugin and driver the APK carries, from the resolved graph and the artifact ------------

GLOG="$STAGE_ROOT/gradle-resolved.log"
(cd "$AND" && ./gradlew -q :app:labNativeResolved --no-configuration-cache \
    -PlabNativeVersion="$native" -PlabMavenRepo="$M2") > "$GLOG" 2>&1 \
  || { tail -20 "$GLOG"; die "gradle :app:labNativeResolved failed ($GLOG)"; }
resolved_lines="$(grep '^\[lab\] tsbackgroundfetch resolved=' "$GLOG" || true)"
[ "$(printf '%s\n' "$resolved_lines" | grep -c . || true)" -eq 1 ] \
  || die "expected exactly one resolved tsbackgroundfetch in debugRuntimeClasspath, got: ${resolved_lines:-none}"
echo "$resolved_lines"
resolved="$(printf '%s\n' "$resolved_lines" | sed -n 's/^\[lab\] tsbackgroundfetch resolved=\([^ ]*\).*/\1/p')"
[ "$resolved" = "$native" ] || die "the app resolved tsbackgroundfetch '$resolved', not '$native': the pin did not hold"
aar_got="$(sed -n 's/^\[lab\] tsbackgroundfetch aar=.* sha256=\([0-9a-f]*\)$/\1/p' "$GLOG" | tail -1)"
[ -n "$aar_got" ] || die "labNativeResolved printed no AAR for tsbackgroundfetch — what the APK links is unverified"
[ "$aar_got" = "$aar_want" ] || die "the tsbackgroundfetch AAR gradle resolved (sha256 $aar_got) is not labs/m2's '$native' ($aar_want)"

[ -f "$APK" ] || die "no $APK after the build"
sdk="${ANDROID_HOME}"
bt="$(ls -d "$sdk"/build-tools/* 2>/dev/null | sort -V | tail -1)"
if [ -x "$bt/aapt2" ]; then
  pkg="$("$bt/aapt2" dump badging "$APK" 2>/dev/null | sed -n "s/^package: name='\([^']*\)'.*/\1/p")"
  [ "$pkg" = "$PKG" ] || die "the APK is package '$pkg', lab.yaml names $PKG"
fi
# Listed into a variable first: under pipefail, `unzip -p | grep -q` fails whenever grep's early exit SIGPIPEs unzip.
dex="$(unzip -p "$APK" 'classes*.dex' | LC_ALL=C strings -a)"
for cls in 'Lcom/transistorsoft/tsbackgroundfetch/BackgroundFetch;' \
           'Lcom/transistorsoft/cordova/backgroundfetch/CDVBackgroundFetch;' \
           'Lcom/transistorsoft/cordova/backgroundfetch/BackgroundFetchHeadlessTask;' \
           "Lcom/transistorsoft/fetchlab/cordova/LabPlugin;" \
           "Lcom/transistorsoft/fetchlab/cordova/LabApplication;" \
           "Lcom/transistorsoft/fetchlab/cordova/MainActivity;"; do
  grep -qF "$cls" <<<"$dex" || die "the APK's dex defines no $cls"
done
# The lab's MainActivity, not the template's: a literal only the overlay carries.
grep -qF 'point=finishNow activity=' <<<"$dex" || die "the APK's MainActivity is not app/native/MainActivity.java (no point=finishNow)"
# The driver and the shared scripts, byte for byte; the plugin's JS as cordova wraps it (cordova.define(…, function
# (require, exports, module) { <www/index.js> })), so the stage's file must be inside it whole.
TMPA="$(mktemp -d "$STAGE_ROOT/apk.XXXXXX")"
unzip -q -o "$APK" 'assets/www/*' -d "$TMPA"
cmp -s www/index.html "$TMPA/assets/www/index.html" || die "the APK does not carry app/www/index.html as it is"
for f in labapi.js labevents.js logo.js; do
  cmp -s "$LABS/platforms/_shared/$f" "$TMPA/assets/www/$f" || die "the APK does not carry labs/platforms/_shared/$f as it is"
done
python3 - "$STAGE/www/index.js" "$TMPA/assets/www/plugins/cordova-plugin-background-fetch/www/index.js" \
         lab-plugin/www/lab.js "$TMPA/assets/www/plugins/lab-plugin/www/lab.js" <<'PY'
import sys
pairs = list(zip(sys.argv[1::2], sys.argv[2::2]))
for src, packed in pairs:
    try:
        body, wrapped = open(src, "rb").read().strip(), open(packed, "rb").read()
    except OSError as e:
        sys.exit("[lab] %s" % e)
    if body not in wrapped:
        sys.exit("[lab] the APK's %s does not carry %s" % (packed.split("/assets/www/")[1], src))
PY
rm -rf "${TMPA:?}"
stage_py verify "$want_real" "$STAGE"

size="$(stat -f %z "$APK" 2>/dev/null || stat -c %s "$APK")"
echo "[lab] built android native=$native (commit $(prov commit | cut -c1-12) dirty=$(prov dirty)) aar=sha256:${aar_got:0:12}" \
     "plugin=$want_real cordova-android=$ca cordova-cli=$cli apk=$APK size=$size"
