#!/usr/bin/env bash
# Cordova fetch-lab build (WO-002).  usage: build.sh <native label> <plugin checkout|default>
#
# Modelled on bg-forge's labs/platforms/cordova/build.sh (addc50501), with the fetch hosts' staging and native-pin
# proofs (labs/platforms/capacitor/build.sh, labs/platforms/flutter/build.sh).  LAB_OS (run.py sets it from the
# platform view's OS) picks the branch: android (the default).  iOS is the next slice.
#
#   <native label>   a tsbackgroundfetch snapshot published into labs/m2 (labs/m2/.provenance/<label>.txt).  The app
#                    resolves com.transistorsoft:tsbackgroundfetch:<label> from labs/m2 ONLY, at that version whatever
#                    the plugin asks for (app/lab-native.gradle), and this script refuses an APK whose resolved version
#                    is not the label or whose AAR is not byte-identical to labs/m2's.
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
CORDOVA_ANDROID="15.0.0"            # app/package.json's exact devDependency, installed from the committed lock
die() { echo "[lab] $*" >&2; exit 1; }

OS="${LAB_OS:-android}"
case "$OS" in
  android) ;;
  ios) die "Cordova iOS is the next slice (WO-002: Cordova Android first)" ;;
  *) die "LAB_OS=$OS: this lab builds android" ;;
esac

# --- the native snapshot -------------------------------------------------------------------------------------------

M2="$LABS/m2"
PROV="$M2/.provenance/$native.txt"
MOD="$M2/com/transistorsoft/tsbackgroundfetch/$native"
AAR="$MOD/tsbackgroundfetch-$native.aar"
[ -f "$PROV" ] || die "no Android snapshot labelled '$native': $PROV is missing — publish it into labs/m2 first"
[ -f "$AAR" ] && [ -f "$MOD/tsbackgroundfetch-$native.pom" ] \
  || die "labs/m2 has provenance for '$native' but no tsbackgroundfetch-$native.aar/.pom under $MOD"
aar_want="$(shasum -a 256 "$AAR" | cut -d' ' -f1)"
prov() { sed -n "s/^$1:[[:space:]]*//p" "$PROV" | head -1; }

# --- the toolchain -------------------------------------------------------------------------------------------------

command -v cordova >/dev/null || die "the cordova CLI is not on PATH (npm i -g cordova)"
cli="$(cordova --version 2>/dev/null | head -1 | tr -d '[:space:]')"
[ "${cli%%.*}" -ge 13 ] 2>/dev/null || die "cordova CLI '${cli:-unknown}' is too old for cordova-android $CORDOVA_ANDROID (need >= 13)"
if [ -z "${ANDROID_HOME:-}" ]; then
  for candidate in "${ANDROID_SDK_ROOT:-}" "$HOME/Library/Android/sdk" "$HOME/Android/Sdk"; do
    if [ -n "$candidate" ] && [ -d "$candidate" ]; then ANDROID_HOME="$candidate"; break; fi
  done
fi
[ -n "${ANDROID_HOME:-}" ] || die "set ANDROID_HOME: cordova platform add needs it"
export ANDROID_HOME ANDROID_SDK_ROOT="$ANDROID_HOME"

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
[ -f "$STAGE/plugin.xml" ] && [ -f "$STAGE/www/index.js" ] && [ -f "$STAGE/src/android/CDVBackgroundFetch.java" ] \
  || die "the stage has no plugin.xml, www/index.js or src/android/CDVBackgroundFetch.java"

# --- the project: cordova-android from the lock, generated when its inputs change ---------------------------------

cd "$APP"
# What cordova must never rewrite (each is tracked and hashed into run.py's stamp), by content: checked after the plugin
# adds below, committed or not.
tracked_sums() { shasum package.json package-lock.json config.xml; }
before="$(tracked_sums)"
npm ci --prefer-offline --no-audit --no-fund > "$STAGE_ROOT/npm-ci.log" 2>&1 \
  || { tail -20 "$STAGE_ROOT/npm-ci.log"; die "npm ci failed ($STAGE_ROOT/npm-ci.log)"; }
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
