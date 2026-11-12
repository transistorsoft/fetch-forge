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
  android) ;;
  ios) die "Capacitor iOS is the next slice (WO-002: Capacitor Android first, then iOS on CocoaPods and SwiftPM)" ;;
  *) die "LAB_OS=$OS: this lab builds android" ;;
esac

# --- the native snapshot -------------------------------------------------------------------------------------------

M2="$LABS/m2"
PROV="$M2/.provenance/$native.txt"
MOD="$M2/com/transistorsoft/tsbackgroundfetch/$native"
AAR="$MOD/tsbackgroundfetch-$native.aar"
[ -f "$PROV" ] || die "no Android snapshot labelled '$native': $PROV is missing — publish it into labs/m2 first"
[ -f "$AAR" ] && [ -f "$MOD/tsbackgroundfetch-$native.pom" ] \
  || die "labs/m2 has provenance for '$native' but no tsbackgroundfetch-$native.aar/.pom under $MOD (a publish still running, or one that failed)"
aar_want="$(shasum -a 256 "$AAR" | cut -d' ' -f1)"
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

# --- JavaScript dependencies --------------------------------------------------------------------------------------

cd "$HERE/app"
# Always install: node_modules is what capacitor.settings.gradle compiles (@capacitor/android) and what bg.js inlines.
# `npm ci` removes node_modules first and never writes the lock (bg-forge's rule).
npm ci --no-audit --no-fund
android_v="$(node -p "require('$PWD/node_modules/@capacitor/android/package.json').version")"
core_v="$(node -p "require('$PWD/node_modules/@capacitor/core/package.json').version")"
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
