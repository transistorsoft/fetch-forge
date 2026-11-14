#!/usr/bin/env bash
# Flutter fetch-lab build (WO-002).  usage: build.sh <native label> <plugin checkout|default>
#
# Modelled on bg-forge's labs/platforms/flutter/build.sh (addc50501) with the fetch hosts' staging and native-pin proofs
# (labs/platforms/capacitor/build.sh).  LAB_OS (run.py sets it from the platform view's OS) picks the branch: android
# (the default).  iOS is the next slice (WO-002: Flutter Android, then iOS on CocoaPods and SwiftPM).
#
#   <native label>   a tsbackgroundfetch snapshot published into labs/m2 (labs/m2/.provenance/<label>.txt).  The app
#                    resolves com.transistorsoft:tsbackgroundfetch:<label> from labs/m2 ONLY (app/android/build.gradle.kts'
#                    exclusiveContent), and this script refuses an APK whose resolved version is not the label or whose
#                    AAR is not byte-identical to labs/m2's.
#   <plugin>         `default` (lab.yaml default_checkout: <repo>/flutter/background_fetch) or a path to another
#                    checkout of the background_fetch Dart plugin.
#
# The plugin is STAGED, never built in place: its working-tree files (git ls-files -co --exclude-standard, minus
# example/) are copied into ../.stage/background_fetch (gitignored), which app/pubspec.yaml depends on by path, so no
# build writes into the plugin checkout, a submodule this lab only reads.  The stage records what it was copied from
# (.lab-source), and the build refuses a stage that does not name the checkout it was given.
set -euo pipefail
native="${1:?usage: build.sh <native label> <plugin checkout|default>}"; plugin="${2:-default}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LABS="$(cd "$HERE/../.." && pwd)"
REPO="$(cd "$LABS/.." && pwd)"
die() { echo "[lab] $*" >&2; exit 1; }

OS="${LAB_OS:-android}"
case "$OS" in
  android) ;;
  ios) die "Flutter iOS is the next slice (WO-002: Flutter Android first, then iOS on CocoaPods and SwiftPM)" ;;
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

# --- the plugin checkout, staged -----------------------------------------------------------------------------------

if [ "$plugin" = "default" ] || [ -z "$plugin" ]; then
  want="$REPO/flutter/background_fetch"      # lab.yaml default_checkout
else
  want="$plugin"
fi
want_real="$(cd "$want" 2>/dev/null && pwd -P)" || die "no plugin checkout at $want"
git -C "$want_real" rev-parse --show-toplevel >/dev/null 2>&1 || die "$want_real is not a git checkout"
grep -qE "^name:[[:space:]]*background_fetch[[:space:]]*$" "$want_real/pubspec.yaml" 2>/dev/null \
  || die "$want_real is not the background_fetch Dart plugin"

STAGE_ROOT="$HERE/.stage"
STAGE="$STAGE_ROOT/background_fetch"
mkdir -p "$STAGE"

# stage <checkout> <stage>: copy the checkout's working-tree files in, drop what it no longer has (the stage's own
# build output excepted), and write .lab-source.  verify <checkout> <stage>: the stage still holds exactly those bytes
# and names that checkout.  Read-only on the checkout: GIT_OPTIONAL_LOCKS=0 keeps `git status` from refreshing its
# index.  (The Capacitor and React Native hosts' stage_py.)
stage_py() {
  GIT_OPTIONAL_LOCKS=0 python3 - "$@" <<'PY'
import hashlib, os, shutil, subprocess, sys
mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
KEEP = (".dart_tool/", "build/", "android/build/", "android/.gradle/", "android/.cxx/")   # only the stage ever has these
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
[ -f "$STAGE/lib/background_fetch.dart" ] && [ -f "$STAGE/android/build.gradle" ] \
  || die "the stage has no lib/background_fetch.dart or android/build.gradle"

# --- the app -------------------------------------------------------------------------------------------------------

cd "$HERE/app"
[ -f lib/main.dart ] || die "app/lib/main.dart is missing (the Dart driver)"
LOG="$STAGE_ROOT/flutter-build.log"
flutter --suppress-analytics pub get > "$STAGE_ROOT/pub-get.log" 2>&1 \
  || { tail -20 "$STAGE_ROOT/pub-get.log"; die "flutter pub get failed ($STAGE_ROOT/pub-get.log)"; }
# The resolved plugin must be the stage (pub records a path package's resolved path in package_config.json).
python3 - .dart_tool/package_config.json "$STAGE" <<'PY'
import json, os, sys
cfg, stage = json.load(open(sys.argv[1])), os.path.realpath(sys.argv[2])
pkg = {p["name"]: p for p in cfg["packages"]}.get("background_fetch")
if not pkg:
    sys.exit("[lab] package_config.json resolves no background_fetch")
root = pkg["rootUri"]
root = root[len("file://"):] if root.startswith("file://") else os.path.join(os.path.dirname(sys.argv[1]), root)
if os.path.realpath(root) != stage:
    sys.exit("[lab] background_fetch resolves to %s, not the stage %s" % (os.path.realpath(root), stage))
PY

# run.py's stamp sits beside the APK and vouches for it.  This build replaces the APK before any proof below runs, so
# the stamp goes first: a direct build.sh run (another label, another checkout) or one the proofs refuse must not leave
# the previous build's stamp vouching for a different APK.  run.py writes a new one after a clean exit.  (The Capacitor
# host's iOS branch does the same.)
rm -f "$HERE/app/build/app/outputs/flutter-apk/app-debug.apk.lab-stamp"
set +e
flutter --suppress-analytics build apk --debug -P labNativeVersion="$native" -P labMavenRepo="$M2" 2>&1 | tee "$LOG"
status="${PIPESTATUS[0]}"
set -e
[ "$status" -eq 0 ] || die "flutter build apk failed ($status) — $LOG"

# --- prove WHICH native SDK, plugin and driver the APK carries, from the resolved graph and the artifact ------------

GLOG="$STAGE_ROOT/gradle-resolved.log"
(cd android && ./gradlew -q :app:labNativeResolved --no-configuration-cache \
    -PlabNativeVersion="$native" -PlabMavenRepo="$M2") > "$GLOG" 2>&1 \
  || { tail -20 "$GLOG"; die "gradle :app:labNativeResolved failed ($GLOG)"; }
resolved_lines="$(grep '^\[lab\] tsbackgroundfetch resolved=' "$GLOG" || true)"
[ "$(printf '%s\n' "$resolved_lines" | grep -c . || true)" -eq 1 ] \
  || die "expected exactly one resolved tsbackgroundfetch in debugRuntimeClasspath, got: ${resolved_lines:-none}"
echo "$resolved_lines"
resolved="$(printf '%s\n' "$resolved_lines" | sed -n 's/^\[lab\] tsbackgroundfetch resolved=\([^ ]*\).*/\1/p')"
[ "$resolved" = "$native" ] || die "the app resolved tsbackgroundfetch '$resolved', not '$native': something overrode the pin
(the plugin's own safeExtGet('backgroundFetchVersion', '4.1.+') floor, a forced version, or a conflict)"
aar_got="$(sed -n 's/^\[lab\] tsbackgroundfetch aar=.* sha256=\([0-9a-f]*\)$/\1/p' "$GLOG" | tail -1)"
[ -n "$aar_got" ] || die "labNativeResolved printed no AAR for tsbackgroundfetch — what the APK links is unverified"
[ "$aar_got" = "$aar_want" ] || die "the tsbackgroundfetch AAR gradle resolved (sha256 $aar_got) is not labs/m2's '$native' ($aar_want)"

APK="$HERE/app/build/app/outputs/flutter-apk/app-debug.apk"
[ -f "$APK" ] || die "no $APK after the build"
sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
bt="$(ls -d "$sdk"/build-tools/* 2>/dev/null | sort -V | tail -1)"
if [ -x "$bt/aapt2" ]; then
  pkg="$("$bt/aapt2" dump badging "$APK" 2>/dev/null | sed -n "s/^package: name='\([^']*\)'.*/\1/p")"
  [ "$pkg" = "com.transistorsoft.fetchlab.flutter" ] || die "the APK is package '$pkg', lab.yaml names com.transistorsoft.fetchlab.flutter"
fi
# Listed into a variable first: under pipefail, `unzip -l | grep -q` fails whenever grep's early exit SIGPIPEs unzip.
dex="$(unzip -p "$APK" 'classes*.dex' | LC_ALL=C strings -a)"
for cls in 'Lcom/transistorsoft/tsbackgroundfetch/BackgroundFetch;' \
           'Lcom/transistorsoft/flutter/backgroundfetch/BackgroundFetchPlugin;' \
           'Lcom/transistorsoft/fetchlab/labprobe/LabProbePlugin;' \
           'Lcom/transistorsoft/fetchlab/flutter/MainActivity;'; do
  grep -qF "$cls" <<<"$dex" || die "the APK's dex defines no $cls"
done
# A debug APK carries the Dart program as a kernel blob, and a kernel carries each library's source text whole: the
# driver (app/lib/main.dart) and the staged plugin's Dart (every .stage/background_fetch/lib/**/*.dart) must each be in
# it byte for byte, so an APK whose Dart is not these files as they are now is refused (the Capacitor host cmp's its
# bg.js and index.html the same way).  lab_probe's Dart is not imported by the driver, so it is not in the kernel; its
# native half is the dex check above.
KERNEL="$STAGE_ROOT/kernel_blob.bin"
unzip -p "$APK" assets/flutter_assets/kernel_blob.bin > "$KERNEL" || die "the APK has no assets/flutter_assets/kernel_blob.bin"
python3 - "$KERNEL" "$HERE/app/lib/main.dart" "$STAGE/lib" <<'PY'
import os, sys
kernel = open(sys.argv[1], "rb").read()
files = [sys.argv[2]] + sorted(os.path.join(r, n) for r, _, ns in os.walk(sys.argv[3]) for n in ns if n.endswith(".dart"))
if len(files) < 2:
    sys.exit("[lab] the stage has no lib/**/*.dart to prove")
missing = [f for f in files if open(f, "rb").read() not in kernel]
if missing:
    sys.exit("[lab] the APK's Dart kernel does not carry, byte for byte: %s" % ", ".join(missing))
print("[lab] kernel carries %d Dart sources byte for byte (the driver and the stage's lib/)" % len(files))
PY
stage_py verify "$want_real" "$STAGE"

size="$(stat -f %z "$APK" 2>/dev/null || stat -c %s "$APK")"
fv="$(flutter --version --machine 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("frameworkVersion","?"))' || echo '?')"
echo "[lab] built android native=$native (commit $(prov commit | cut -c1-12) dirty=$(prov dirty)) aar=sha256:${aar_got:0:12}" \
     "plugin=$want_real flutter=$fv apk=$APK size=$size"
