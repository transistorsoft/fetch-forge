#!/usr/bin/env bash
# Capacitor fetch-lab build (WO-002).  usage: build.sh <native label> <plugin checkout|default>
#
# Ported from bg-forge's labs/platforms/capacitor/build.sh (addc50501) with the fetch React Native host's staging and
# native-pin proofs (labs/platforms/react-native/build.sh).  LAB_OS (run.py sets it from the platform view's OS) picks
# the branch: android (the default).  iOS is the next slice (WO-002: Capacitor iOS on CocoaPods and SwiftPM).
#
#   <native label>   a tsbackgroundfetch snapshot published into labs/m2 (labs/m2/.provenance/<label>.txt).  The app
#                    resolves com.transistorsoft:tsbackgroundfetch:<label> from labs/m2 ONLY (app/android/build.gradle's
#                    exclusiveContent), and this script refuses an APK whose resolved version is not the label or whose
#                    AAR is not byte-identical to labs/m2's.
#   <plugin>         `default` (lab.yaml default_checkout: <repo>/capacitor/background-fetch) or a path to another
#                    checkout of @transistorsoft/capacitor-background-fetch.
#
# `npx cap sync` is never run here: it regenerates capacitor.plugins.json and the Gradle wiring from package.json —
# which does not name the plugin under test — and would silently swap it for the published one.  Everything sync would
# have generated is written by hand below.
#
# The plugin is STAGED, never built in place: its working-tree files (git ls-files -co --exclude-standard, minus
# example/) are copied into ../.stage/capacitor-background-fetch (gitignored) and Gradle builds the stage's android/,
# so it never writes android/build/ into the plugin checkout, a submodule this lab only reads.  The stage records what
# it was copied from (.lab-source), and the build refuses a stage that does not name the checkout it was given.
set -euo pipefail
native="${1:?usage: build.sh <native label> <plugin checkout|default>}"; plugin="${2:-default}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LABS="$(cd "$HERE/../.." && pwd)"
REPO="$(cd "$LABS/.." && pwd)"
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
    || die "labs/m2 has provenance for '$native' but no tsbackgroundfetch-$native.aar/.pom under $MOD (a publish still running, or one that failed)"
  aar_want="$(shasum -a 256 "$AAR" | cut -d' ' -f1)"
else
  # (WO-002, iOS) The labelled Simulator snapshot labs/harness/publish_native_fetch_ios.sh wrote: an xcframework, the
  # checkout's podspec stamped with its marketing version (the plugin's `~> 4.2` resolves against it) and a
  # Package.swift whose binaryTarget is that xcframework by path (the React Native host's store, its build.sh).
  BUNDLE="${LAB_BUNDLE:?set LAB_BUNDLE to the bundle id lab.yaml names}"
  XCFW="${LAB_XCFRAMEWORKS:?set LAB_XCFRAMEWORKS to the labelled xcframework store}"
  CHANNEL="${LAB_CHANNEL:-pods}"
  SNAPSHOT="$XCFW/$native"
  PROV="$XCFW/.provenance/$native.txt"
  [ -d "$SNAPSHOT/TSBackgroundFetch.xcframework" ] && [ -f "$SNAPSHOT/TSBackgroundFetch.podspec" ] && [ -f "$PROV" ] \
    || die "no iOS snapshot labelled '$native' under $XCFW — publish it: labs/harness/publish_native_fetch_ios.sh $native"
  case "$CHANNEL" in
    pods|spm) ;;
    *) die "LAB_CHANNEL=$CHANNEL: pods or spm (lab.yaml's ios build.channels)" ;;
  esac
fi
prov() { sed -n "s/^$1:[[:space:]]*//p" "$PROV" | head -1; }

# --- the plugin checkout, staged -----------------------------------------------------------------------------------

if [ "$plugin" = "default" ] || [ -z "$plugin" ]; then
  want="$REPO/capacitor/background-fetch"      # lab.yaml default_checkout
else
  want="$plugin"
fi
want_real="$(cd "$want" 2>/dev/null && pwd -P)" || die "no plugin checkout at $want"
git -C "$want_real" rev-parse --show-toplevel >/dev/null 2>&1 || die "$want_real is not a git checkout"
[ -f "$want_real/package.json" ] && [ "$(node -p "require('$want_real/package.json').name")" = "@transistorsoft/capacitor-background-fetch" ] \
  || die "$want_real is not @transistorsoft/capacitor-background-fetch"

STAGE_ROOT="$HERE/.stage"
STAGE="$STAGE_ROOT/capacitor-background-fetch"
mkdir -p "$STAGE"

# stage <checkout> <stage>: copy the checkout's working-tree files in, drop what it no longer has (the stage's own
# build output excepted), and write .lab-source.  verify <checkout> <stage>: the stage still holds exactly those bytes
# and names that checkout.  Read-only on the checkout: GIT_OPTIONAL_LOCKS=0 keeps `git status` from refreshing its
# index.  (React Native's stage_py, labs/platforms/react-native/build.sh.)
stage_py() {
  GIT_OPTIONAL_LOCKS=0 python3 - "$@" <<'PY'
import hashlib, os, shutil, subprocess, sys
mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
KEEP = ("android/build/", "android/.gradle/", "android/.cxx/")   # the build's output: only the stage ever has it
SKIP = ("example/",)                                              # the demo app is not the bridge

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
[ -f "$STAGE/src/index.ts" ] && [ -f "$STAGE/android/build.gradle" ] || die "the stage has no src/index.ts or android/build.gradle"
if [ "$OS" = ios ]; then
  [ -f "$STAGE/TransistorsoftCapacitorBackgroundFetch.podspec" ] && [ -d "$STAGE/ios/Sources/BackgroundFetchPlugin" ] \
    || die "the stage has no TransistorsoftCapacitorBackgroundFetch.podspec or ios/Sources/BackgroundFetchPlugin"
fi

# --- JavaScript dependencies --------------------------------------------------------------------------------------

cd "$HERE/app"
# Always install: node_modules is what capacitor.settings.gradle compiles (@capacitor/android), what the iOS Podfile
# pins (@capacitor/ios) and what bg.js inlines.  `npm ci` removes node_modules first and never writes the lock
# (bg-forge's rule).
npm ci --no-audit --no-fund
core_v="$(node -p "require('$PWD/node_modules/@capacitor/core/package.json').version")"
if [ "$OS" = android ]; then
  android_v="$(node -p "require('$PWD/node_modules/@capacitor/android/package.json').version")"
  [ "$core_v" = "$android_v" ] || die "the lab's @capacitor/core $core_v and @capacitor/android $android_v differ — fix app/package.json"
  ASSETS=android/app/src/main/assets
  PUBLIC="$ASSETS/public"
  mkdir -p "$PUBLIC"
  # www/ (run.py copies it before a harness build too; copied here so a direct build.sh run is complete).
  cp -R www/. "$PUBLIC/"

  # The plugin roster BridgeActivity registers at boot (what `cap sync` would write).  LabPlugin is registered in
  # MainActivity.
  printf '[\n  {"pkg": "@transistorsoft/capacitor-background-fetch", "classpath": "com.transistorsoft.bgfetch.capacitor.BackgroundFetchPlugin"}\n]\n' \
    > "$ASSETS/capacitor.plugins.json"
else
  # The JS core inlined into bg.js must be the version whose native bridge the WebView runs: the Podfile's Capacitor
  # pod is this app's @capacitor/ios.
  ios_v="$(node -p "require('$PWD/node_modules/@capacitor/ios/package.json').version")"
  [ "$core_v" = "$ios_v" ] || die "the lab's @capacitor/core $core_v and @capacitor/ios $ios_v differ — fix app/package.json"

  # (WO-002, iOS) The ios/ tree is GENERATED and gitignored: @capacitor/cli's own CocoaPods template — what
  # `cap add ios --packagemanager CocoaPods` unpacks — plus, by hand, everything `cap sync ios` would write, because
  # sync would name the PUBLISHED plugin (bg-forge's host, its build.sh).  The tracked piece is app/native-ios/.
  # Regenerated whenever the lab's own locked template changes (a cli bump); nothing in it is edited but below.
  # One host per channel, both generated and gitignored: ios/ from the CocoaPods template (its Podfile requires
  # ../../node_modules, so it stays at that depth), build/ios-spm/ from the Swift Package Manager one (what a plain `cap
  # add ios` unpacks, Capacitor 8's default).  Both sit under a directory run.py's source stamp skips (`ios`, `build`):
  # a host one channel's build rewrites must not make the other OS's or channel's installed app stale.
  case "$CHANNEL" in
    pods) H=ios; TPL=node_modules/@capacitor/cli/assets/ios-pods-template.tar.gz ;;
    spm)  H=build/ios-spm; TPL=node_modules/@capacitor/cli/assets/ios-spm-template.tar.gz ;;
  esac
  [ -f "$TPL" ] || die "no $TPL — the lab's package.json must keep @capacitor/cli"
  host="template=$(shasum -a 1 "$TPL" | cut -d' ' -f1) cli=$(node -p "require('@capacitor/cli/package.json').version")"
  if [ "$(cat "$H/.lab-host" 2>/dev/null)" != "$host" ]; then
    echo "[lab] generating $H/ from $TPL ($host)"
    rm -rf "${H:?}" && mkdir -p "$H" && tar -xzf "$TPL" -C "$H"
    echo "$host" > "$H/.lab-host"
  fi
  APPDIR="$H/App/App"
  PUBLIC="$APPDIR/public"
  # What `cap copy ios` writes: the web assets into App/public (bg.js and the shared JavaScript follow below).
  rm -rf "$PUBLIC" && mkdir -p "$PUBLIC" && cp -R www/. "$PUBLIC/"
fi

# The PUBLISHED surface — the stage's src/index.ts, the class a customer imports — bundled as the global BGFetch, never
# Capacitor.Plugins.BackgroundFetch (the native @PluginMethod roster).  @capacitor/core resolves from THIS app's
# node_modules through NODE_PATH: the stage has no node_modules, and the checkout's own lock holds @capacitor/core
# 8.3.0, while the core inlined into bg.js must be the version whose native bridge this app runs.  The metafile proves
# where every input came from.
#
# (WO-002, 2026-11-12, MEASURED) @transistorsoft/background-fetch-types is aliased to an empty module, as the plugin's
# own build does in effect.  Its src/definitions.ts re-exports three INTERFACES with a value-form `export {…} from`;
# the plugin's `tsc` build erases them (its dist/esm/definitions.js imports nothing from the package), but esbuild,
# which compiles one file at a time, cannot know they are types and keeps the import.  And the types package's own
# runtime JS throws when loaded (1.0.0 dist/BackgroundFetch.js: `exports.default = BackgroundFetch;`, an ambient name
# that is never defined): the first capacitor api-constants run read "ReferenceError: BackgroundFetch is not defined"
# from bg.js and no driver line at all.  The guard below keeps the alias honest: src may take nothing from the
# package but the type names tsc erases, so the stub can never hide a value the plugin uses at run time.
TYPES_PKG="@transistorsoft/background-fetch-types"
python3 - "$STAGE/src" "$TYPES_PKG" <<'PY'
import os, re, sys
src, pkg = sys.argv[1], sys.argv[2]
TYPE_NAMES = {"BackgroundFetchConfig", "TaskConfig", "HeadlessEvent", "BackgroundFetchStatus", "NetworkType"}
stmt = re.compile(r"(import|export)\s+(type\s+)?(\{[^}]*\}|\*(?:\s+as\s+\w+)?|\w+(?:\s*,\s*\{[^}]*\})?)\s+from\s+['\"]"
                  + re.escape(pkg) + r"['\"]", re.S)
bad = []
for root, _, files in os.walk(src):
    for f in files:
        if not f.endswith((".ts", ".tsx", ".js")):
            continue
        text = open(os.path.join(root, f), encoding="utf-8").read()
        found = stmt.findall(text)
        if text.count(pkg) != len(found):
            bad.append("%s: a reference to %s this guard cannot read" % (f, pkg))
        for kind, is_type, what in found:
            if is_type:
                continue
            names = set(re.findall(r"\b(\w+)\b(?:\s+as\s+\w+)?", what.strip("{} "))) - {"as"}
            if not what.startswith("{") or names - TYPE_NAMES:
                bad.append("%s: %s %s from %s" % (f, kind, what.strip(), pkg))
if bad:
    sys.exit("[lab] the plugin takes more than erased types from %s, so the lab cannot stand in an empty module "
             "for it: %s" % (pkg, "; ".join(bad)))
PY
STUB="$STAGE_ROOT/types-elided.cjs"
printf '%s\n' "// Written by build.sh: $TYPES_PKG with its type-only exports erased, as the plugin's tsc build does." \
  "module.exports = {};" > "$STUB"
META="$STAGE_ROOT/bg.meta.json"
NODE_PATH="$PWD/node_modules" ./node_modules/.bin/esbuild "$STAGE/src/index.ts" --bundle --format=iife \
  --global-name=BGFetch --target=es2018 --log-level=warning --alias:"$TYPES_PKG"="$STUB" \
  --metafile="$META" --outfile="$PUBLIC/bg.js"
grep -q "registerPlugin" "$PUBLIC/bg.js" || die "bg.js does not look like the plugin bundle"
python3 - "$META" "$PWD/node_modules" "$STAGE" "$STUB" <<'PY'
import json, os, sys
meta, nm, stage, stub = (json.load(open(sys.argv[1])), os.path.realpath(sys.argv[2]), os.path.realpath(sys.argv[3]),
                         os.path.realpath(sys.argv[4]))
cwd = os.getcwd()
bad, cores = [], 0
for path in meta["inputs"]:
    real = os.path.realpath(os.path.join(cwd, path))
    if "background-fetch-types" in path:
        bad.append(real + " (the types package itself)")
    elif "@capacitor/core" in path:
        cores += 1
        if not real.startswith(nm + os.sep):
            bad.append(real)
    elif not (real == stub or real.startswith(stage + os.sep) or real.startswith(nm + os.sep)):
        bad.append(real)
if bad:
    sys.exit("[lab] bg.js inlines inputs it must not: " + ", ".join(bad[:5]))
if not cores:
    sys.exit("[lab] bg.js inlines no @capacitor/core — the metafile names %d inputs" % len(meta["inputs"]))
print("[lab] bg.js: %d inputs, @capacitor/core from this app's node_modules, the types package erased"
      % len(meta["inputs"]))
PY

# The api-hook reporter, events arm and logo every JavaScript driver shares (labs/platforms/_shared, vendored
# byte-identical from bg-forge; run.py copies www/ flat, so these come from outside it).
for f in labapi.js labevents.js logo.js; do
  [ -f "$LABS/platforms/_shared/$f" ] || die "labs/platforms/_shared/$f is missing (the vendored LabKit JavaScript)"
  cp "$LABS/platforms/_shared/$f" "$PUBLIC/$f"
done

# --- iOS (WO-002) --------------------------------------------------------------------------------------------------

if [ "$OS" = ios ]; then
  PB=/usr/libexec/PlistBuddy
  marketing="$(prov marketing)"; build_no="$(prov build)"; sha_want="$(prov sha256_sim)"
  [ -n "$marketing" ] && [ -n "$build_no" ] && [ -n "$sha_want" ] || die "$PROV lacks marketing/build/sha256_sim"

  # The overlay (AppDelegate + the Lab and Console plugins), the bundle id, and the SDK's version string.  No SDK code
  # references TSBackgroundFetchVersionString, the slice is a static archive, and the linker takes an archive member
  # only when something references it: without this the app carries no PROJECT:TSBackgroundFetch-<build> string and
  # the harness's native_resolved refuses it (the React Native host references it from LabModule.m; a Swift file
  # cannot name a C array, so the App target's link does).  Written into the two App build configurations, the ones
  # that set PRODUCT_BUNDLE_IDENTIFIER; $(inherited) keeps the xcconfig flags (CocoaPods', the SwiftPM template's).
  cp native-ios/AppDelegate.swift "$APPDIR/AppDelegate.swift"
  python3 - "$H/App/App.xcodeproj/project.pbxproj" "$BUNDLE" <<'PY'
import re, sys
path, bundle = sys.argv[1], sys.argv[2]
s = open(path).read()
s, n = re.subn(r"PRODUCT_BUNDLE_IDENTIFIER = [^;]*;", "PRODUCT_BUNDLE_IDENTIFIER = %s;" % bundle, s)
if n != 2:
    sys.exit("[lab] the template's project sets PRODUCT_BUNDLE_IDENTIFIER %d times, not the App target's 2" % n)
flag = '\t\t\t\tOTHER_LDFLAGS = (\n\t\t\t\t\t"$(inherited)",\n\t\t\t\t\t"-Wl,-u,_TSBackgroundFetchVersionString",\n\t\t\t\t);\n'
s = re.sub(r"\t\t\t\tOTHER_LDFLAGS = \(\n.*?\);\n", "", s, flags=re.S)       # this build's, never a stale one
s, n = re.subn(r"(\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = [^;]*;\n)", lambda m: m.group(1) + flag, s)
if n != 2:
    sys.exit("[lab] could not add OTHER_LDFLAGS to the App target's 2 configurations (%d)" % n)
open(path, "w").write(s)
PY

  # What `cap sync ios` writes beside the web assets: the config, whose packageClassList is the plugin roster the iOS
  # bridge registers (the twin of Android's hand-written capacitor.plugins.json), and an empty Cordova config.xml.
  # `BackgroundFetchPlugin` is the plugin's @objc name; LabPlugin and LabConsolePlugin are the overlay's.
  # LabConsolePlugin takes over the `Console` name, so it only works while the bridge still routes console there.
  grep -q "cap.toNative('Console', 'log'" node_modules/@capacitor/ios/Capacitor/Capacitor/assets/native-bridge.js \
    || die "@capacitor/ios no longer sends console lines to its Console plugin — LabConsolePlugin would log nothing"
  python3 - capacitor.config.json "$APPDIR/capacitor.config.json" <<'PY'
import json, sys
config = json.load(open(sys.argv[1]))
config["packageClassList"] = ["BackgroundFetchPlugin", "LabPlugin", "LabConsolePlugin"]
json.dump(config, open(sys.argv[2], "w"), indent=2)
PY
  printf '%s\n' "<?xml version='1.0' encoding='utf-8'?>" \
    '<widget version="1.0.0" xmlns="http://www.w3.org/ns/widgets" xmlns:cdv="http://cordova.apache.org/ns/1.0">' \
    '  <access origin="*" />' '</widget>' > "$APPDIR/config.xml"

  # The keys the SDK's iOS setup needs, which the stock template carries none of (the React Native host's Info.plist):
  # UIBackgroundModes fetch and processing, and BGTaskSchedulerPermittedIdentifiers — the SDK's fetch task, and the
  # lab's custom task (ios-task-sim, api-smoke-ios).  Written onto a PRISTINE copy of the template's plist each build,
  # so an edit here always reaches an existing host.
  PLIST="$APPDIR/Info.plist"
  tar -xzf "$TPL" -C "$H" App/App/Info.plist
  for key in UIBackgroundModes BGTaskSchedulerPermittedIdentifiers; do
    $PB -c "Delete :$key" "$PLIST" >/dev/null 2>&1 || true
  done
  $PB -c "Add :UIBackgroundModes array" -c "Add :UIBackgroundModes:0 string fetch" \
      -c "Add :UIBackgroundModes:1 string processing" "$PLIST"
  $PB -c "Add :BGTaskSchedulerPermittedIdentifiers array" \
      -c "Add :BGTaskSchedulerPermittedIdentifiers:0 string com.transistorsoft.fetch" \
      -c "Add :BGTaskSchedulerPermittedIdentifiers:1 string com.transistorsoft.fetchlab.task" "$PLIST"
  $PB -c "Set :CFBundleDisplayName 'Fetch Cap Lab'" "$PLIST" 2>/dev/null \
    || $PB -c "Add :CFBundleDisplayName string 'Fetch Cap Lab'" "$PLIST"

  DERIVED="$H/build"
  PRODUCTS="$DERIVED/Build/Products/Debug-iphonesimulator"
  mkdir -p "$DERIVED"
  case "$CHANNEL" in
  pods)
  # The Podfile's capacitor_pods — what sync would generate, but naming the stage of the checkout under test and the
  # labelled snapshot BY PATH (pod install is the pin, Podfile.lock the record).  Rewritten every build.
  python3 - "$H/App/Podfile" <<'PY'
import re, sys
path = sys.argv[1]
s = open(path).read()
pods = ("def capacitor_pods\n"
        "  pod 'Capacitor', :path => '../../node_modules/@capacitor/ios'\n"
        "  pod 'CapacitorCordova', :path => '../../node_modules/@capacitor/ios'\n"
        "  pod 'TransistorsoftCapacitorBackgroundFetch', :path => ENV.fetch('LAB_PLUGIN')   # (WO-002) the stage\n"
        "  pod 'TSBackgroundFetch', :path => ENV.fetch('LAB_TSBF')   # (WO-002) the labelled snapshot\n"
        "end\n")
s, n = re.subn(r"def capacitor_pods\n.*?\nend\n", lambda m: pods, s, count=1, flags=re.S)
if n != 1:
    sys.exit("[lab] the Podfile has no `def capacitor_pods … end` to fill")
open(path, "w").write(s)
PY

  export LAB_TSBF="$SNAPSHOT" LAB_PLUGIN="$STAGE"
  # The snapshot root is on -F before the slice CocoaPods copies (and this script proves): a TSBackgroundFetch.framework
  # there would be the one linked.  The store holds only the xcframework.
  [ ! -e "$SNAPSHOT/TSBackgroundFetch.framework" ] || die "$SNAPSHOT holds a TSBackgroundFetch.framework, which the link would take over the proven slice"
  # Install whenever the lock does not name this snapshot AND the stage, no longer matches the sandbox, was made from
  # another Podfile, or from other :path podspecs — the plugin's (its file list, podspec, package.json) or the
  # snapshot's, which a re-publish under the same label rewrites — or another @capacitor/ios (development pods are read
  # at install time only; a label switch changes the environment, never the Podfile text).  bg-forge's rule.
  LOCK="$H/App/Podfile.lock"
  inputs="capacitor-ios=$ios_v $( (cd "$STAGE" && find ios/Sources/BackgroundFetchPlugin | sort; cat TransistorsoftCapacitorBackgroundFetch.podspec package.json "$SNAPSHOT/TSBackgroundFetch.podspec") | shasum -a 1 | cut -d' ' -f1)"
  if ! grep -q "TSBackgroundFetch (from \`$SNAPSHOT\`)" "$LOCK" 2>/dev/null \
     || ! grep -q "TransistorsoftCapacitorBackgroundFetch (from \`$STAGE\`)" "$LOCK" \
     || ! cmp -s "$LOCK" "$H/App/Pods/Manifest.lock" \
     || ! grep -q "^PODFILE CHECKSUM: $(shasum -a 1 "$H/App/Podfile" | cut -d' ' -f1)$" "$LOCK" \
     || [ "$(cat "$H/App/Pods/.lab-inputs" 2>/dev/null)" != "$inputs" ]; then
    echo "[lab] pod install (snapshot, plugin, Podfile or development pods changed)"
    rm -rf "$PRODUCTS/XCFrameworkIntermediates/TSBackgroundFetch"
    (cd "$H/App" && LANG=en_US.UTF-8 pod install > ../build/pod-install.log 2>&1) \
      || { tail -30 "$DERIVED/pod-install.log" || true; die "pod install failed ($DERIVED/pod-install.log)"; }
    echo "$inputs" > "$H/App/Pods/.lab-inputs"
  fi

  echo "[lab] xcodebuild (CocoaPods) — $DERIVED/xcodebuild.log"
  LANG=en_US.UTF-8 xcodebuild -workspace "$H/App/App.xcworkspace" -scheme App -configuration Debug \
    -sdk iphonesimulator -arch "$(uname -m)" -derivedDataPath "$DERIVED" build > "$DERIVED/xcodebuild.log" 2>&1 \
    || { grep -E 'error:|BUILD FAILED' "$DERIVED/xcodebuild.log" | tail -30 || true; die "xcodebuild failed ($DERIVED/xcodebuild.log)"; }
  grep -q "TSBackgroundFetch (from \`$SNAPSHOT\`)" "$LOCK" \
    || die "Podfile.lock does not record the snapshot as TSBackgroundFetch's source"
  grep -q "TransistorsoftCapacitorBackgroundFetch (from \`$STAGE\`)" "$LOCK" \
    || die "Podfile.lock does not record the stage as the plugin's source"
  SLICE="$PRODUCTS/XCFrameworkIntermediates/TSBackgroundFetch/TSBackgroundFetch.framework"
  ;;

  spm)
  # CapApp-SPM/Package.swift as `cap sync` writes it (@capacitor/cli dist/util/spm.js generatePackageText):
  # capacitor-swift-pm at exactly this app's @capacitor/ios, so the native bridge matches the @capacitor/core bg.js
  # inlines, and the plugin as a path package — the stage — under its iOS name.  Rewritten every build.
  [ -f "$STAGE/Package.swift" ] || die "the stage has no Package.swift: the plugin cannot be built with Swift Package Manager"
  # Relative to the package that names it (SwiftPM resolves a path from the manifest's own directory).
  rel="$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$STAGE" "$H/App/CapApp-SPM")"
  cat > "$H/App/CapApp-SPM/Package.swift" <<SWIFT
// swift-tools-version: 5.9
import PackageDescription

// Written by labs/platforms/capacitor/build.sh (WO-002) as \`cap sync\` would, naming the stage of the plugin under test.
let package = Package(
    name: "CapApp-SPM",
    platforms: [.iOS(.v15)],
    products: [
        .library(
            name: "CapApp-SPM",
            targets: ["CapApp-SPM"])
    ],
    dependencies: [
        .package(url: "https://github.com/ionic-team/capacitor-swift-pm.git", exact: "$ios_v"),
        .package(name: "TransistorsoftCapacitorBackgroundFetch", path: "$rel")
    ],
    targets: [
        .target(
            name: "CapApp-SPM",
            dependencies: [
                .product(name: "Capacitor", package: "capacitor-swift-pm"),
                .product(name: "Cordova", package: "capacitor-swift-pm"),
                .product(name: "TransistorsoftCapacitorBackgroundFetch", package: "TransistorsoftCapacitorBackgroundFetch")
            ]
        )
    ]
)
SWIFT

  # The label as the package the plugin asks for (the React Native host's mirror, its build.sh).  The plugin's
  # Package.swift wants .package(url: "https://github.com/transistorsoft/transistor-background-fetch.git", from:
  # "4.2.0"): a SwiftPM mirror maps that URL to a local git repo of the label, tagged with its iOS version, so the
  # plugin's manifest is never edited and its own requirement is what resolves.  The repo is named as the URL's package
  # so the resolved identity is the one the plugin's `package:` names.  One mirror per label, its commit made with fixed
  # dates: SwiftPM records each version's revision per location and refuses it at another revision later.
  SRCPKG="$DERIVED/SourcePackages"
  RESOLVED="$H/App/App.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
  MIRROR="$DERIVED/lab-mirror/$native/transistor-background-fetch"
  mirror_source="label=$native commit=$(prov commit) sha256_sim=$sha_want marketing=$marketing"
  if [ "$(cat "$MIRROR.lab-source" 2>/dev/null)" != "$mirror_source" ] \
     || ! git -C "$MIRROR" rev-parse -q --verify "refs/tags/$marketing" >/dev/null 2>&1; then
    echo "[lab] SwiftPM mirror of $native (tag $marketing)"
    rm -rf "${MIRROR:?}" "$MIRROR.lab-source" "${SRCPKG:?}" "$RESOLVED"
    mkdir -p "$MIRROR"
    (cd "$SNAPSHOT" && tar --exclude .swiftpm --exclude .build -cf - .) | tar -x -C "$MIRROR"
    git -C "$MIRROR" init -q
    git -C "$MIRROR" add -A
    GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000" GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000" \
      git -C "$MIRROR" -c user.email=lab@fetch-forge.invalid -c user.name=labkit -c commit.gpgsign=false commit -qm "$mirror_source"
    git -C "$MIRROR" -c tag.gpgsign=false tag "$marketing"
    echo "$mirror_source" > "$MIRROR.lab-source"
  fi
  CFG="$H/App/App.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/configuration"
  mkdir -p "$CFG"
  mirrors="$(printf '{"object":[{"mirror":"file://%s","original":"https://github.com/transistorsoft/transistor-background-fetch.git"}],"version":1}' \
    "$(cd "$MIRROR" && pwd -P)")"
  # Another label's mirror in use before: SwiftPM's checkouts and pins name it, so they go too.
  if [ "$(cat "$CFG/mirrors.json" 2>/dev/null)" != "$mirrors" ]; then
    rm -rf "${SRCPKG:?}" "$RESOLVED"
  fi
  printf '%s\n' "$mirrors" > "$CFG/mirrors.json"

  echo "[lab] xcodebuild (Swift Package Manager) — $DERIVED/xcodebuild.log"
  LANG=en_US.UTF-8 xcodebuild -project "$H/App/App.xcodeproj" -scheme App -configuration Debug \
    -sdk iphonesimulator -arch "$(uname -m)" -derivedDataPath "$DERIVED" -clonedSourcePackagesDirPath "$SRCPKG" \
    build > "$DERIVED/xcodebuild.log" 2>&1 \
    || { grep -E 'error:|BUILD FAILED' "$DERIVED/xcodebuild.log" | tail -30 || true; die "xcodebuild failed ($DERIVED/xcodebuild.log)"; }
  # Which packages SwiftPM resolved: the SDK at the mirror's tag and commit, Capacitor at this app's @capacitor/ios.
  pins="$(python3 - "$RESOLVED" <<'PY'
import json, sys
pins = {p.get("location"): p for p in json.load(open(sys.argv[1])).get("pins", [])}
def pin(url):
    p = pins.get(url)
    return "%s %s %s" % (p["identity"], p["state"].get("version") or "-", p["state"].get("revision") or "-") if p else "none"
print(pin("https://github.com/transistorsoft/transistor-background-fetch.git"))
print(pin("https://github.com/ionic-team/capacitor-swift-pm.git"))
PY
)"
  [ "$(sed -n 1p <<<"$pins")" = "transistor-background-fetch $marketing $(git -C "$MIRROR" rev-parse HEAD)" ] \
    || die "Package.resolved pins transistor-background-fetch as '$(sed -n 1p <<<"$pins")', not the mirror of '$native' at $marketing"
  [ "$(sed -n 2p <<<"$pins" | cut -d' ' -f1-2)" = "capacitor-swift-pm $ios_v" ] \
    || die "Package.resolved pins capacitor-swift-pm as '$(sed -n 2p <<<"$pins")', not $ios_v"
  SLICE="$PRODUCTS/TSBackgroundFetch.framework"
  ;;
  esac

  # --- prove WHICH native SDK and plugin the app links, from the artifact, never the label ----------------------
  APP="$PRODUCTS/App.app"
  [ -d "$APP" ] || die "no $APP after the build"
  [ -f "$SLICE/TSBackgroundFetch" ] || die "the $CHANNEL build left no TSBackgroundFetch slice at $SLICE"
  sha_got="$(shasum -a 256 "$SLICE/TSBackgroundFetch" | cut -d' ' -f1)"
  [ "$sha_got" = "$sha_want" ] || die "the slice the $CHANNEL build linked (sha256 $sha_got) is not the one labelled '$native' ($sha_want)"
  mk_got="$($PB -c 'Print CFBundleShortVersionString' "$SLICE/Info.plist")"
  [ "$mk_got" = "$marketing" ] || die "the linked TSBackgroundFetch says $mk_got, the provenance of '$native' says $marketing"
  # Exactly ONE binary in the app carries the SDK's version string, and it is the one the harness reads
  # (fetch_pack Simctl.native_resolved: <exe>.debug.dylib, else <exe>).  (grep -c after a pipe: under pipefail an
  # early-exiting grep -q SIGPIPEs strings.)
  carriers="$(find "$APP" -type f \( -perm -u+x -o -name '*.dylib' \) | sort | while read -r f; do
      [ "$(strings - "$f" 2>/dev/null | grep -cE "PROJECT:TSBackgroundFetch-[0-9]")" -gt 0 ] && echo "$f"; done || true)"
  [ "$(printf '%s\n' "$carriers" | grep -c .)" -eq 1 ] \
    || die "expected exactly one binary carrying TSBackgroundFetch's version string, found: ${carriers:-none}"
  exe="$($PB -c 'Print CFBundleExecutable' "$APP/Info.plist")"
  case "$carriers" in "$APP/$exe.debug.dylib"|"$APP/$exe") ;;
    *) die "TSBackgroundFetch's version string is in $carriers, not the app executable the harness reads" ;; esac
  [ "$(strings - "$carriers" | grep -c "PROJECT:TSBackgroundFetch-$build_no")" -gt 0 ] \
    || die "$carriers carries no PROJECT:TSBackgroundFetch-$build_no — the app does not link the '$native' snapshot"
  # A class packageClassList names but the binary lacks is skipped in silence at launch.
  defines() { [ "$(nm -U "$1" 2>/dev/null | grep -c "_OBJC_CLASS_\$_$2\$")" -gt 0 ]; }
  for cls in BackgroundFetchPlugin LabPlugin LabConsolePlugin TSBackgroundFetch; do
    defines "$carriers" "$cls" || die "$carriers does not define $cls"
  done
  # And the SDK's code is in that binary alone (bg-forge's TSConfig check): a second, partial copy elsewhere — without
  # the version member — would give the probe and the plugin two singletons.  (The SwiftPM build also embeds an
  # empty TSBackgroundFetch.framework stub, MEASURED: it defines nothing, and nothing loads it.)
  sdk_code="$(find "$APP" -type f \( -perm -u+x -o -name '*.dylib' \) | sort | while read -r f; do
      defines "$f" TSBackgroundFetch && echo "$f"; done || true)"
  [ "$sdk_code" = "$carriers" ] || die "TSBackgroundFetch is defined in [${sdk_code:-nothing}], its version string in [$carriers]"
  [ "$(strings - "$carriers" | grep -c "LAB point=legacyFetch")" -gt 0 ] \
    || die "$carriers lacks the legacy-fetch forward (AppDelegate.swift): a pushed fetch would reach nothing"
  id_got="$($PB -c 'Print CFBundleIdentifier' "$APP/Info.plist")"
  [ "$id_got" = "$BUNDLE" ] || die "the built app is $id_got, lab.yaml names $BUNDLE"
  for key in "UIBackgroundModes" "BGTaskSchedulerPermittedIdentifiers"; do
    $PB -c "Print :$key" "$APP/Info.plist" >/dev/null 2>&1 || die "the built app's Info.plist has no $key"
  done
  cmp -s "$PUBLIC/bg.js" "$APP/public/bg.js" || die "the app does not carry this build's bg.js"
  cmp -s www/index.html "$APP/public/index.html" || die "the app does not carry www/index.html as it is"
  stage_py verify "$want_real" "$STAGE"

  # One artifact whichever channel built it (the React Native host's rule, bg-forge WO-192 D2): lab.yaml's ios
  # build.app.
  OUT="$HERE/app/build/lab-ios/App.app"
  # Both channels, and every label, write this one path: run.py's stamp beside it vouched for the app it built, so it
  # goes with that app (run.py writes a new one after this script exits; a direct build.sh run leaves none).
  rm -rf "$OUT" "$OUT.lab-stamp" && mkdir -p "$(dirname "$OUT")" && ditto "$APP" "$OUT"
  echo "[lab] built ios channel=$CHANNEL native=$native ($marketing/$build_no, commit $(prov commit | cut -c1-12))" \
       "plugin=$want_real capacitor=$ios_v app=$OUT"
  exit 0
fi

# --- Android -------------------------------------------------------------------------------------------------------

sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
[ -d "$sdk" ] || die "no Android SDK at $sdk — set ANDROID_HOME"
# local.properties is gitignored and nothing else writes it: without it gradle stops at "SDK location not found".
[ -f android/local.properties ] || echo "sdk.dir=$sdk" > android/local.properties

# --no-configuration-cache: labNativeResolved reads the resolved graph at execution time, which a cached configuration
# would not re-resolve (and the task declares itself incompatible with it).
LOG="$STAGE_ROOT/gradle.log"
set +e
(cd android && ./gradlew :app:assembleDebug :app:labNativeResolved --no-configuration-cache \
    -PlabNativeVersion="$native" -PlabMavenRepo="$M2" -PlabPluginDir="$STAGE/android") 2>&1 | tee "$LOG"
status="${PIPESTATUS[0]}"
set -e
[ "$status" -eq 0 ] || die "gradle failed ($status) — $LOG"

# --- prove WHICH native SDK and which plugin the APK carries, from the resolved graph and the artifact -------------

resolved_lines="$(grep '^\[lab\] tsbackgroundfetch resolved=' "$LOG" || true)"
[ "$(printf '%s\n' "$resolved_lines" | grep -c . || true)" -eq 1 ] \
  || die "expected exactly one resolved tsbackgroundfetch in debugRuntimeClasspath, got: ${resolved_lines:-none}"
echo "$resolved_lines"
resolved="$(printf '%s\n' "$resolved_lines" | sed -n 's/^\[lab\] tsbackgroundfetch resolved=\([^ ]*\).*/\1/p')"
[ "$resolved" = "$native" ] || die "the app resolved tsbackgroundfetch '$resolved', not '$native'.
Something overrode the pin (the plugin's own safeExtGet('backgroundFetchVersion', '4.1.+') floor, a forced version,
or a conflict) — the APK does NOT contain the SDK under test."
aar_got="$(sed -n 's/^\[lab\] tsbackgroundfetch aar=.* sha256=\([0-9a-f]*\)$/\1/p' "$LOG" | tail -1)"
[ -n "$aar_got" ] || die "labNativeResolved printed no AAR for tsbackgroundfetch — what the APK links is unverified"
[ "$aar_got" = "$aar_want" ] || die "the tsbackgroundfetch AAR gradle resolved (sha256 $aar_got) is not labs/m2's '$native' ($aar_want)"

APK="$HERE/app/android/app/build/outputs/apk/debug/app-debug.apk"
[ -f "$APK" ] || die "no $APK after the build"
bt="$(ls -d "$sdk"/build-tools/* 2>/dev/null | sort -V | tail -1)"
if [ -x "$bt/aapt2" ]; then
  pkg="$("$bt/aapt2" dump badging "$APK" 2>/dev/null | sed -n "s/^package: name='\([^']*\)'.*/\1/p")"
  [ "$pkg" = "com.transistorsoft.fetchlab.capacitor" ] || die "the APK is package '$pkg', lab.yaml names com.transistorsoft.fetchlab.capacitor"
fi
# Listed into a variable first: under pipefail, `unzip -l | grep -q` fails whenever grep's early exit SIGPIPEs unzip.
dex="$(unzip -p "$APK" 'classes*.dex' | LC_ALL=C strings -a)"
for cls in 'Lcom/transistorsoft/tsbackgroundfetch/BackgroundFetch;' \
           'Lcom/transistorsoft/bgfetch/capacitor/BackgroundFetchPlugin;' \
           'Lcom/transistorsoft/fetchlab/capacitor/BackgroundFetchHeadlessTask;'; do
  grep -qF "$cls" <<<"$dex" || die "the APK's dex defines no $cls"
done
unzip -p "$APK" assets/public/bg.js | cmp -s - "$PUBLIC/bg.js" || die "the APK does not carry this build's bg.js"
unzip -p "$APK" assets/public/index.html | cmp -s - www/index.html || die "the APK does not carry www/index.html as it is"
stage_py verify "$want_real" "$STAGE"

size="$(stat -f %z "$APK" 2>/dev/null || stat -c %s "$APK")"
echo "[lab] built android native=$native (commit $(prov commit | cut -c1-12) dirty=$(prov dirty)) aar=sha256:${aar_got:0:12}" \
     "plugin=$want_real capacitor=$android_v apk=$APK size=$size"
