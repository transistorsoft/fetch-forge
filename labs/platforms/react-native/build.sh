#!/usr/bin/env bash
# React Native fetch-lab build (WO-002).  usage: build.sh <native label> <plugin checkout|default>
#
# Modelled on bg-forge's labs/platforms/react-native/build.sh (49d38d47f), Android only in this slice.
#
#   <native label>   a tsbackgroundfetch snapshot published into labs/m2 (labs/m2/.provenance/<label>.txt).
#                    The app resolves com.transistorsoft:tsbackgroundfetch:<label> from labs/m2 ONLY
#                    (app/android/build.gradle's exclusiveContent), and this script refuses an APK whose
#                    resolved version is not the label or whose AAR is not byte-identical to labs/m2's.
#   <plugin>         `default` (lab.yaml default_checkout: <repo>/react-native/background-fetch) or a path
#                    to another checkout of react-native-background-fetch.
#
# DEBUG build with a pre-made `--dev false` JS bundle; MainApplication passes useDevSupport = false, so the app
# never looks for Metro.
#
# The plugin is STAGED, never linked: its working-tree files (git ls-files -co --exclude-standard, minus
# example/) are copied into ../.stage/react-native-background-fetch (gitignored), which app/package.json links.
# A link into the checkout itself would have Gradle write android/build/ into it — the plugin checkout is a
# submodule this lab only reads.  The stage records what it was copied from (.lab-source), and the build
# refuses a link that does not resolve to the stage of the checkout it was given.
set -euo pipefail
native="${1:?usage: build.sh <native label> <plugin checkout|default>}"; plugin="${2:-default}"
HERE="$(cd "$(dirname "$0")" && pwd)"
LABS="$(cd "$HERE/../.." && pwd)"
REPO="$(cd "$LABS/.." && pwd)"
die() { echo "[lab] $*" >&2; exit 1; }

[ "${LAB_OS:-android}" = "android" ] || die "LAB_OS=${LAB_OS}: the React Native fetch lab has no iOS host yet (lab.yaml has no ios: block)"

# --- the native snapshot ------------------------------------------------------------------------------------------

M2="$LABS/m2"
PROV="$M2/.provenance/$native.txt"
MOD="$M2/com/transistorsoft/tsbackgroundfetch/$native"
AAR="$MOD/tsbackgroundfetch-$native.aar"
[ -f "$PROV" ] || die "no Android snapshot labelled '$native': $PROV is missing — publish it into labs/m2 first"
[ -f "$AAR" ] && [ -f "$MOD/tsbackgroundfetch-$native.pom" ] \
  || die "labs/m2 has provenance for '$native' but no tsbackgroundfetch-$native.aar/.pom under $MOD (a publish still running, or one that failed)"
aar_want="$(shasum -a 256 "$AAR" | cut -d' ' -f1)"
prov() { sed -n "s/^$1:[[:space:]]*//p" "$PROV" | head -1; }

# --- the plugin checkout, staged --------------------------------------------------------------------------------

if [ "$plugin" = "default" ] || [ -z "$plugin" ]; then
  want="$REPO/react-native/background-fetch"      # lab.yaml default_checkout
else
  want="$plugin"
fi
want_real="$(cd "$want" 2>/dev/null && pwd -P)" || die "no plugin checkout at $want"
git -C "$want_real" rev-parse --show-toplevel >/dev/null 2>&1 || die "$want_real is not a git checkout"
[ -f "$want_real/package.json" ] && [ "$(node -p "require('$want_real/package.json').name")" = "react-native-background-fetch" ] \
  || die "$want_real is not react-native-background-fetch"

STAGE_ROOT="$HERE/.stage"
STAGE="$STAGE_ROOT/react-native-background-fetch"
mkdir -p "$STAGE"

# stage <checkout> <stage>: copy the checkout's working-tree files in, drop what it no longer has (the stage's own
# build output excepted), and write .lab-source.  verify <checkout> <stage>: the stage still holds exactly those
# bytes and names that checkout.  Read-only on the checkout: GIT_OPTIONAL_LOCKS=0 keeps `git status` from
# refreshing its index.
stage_py() {
  GIT_OPTIONAL_LOCKS=0 python3 - "$@" <<'PY'
import hashlib, os, shutil, subprocess, sys
mode, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
KEEP = ("android/build/", "android/.gradle/", "android/.cxx/")   # the build's output: only the stage ever has it
SKIP = ("example/",)                                              # the demo apps are not the bridge

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

# --- JavaScript dependencies ------------------------------------------------------------------------------------

cd "$HERE/app"
# Always install: node_modules holds the link this build tests (bg-forge's rule — a skipped install once built the
# previous checkout under a variant's name).  The link always names the stage; the stage names the checkout.
npm install --no-audit --no-fund
got_real="$(cd node_modules/react-native-background-fetch 2>/dev/null && pwd -P)" || got_real="(dangling)"
[ "$got_real" = "$(cd "$STAGE" && pwd -P)" ] \
  || die "node_modules/react-native-background-fetch resolves to $got_real, not the stage $STAGE — restore app/package.json's file:../.stage/react-native-background-fetch"
stage_py verify "$want_real" "$STAGE"

# The drivers' shared reporter (labs/platforms/_shared, vendored byte-identical from bg-forge): Metro bundles it
# from there (metro.config.js watchFolders), so a missing file is a build failure, never a silent stub.
for f in labapi.js labevents.js logo.js; do
  [ -f "$LABS/platforms/_shared/$f" ] || die "labs/platforms/_shared/$f is missing (the vendored LabKit JavaScript)"
done

# --- Android ----------------------------------------------------------------------------------------------------

sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
[ -d "$sdk" ] || die "no Android SDK at $sdk — set ANDROID_HOME"
# local.properties is gitignored and nothing else writes it: without it gradle stops at "SDK location not found".
[ -f android/local.properties ] || echo "sdk.dir=$sdk" > android/local.properties

mkdir -p android/app/src/main/assets
BUNDLE=android/app/src/main/assets/index.android.bundle
rm -f "$BUNDLE"
npx react-native bundle --platform android --dev false --entry-file index.js \
  --bundle-output "$BUNDLE" --assets-dest android/app/src/main/res
grep -q '__DEV__=false' "$BUNDLE" || die "$BUNDLE is not a --dev false bundle"

# --no-configuration-cache: labNativeResolved reads the resolved graph at execution time, which a cached
# configuration would not re-resolve (and the task declares itself incompatible with it).
LOG="$STAGE_ROOT/gradle.log"
set +e
(cd android && ./gradlew :app:assembleDebug :app:labNativeResolved --no-configuration-cache \
    -PlabNativeVersion="$native" -PlabMavenRepo="$M2") 2>&1 | tee "$LOG"
status="${PIPESTATUS[0]}"
set -e
[ "$status" -eq 0 ] || die "gradle failed ($status) — $LOG"

# --- prove WHICH native SDK the APK links, from the resolved graph and the artifact, never from the label ---------

resolved_lines="$(grep '^\[lab\] tsbackgroundfetch resolved=' "$LOG" || true)"
[ "$(printf '%s\n' "$resolved_lines" | grep -c . || true)" -eq 1 ] \
  || die "expected exactly one resolved tsbackgroundfetch in debugRuntimeClasspath, got: ${resolved_lines:-none}"
echo "$resolved_lines"
resolved="$(printf '%s\n' "$resolved_lines" | sed -n 's/^\[lab\] tsbackgroundfetch resolved=\([^ ]*\).*/\1/p')"
[ "$resolved" = "$native" ] || die "the app resolved tsbackgroundfetch '$resolved', not '$native'.
Something overrode the pin (the plugin's own safeExtGet('backgroundFetchVersion', '4.1.+') floor, a forced
version, or a conflict) — the APK does NOT contain the SDK under test."
aar_got="$(sed -n 's/^\[lab\] tsbackgroundfetch aar=.* sha256=\([0-9a-f]*\)$/\1/p' "$LOG" | tail -1)"
[ -n "$aar_got" ] || die "labNativeResolved printed no AAR for tsbackgroundfetch — what the APK links is unverified"
[ "$aar_got" = "$aar_want" ] || die "the tsbackgroundfetch AAR gradle resolved (sha256 $aar_got) is not labs/m2's '$native' ($aar_want)"

APK="$HERE/app/android/app/build/outputs/apk/debug/app-debug.apk"
[ -f "$APK" ] || die "no $APK after the build"
bt="$(ls -d "$sdk"/build-tools/* 2>/dev/null | sort -V | tail -1)"
if [ -x "$bt/aapt2" ]; then
  pkg="$("$bt/aapt2" dump badging "$APK" 2>/dev/null | sed -n "s/^package: name='\([^']*\)'.*/\1/p")"
  [ "$pkg" = "com.transistorsoft.fetchlab.react" ] || die "the APK is package '$pkg', lab.yaml names com.transistorsoft.fetchlab.react"
fi
# Listed into a variable first: under pipefail, `unzip -l | grep -q` fails whenever grep's early exit SIGPIPEs unzip.
listing="$(unzip -l "$APK")"
grep -q 'assets/index.android.bundle' <<<"$listing" || die "the APK carries no assets/index.android.bundle — it would wait for Metro"
[ "$(unzip -p "$APK" 'classes*.dex' | LC_ALL=C grep -c 'Lcom/transistorsoft/tsbackgroundfetch/BackgroundFetch;' || true)" -gt 0 ] \
  || die "the APK's dex defines no com.transistorsoft.tsbackgroundfetch.BackgroundFetch"

size="$(stat -f %z "$APK" 2>/dev/null || stat -c %s "$APK")"
echo "[lab] built android native=$native (commit $(prov commit | cut -c1-12) dirty=$(prov dirty)) aar=sha256:${aar_got:0:12}" \
     "plugin=$want_real apk=$APK size=$size"
