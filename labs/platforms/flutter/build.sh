#!/usr/bin/env bash
# Flutter fetch-lab build (WO-002).  usage: build.sh <native label> <plugin checkout|default>
#
# Modelled on bg-forge's labs/platforms/flutter/build.sh (addc50501) with the fetch hosts' staging and native-pin proofs
# (labs/platforms/capacitor/build.sh).  LAB_OS (run.py sets it from the platform view's OS) picks the branch: android
# (the default) or ios, the Simulator app, on the channel LAB_CHANNEL names (pods, the default, or spm).
#
#   <native label>   Android: a tsbackgroundfetch snapshot published into labs/m2 (labs/m2/.provenance/<label>.txt).  The
#                    app resolves com.transistorsoft:tsbackgroundfetch:<label> from labs/m2 ONLY
#                    (app/android/build.gradle.kts' exclusiveContent), and this script refuses an APK whose resolved
#                    version is not the label or whose AAR is not byte-identical to labs/m2's.
#                    iOS: the Simulator snapshot of the same label in LAB_XCFRAMEWORKS
#                    (labs/harness/publish_native_fetch_ios.sh), pinned by path (pods) or through a SwiftPM mirror of
#                    the label (spm); this script refuses an app whose linked slice is not that snapshot's.
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
  # (WO-002, iOS) The labelled Simulator snapshot labs/harness/publish_native_fetch_ios.sh wrote: an xcframework, the
  # checkout's podspec stamped with its marketing version (the plugin's `~> 4.2` resolves against it) and a
  # Package.swift whose binaryTarget is that xcframework by path (the Capacitor and React Native hosts' store).
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
KEEP = (".dart_tool/", "build/", "android/build/", "android/.gradle/", "android/.cxx/",   # only the stage ever has these
        "ios/background_fetch/.swiftpm/", "ios/background_fetch/.build/")                # (Xcode's, on a path package)
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
[ -f "$STAGE/lib/background_fetch.dart" ] || die "the stage has no lib/background_fetch.dart"
[ -f "$HERE/app/lib/main.dart" ] || die "app/lib/main.dart is missing (the Dart driver)"

# resolves_stage <package_config.json>: the plugin pub resolved is the stage (pub records a path package's resolved path).
resolves_stage() {
  python3 - "$1" "$STAGE" <<'PY'
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
}

# kernel_proof <kernel_blob.bin>: a debug build carries the Dart program as a kernel blob, and a kernel carries each
# library's source text whole: the driver (app/lib/main.dart) and the staged plugin's Dart (every
# .stage/background_fetch/lib/**/*.dart) must each be in it byte for byte, so an artifact whose Dart is not these files
# as they are now is refused (the Capacitor host cmp's its bg.js and index.html the same way).  lab_probe's Dart is not
# imported by the driver, so it is not in the kernel; its native half is the dex check (Android).
kernel_proof() {
  python3 - "$1" "$HERE/app/lib/main.dart" "$STAGE/lib" <<'PY'
import os, sys
kernel = open(sys.argv[1], "rb").read()
files = [sys.argv[2]] + sorted(os.path.join(r, n) for r, _, ns in os.walk(sys.argv[3]) for n in ns if n.endswith(".dart"))
if len(files) < 2:
    sys.exit("[lab] the stage has no lib/**/*.dart to prove")
missing = [f for f in files if open(f, "rb").read() not in kernel]
if missing:
    sys.exit("[lab] the Dart kernel does not carry, byte for byte: %s" % ", ".join(missing))
print("[lab] kernel carries %d Dart sources byte for byte (the driver and the stage's lib/)" % len(files))
PY
}

# write_if_changed <path>: stdin into <path> only when the bytes differ, so a rebuild with nothing new moves no mtime
# (Flutter fingerprints the Podfile and the Xcode project, and runs pod install again when either changes).
write_if_changed() {
  local tmp; tmp="$(mktemp "$1.lab.XXXXXX")"
  cat > "$tmp"
  if cmp -s "$tmp" "$1"; then rm -f "$tmp"; else mv "$tmp" "$1"; fi
}

# --- iOS (WO-002) --------------------------------------------------------------------------------------------------
#
# Each channel builds its own GENERATED Flutter project, app/build/ios-pods or app/build/ios-spm (gitignored, under a
# directory run.py's source stamp skips).  Flutter takes `enable-swift-package-manager` from the pubspec of the project
# it runs in, and that outranks the global setting, which is `true` on this Mac (flutter_tools
# flutter_features_config.dart:104-115); a project per channel is the one way to build both without editing a tracked
# file per build, and it leaves app/ the Android project it is.  app/lib/main.dart is copied in; the kernel proof holds
# the artifact to it.  The project's ios/ is `flutter create`'s, run inside the project so the template matches its
# channel, and regenerated when the Flutter SDK or the channel changes (ios/.lab-host).  Tracked: app/native-ios/
# AppDelegate.swift, copied over the template's.

if [ "$OS" = ios ]; then
  PB=/usr/libexec/PlistBuddy
  marketing="$(prov marketing)"; build_no="$(prov build)"; sha_want="$(prov sha256_sim)"
  [ -n "$marketing" ] && [ -n "$build_no" ] && [ -n "$sha_want" ] || die "$PROV lacks marketing/build/sha256_sim"
  [ -f "$STAGE/ios/background_fetch.podspec" ] && [ -f "$STAGE/ios/background_fetch/Package.swift" ] \
    || die "the stage has no ios/background_fetch.podspec or ios/background_fetch/Package.swift"
  H="$HERE/app/build/ios-$CHANNEL"
  spm=false; [ "$CHANNEL" = spm ] && spm=true
  mkdir -p "$H/lib"
  fv_json="$(flutter --version --machine 2>/dev/null)"
  fv="$(python3 -c 'import json,sys; d=json.load(sys.stdin); print("%s/%s/%s" % (d["frameworkVersion"], d["frameworkRevision"][:12], d["engineRevision"][:12]))' <<<"$fv_json")"
  flutter_root="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["flutterRoot"])' <<<"$fv_json")"

  # The project's pubspec: app/pubspec.yaml's SDK constraint and the stage, without lab_probe (Android's: iOS has no
  # headless engine, and its channel is the AppDelegate's), with this channel's switch.
  sdk_c="$(sed -n 's/^  sdk: *//p' "$HERE/app/pubspec.yaml" | head -1)"
  [ -n "$sdk_c" ] || die "app/pubspec.yaml has no environment sdk constraint"
  rel="$(python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$STAGE" "$H")"
  write_if_changed "$H/pubspec.yaml" <<YAML
# Generated by labs/platforms/flutter/build.sh (WO-002): app/'s iOS $CHANNEL host.  Do not edit; app/pubspec.yaml is the
# project's own.
name: fetch_lab
publish_to: 'none'
version: 1.0.0+1

environment:
  sdk: $sdk_c

dependencies:
  flutter:
    sdk: flutter
  background_fetch:
    path: $rel

flutter:
  uses-material-design: true
  config:
    enable-swift-package-manager: $spm
YAML
  cmp -s "$HERE/app/lib/main.dart" "$H/lib/main.dart" || cp "$HERE/app/lib/main.dart" "$H/lib/main.dart"

  host="flutter=$fv swiftpm=$spm"
  if [ "$(cat "$H/ios/.lab-host" 2>/dev/null)" != "$host" ]; then
    echo "[lab] generating $H/ios ($host)"
    rm -rf "${H:?}/ios" "${H:?}/build" "${H:?}/.dart_tool"
    tmp="$(mktemp -d "$STAGE_ROOT/create.XXXXXX")"
    (cd "$H" && flutter --suppress-analytics create --platforms=ios --org com.transistorsoft.fetchlab \
        --project-name fetch_lab --no-pub "$tmp/fetch_lab" > "$tmp/create.log" 2>&1) \
      || { tail -20 "$tmp/create.log"; die "flutter create failed ($tmp/create.log)"; }
    mv "$tmp/fetch_lab/ios" "$H/ios"
    rm -rf "${tmp:?}"
    cp "$H/ios/Runner/Info.plist" "$H/ios/.lab-Info.plist"     # the template's, pristine: the lab's keys go onto a copy
    echo "$host" > "$H/ios/.lab-host"
  fi
  (cd "$H" && flutter --suppress-analytics pub get > "$H/pub-get.log" 2>&1) \
    || { tail -20 "$H/pub-get.log"; die "flutter pub get failed ($H/pub-get.log)"; }
  resolves_stage "$H/.dart_tool/package_config.json"

  # The overlay, the bundle id, and the SDK's version string.  No SDK code references TSBackgroundFetchVersionString and
  # the slice is a static archive, so the linker takes that member only when something references it: without
  # `-Wl,-u,_TSBackgroundFetchVersionString` the app carries no PROJECT:TSBackgroundFetch-<build> string and the
  # harness's native_resolved refuses it (the Capacitor host's rule; a Swift file cannot name a C array).  Runner's three
  # configurations set the app's id; RunnerTests' three end in .RunnerTests and are left alone.  Written only when it
  # changes: Flutter fingerprints the project file.
  cmp -s "$HERE/app/native-ios/AppDelegate.swift" "$H/ios/Runner/AppDelegate.swift" \
    || cp "$HERE/app/native-ios/AppDelegate.swift" "$H/ios/Runner/AppDelegate.swift"
  python3 - "$H/ios/Runner.xcodeproj/project.pbxproj" "$BUNDLE" <<'PY'
import re, sys
path, bundle = sys.argv[1], sys.argv[2]
old = open(path).read()
s, n = re.subn(r"PRODUCT_BUNDLE_IDENTIFIER = (?![^;]*RunnerTests)[^;]*;", "PRODUCT_BUNDLE_IDENTIFIER = %s;" % bundle, old)
if n != 3:
    sys.exit("[lab] the template's project sets Runner's PRODUCT_BUNDLE_IDENTIFIER %d times, not its 3 configurations'" % n)
flag = '\t\t\t\tOTHER_LDFLAGS = (\n\t\t\t\t\t"$(inherited)",\n\t\t\t\t\t"-Wl,-u,_TSBackgroundFetchVersionString",\n\t\t\t\t);\n'
s = s.replace(flag, "")                                   # this build's, never a stale one
s, n = re.subn(r"(\t\t\t\tPRODUCT_BUNDLE_IDENTIFIER = %s;\n)" % re.escape(bundle), lambda m: m.group(1) + flag, s)
if n != 3:
    sys.exit("[lab] could not add OTHER_LDFLAGS to Runner's 3 configurations (%d)" % n)
if s != old:
    open(path, "w").write(s)
PY

  # The keys the SDK's iOS setup needs, which the template carries none of: UIBackgroundModes fetch and processing, and
  # BGTaskSchedulerPermittedIdentifiers — the SDK's fetch task and the lab's custom task (ios-task-sim, api-smoke-ios).
  # Onto a pristine copy of the template's plist each build, so an edit here always reaches an existing host.
  PLIST="$H/ios/Runner/Info.plist"
  cp "$H/ios/.lab-Info.plist" "$PLIST.lab"
  $PB -c "Add :UIBackgroundModes array" -c "Add :UIBackgroundModes:0 string fetch" \
      -c "Add :UIBackgroundModes:1 string processing" "$PLIST.lab"
  $PB -c "Add :BGTaskSchedulerPermittedIdentifiers array" \
      -c "Add :BGTaskSchedulerPermittedIdentifiers:0 string com.transistorsoft.fetch" \
      -c "Add :BGTaskSchedulerPermittedIdentifiers:1 string com.transistorsoft.fetchlab.task" "$PLIST.lab"
  $PB -c "Set :CFBundleDisplayName 'Fetch Flutter Lab'" "$PLIST.lab" 2>/dev/null \
    || $PB -c "Add :CFBundleDisplayName string 'Fetch Flutter Lab'" "$PLIST.lab"
  if cmp -s "$PLIST.lab" "$PLIST"; then rm -f "$PLIST.lab"; else mv "$PLIST.lab" "$PLIST"; fi

  PRODUCTS="$H/build/ios/Debug-iphonesimulator"
  case "$CHANNEL" in
  pods)
    # Flutter's own Podfile template, with the two edits the plugin's install docs and the lab need: static linkage
    # (help/INSTALL-IOS.md: CocoaPods refuses the dynamic default) and TSBackgroundFetch pinned to the labelled
    # snapshot by a LITERAL path, so a label switch changes the Podfile, which Flutter's fingerprint and pod's checksum
    # both see (bg-forge MEASURED an ENV path building against the previous label's slice).  Written only when it
    # changes.  The plugin itself comes as Flutter's development pod, .symlinks/plugins/background_fetch → the stage.
    [ ! -e "$SNAPSHOT/TSBackgroundFetch.framework" ] \
      || die "$SNAPSHOT holds a TSBackgroundFetch.framework, which the link would take over the proven slice"
    python3 - "$flutter_root/packages/flutter_tools/templates/cocoapods/Podfile-ios" "$SNAPSHOT" > "$H/ios/Podfile.lab" <<'PY'
import re, sys
s = open(sys.argv[1]).read()
s, n1 = re.subn(r"^# platform :ios, '15\.0'$", "platform :ios, '15.0'", s, flags=re.M)
s, n2 = re.subn(r"^  use_frameworks!$",
                "  use_frameworks! :linkage => :static   # (WO-002) the plugin's install docs\n"
                "  pod 'TSBackgroundFetch', :path => '%s'   # (WO-002) the labelled snapshot" % sys.argv[2], s, flags=re.M)
if n1 != 1 or n2 != 1:
    sys.exit("[lab] Flutter's Podfile template changed: platform %d, use_frameworks! %d" % (n1, n2))
sys.stdout.write(s)
PY
    write_if_changed "$H/ios/Podfile" < "$H/ios/Podfile.lab"
    rm -f "$H/ios/Podfile.lab"
    # Install whenever the lock does not name this snapshot, no longer matches the sandbox, was made from another
    # Podfile, or from other development-pod inputs: the plugin's podspec (its version is the stage's pubspec's), its
    # file list, or the snapshot's podspec, which a re-publish under the same label rewrites (the Capacitor host's rule).
    LOCK="$H/ios/Podfile.lock"
    inputs="$( (cd "$STAGE" && find ios -type f -not -path '*/.swiftpm/*' -not -path '*/.build/*' | sort
                cat ios/background_fetch.podspec pubspec.yaml "$SNAPSHOT/TSBackgroundFetch.podspec") | shasum -a 1 | cut -d' ' -f1)"
    if ! grep -q "TSBackgroundFetch (from \`$SNAPSHOT\`)" "$LOCK" 2>/dev/null \
       || ! cmp -s "$LOCK" "$H/ios/Pods/Manifest.lock" \
       || ! grep -q "^PODFILE CHECKSUM: $(shasum -a 1 "$H/ios/Podfile" | cut -d' ' -f1)$" "$LOCK" \
       || [ "$(cat "$H/ios/Pods/.lab-inputs" 2>/dev/null)" != "$inputs" ]; then
      echo "[lab] pod install (snapshot, plugin or Podfile changed)"
      rm -rf "$PRODUCTS/XCFrameworkIntermediates/TSBackgroundFetch"
      (cd "$H/ios" && LANG=en_US.UTF-8 pod install > "$H/pod-install.log" 2>&1) \
        || { tail -30 "$H/pod-install.log" || true; die "pod install failed ($H/pod-install.log)"; }
      echo "$inputs" > "$H/ios/Pods/.lab-inputs"
    fi
    ;;
  spm)
    # The label as the package the plugin asks for (the React Native and Capacitor hosts' mirror).  The plugin's
    # Package.swift wants .package(url: "https://github.com/transistorsoft/transistor-background-fetch.git", from:
    # "4.2.0"): a SwiftPM mirror maps that URL to a local git repo of the label, tagged with its iOS version, so the
    # plugin's manifest is never edited and its own requirement is what resolves.  One mirror per label, its commit made
    # with fixed dates: SwiftPM records each version's revision per location and refuses it at another revision later.
    # Flutter runs xcodebuild twice over: the package prefetch on the PROJECT container and the build on the WORKSPACE
    # (flutter_tools xcode_project.dart:409-419, mac.dart:421), so the mirror goes in both.  Its checkouts are the
    # project's own (-clonedSourcePackagesDirPath build/ios/SourcePackages, xcodeproj.dart:206-208).
    SRCPKG="$H/build/ios/SourcePackages"
    RES_WS="$H/ios/Runner.xcworkspace/xcshareddata/swiftpm/Package.resolved"
    RES_PJ="$H/ios/Runner.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
    MIRROR="$H/build/lab-mirror/$native/transistor-background-fetch"
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
    # Another label's mirror, or another stage content (the stage's folder name never changes, so Flutter's rename that
    # makes Xcode re-read a plugin manifest never happens, swift_package_manager.dart:185-188): the checkouts and pins go.
    spm_inputs="$mirrors $(sed -n 's/.* content=\([0-9a-f]*\).*/\1/p' "$STAGE/.lab-source")"
    if [ "$(cat "$H/build/ios/.lab-spm-inputs" 2>/dev/null)" != "$spm_inputs" ]; then
      rm -rf "${SRCPKG:?}" "$RES_WS" "$RES_PJ"
    fi
    for cfg in "$H/ios/Runner.xcworkspace/xcshareddata/swiftpm/configuration" \
               "$H/ios/Runner.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/configuration"; do
      mkdir -p "$cfg"
      printf '%s\n' "$mirrors" | write_if_changed "$cfg/mirrors.json"
    done
    ;;
  esac

  # run.py's stamp sits beside the artifact; the build replaces it, so the stamp goes first (the Android branch's rule).
  OUT="$HERE/app/build/lab-ios/Runner.app"
  rm -f "$OUT.lab-stamp"
  echo "[lab] flutter build ios --simulator --debug ($CHANNEL) — $H/flutter-build.log"
  (cd "$H" && flutter --suppress-analytics build ios --simulator --debug > "$H/flutter-build.log" 2>&1) \
    || { grep -E 'error:|Error |BUILD FAILED|Failed' "$H/flutter-build.log" | tail -30 || true; die "flutter build ios failed ($H/flutter-build.log)"; }

  case "$CHANNEL" in
  pods)
    grep -q "TSBackgroundFetch (from \`$SNAPSHOT\`)" "$LOCK" \
      || die "Podfile.lock does not record the snapshot as TSBackgroundFetch's source"
    grep -q "background_fetch (from \`.symlinks/plugins/background_fetch/ios\`)" "$LOCK" \
      || die "Podfile.lock does not record the plugin as Flutter's development pod"
    [ "$(cd "$H/ios/.symlinks/plugins/background_fetch" 2>/dev/null && pwd -P)" = "$(cd "$STAGE" && pwd -P)" ] \
      || die "ios/.symlinks/plugins/background_fetch is not the stage"
    SLICE="$PRODUCTS/XCFrameworkIntermediates/TSBackgroundFetch/TSBackgroundFetch.framework"
    ;;
  spm)
    # Which package SwiftPM resolved: the SDK at the mirror's tag and commit, in both containers' pins — the build's
    # (workspace) and the prefetch's (project); a build writes both (MEASURED 2026-11-14).
    want_pin="transistor-background-fetch $marketing $(git -C "$MIRROR" rev-parse HEAD)"
    for res in "$RES_WS" "$RES_PJ"; do
      [ -f "$res" ] || die "the build wrote no $res"
      pin="$(python3 - "$res" <<'PY'
import json, sys
pins = {p.get("location"): p for p in json.load(open(sys.argv[1])).get("pins", [])}
p = pins.get("https://github.com/transistorsoft/transistor-background-fetch.git")
print("%s %s %s" % (p["identity"], p["state"].get("version") or "-", p["state"].get("revision") or "-") if p else "none")
PY
)"
      [ "$pin" = "$want_pin" ] || die "$res pins transistor-background-fetch as '$pin', not the mirror of '$native' ($want_pin)"
    done
    # The plugin package Flutter generated names the stage (FlutterGeneratedPluginSwiftPackage's .packages symlink).
    link="$H/ios/Flutter/ephemeral/Packages/.packages/background_fetch"
    [ "$(cd "$link" 2>/dev/null && pwd -P)" = "$(cd "$STAGE/ios/background_fetch" && pwd -P)" ] \
      || die "$link is not the stage's ios/background_fetch"
    printf '%s\n' "$spm_inputs" > "$H/build/ios/.lab-spm-inputs"
    SLICE="$PRODUCTS/TSBackgroundFetch.framework"
    ;;
  esac

  # --- prove WHICH native SDK, plugin and driver the app carries, from the artifact, never the label ---------------
  APP="$H/build/ios/iphonesimulator/Runner.app"
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
  defines() { [ "$(nm -U "$1" 2>/dev/null | grep -c "_OBJC_CLASS_\$_$2\$")" -gt 0 ]; }
  for cls in BackgroundFetchPlugin TSBackgroundFetch; do
    defines "$carriers" "$cls" || die "$carriers does not define $cls"
  done
  # And the SDK's code is in that binary alone (bg-forge's TSConfig check): a second copy elsewhere would give the
  # probe and the plugin two singletons.
  sdk_code="$(find "$APP" -type f \( -perm -u+x -o -name '*.dylib' \) | sort | while read -r f; do
      defines "$f" TSBackgroundFetch && echo "$f"; done || true)"
  [ "$sdk_code" = "$carriers" ] || die "TSBackgroundFetch is defined in [${sdk_code:-nothing}], its version string in [$carriers]"
  for mark in "LAB point=legacyFetch" "lab/probe"; do
    [ "$(strings - "$carriers" | grep -cF "$mark")" -gt 0 ] || die "$carriers lacks '$mark' (app/native-ios/AppDelegate.swift)"
  done
  id_got="$($PB -c 'Print CFBundleIdentifier' "$APP/Info.plist")"
  [ "$id_got" = "$BUNDLE" ] || die "the built app is $id_got, lab.yaml names $BUNDLE"
  for key in "UIBackgroundModes" "BGTaskSchedulerPermittedIdentifiers"; do
    $PB -c "Print :$key" "$APP/Info.plist" >/dev/null 2>&1 || die "the built app's Info.plist has no $key"
  done
  kernel_proof "$APP/Frameworks/App.framework/flutter_assets/kernel_blob.bin"
  stage_py verify "$want_real" "$STAGE"

  # One artifact whichever channel built it (the React Native and Capacitor hosts' rule, bg-forge WO-192 D2):
  # lab.yaml's ios build.app.
  rm -rf "$OUT" "$OUT.lab-stamp" && mkdir -p "$(dirname "$OUT")" && ditto "$APP" "$OUT"
  echo "[lab] built ios channel=$CHANNEL native=$native ($marketing/$build_no, commit $(prov commit | cut -c1-12))" \
       "plugin=$want_real flutter=$fv app=$OUT"
  exit 0
fi

# --- the app -------------------------------------------------------------------------------------------------------

[ -f "$STAGE/android/build.gradle" ] || die "the stage has no android/build.gradle"
cd "$HERE/app"
LOG="$STAGE_ROOT/flutter-build.log"
flutter --suppress-analytics pub get > "$STAGE_ROOT/pub-get.log" 2>&1 \
  || { tail -20 "$STAGE_ROOT/pub-get.log"; die "flutter pub get failed ($STAGE_ROOT/pub-get.log)"; }
resolves_stage .dart_tool/package_config.json

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
# The APK's Dart is app/lib/main.dart and the stage's lib/, byte for byte (kernel_proof).
KERNEL="$STAGE_ROOT/kernel_blob.bin"
unzip -p "$APK" assets/flutter_assets/kernel_blob.bin > "$KERNEL" || die "the APK has no assets/flutter_assets/kernel_blob.bin"
kernel_proof "$KERNEL"
stage_py verify "$want_real" "$STAGE"

size="$(stat -f %z "$APK" 2>/dev/null || stat -c %s "$APK")"
fv="$(flutter --version --machine 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("frameworkVersion","?"))' || echo '?')"
echo "[lab] built android native=$native (commit $(prov commit | cut -c1-12) dirty=$(prov dirty)) aar=sha256:${aar_got:0:12}" \
     "plugin=$want_real flutter=$fv apk=$APK size=$size"
