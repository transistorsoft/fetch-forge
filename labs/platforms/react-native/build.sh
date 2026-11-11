#!/usr/bin/env bash
# React Native fetch-lab build (WO-002).  usage: build.sh <native label> <plugin checkout|default>
#
# Modelled on bg-forge's labs/platforms/react-native/build.sh (49d38d47f; its iOS branch at 0fe0eb14c).  LAB_OS
# (run.py sets it from the platform view's OS) picks the branch: android (the default) or ios (WO-002 phase 2).
#
#   <native label>   Android: a tsbackgroundfetch snapshot published into labs/m2 (labs/m2/.provenance/<label>.txt).
#                    The app resolves com.transistorsoft:tsbackgroundfetch:<label> from labs/m2 ONLY
#                    (app/android/build.gradle's exclusiveContent), and this script refuses an APK whose
#                    resolved version is not the label or whose AAR is not byte-identical to labs/m2's.
#                    iOS: a TSBackgroundFetch.xcframework snapshot in labs/xcframeworks/<label>
#                    (publish_native_fetch_ios.sh).  LAB_CHANNEL (the variant's `channel:`, bg-forge WO-192) picks
#                    how the app gets it: `pods` (the default), the Podfile's `:path => ENV['LAB_TSBF']` in app/ios;
#                    `spm`, a SwiftPM mirror of the label for the plugin's own Package.swift, in app/ios-spm.  Either
#                    way this script refuses an app whose linked slice is not the label's (sha256, version, PROJECT
#                    string), and copies the one it proved to app/build/lab-ios/FetchLab.app.
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

OS="${LAB_OS:-android}"
case "$OS" in android|ios) ;; *) die "LAB_OS=$OS: this lab builds android or ios" ;; esac

# --- the native snapshot (Android; iOS reads its own below) -------------------------------------------------------

M2="$LABS/m2"
PROV="$M2/.provenance/$native.txt"
MOD="$M2/com/transistorsoft/tsbackgroundfetch/$native"
AAR="$MOD/tsbackgroundfetch-$native.aar"
if [ "$OS" = android ]; then
  [ -f "$PROV" ] || die "no Android snapshot labelled '$native': $PROV is missing — publish it into labs/m2 first"
  [ -f "$AAR" ] && [ -f "$MOD/tsbackgroundfetch-$native.pom" ] \
    || die "labs/m2 has provenance for '$native' but no tsbackgroundfetch-$native.aar/.pom under $MOD (a publish still running, or one that failed)"
  aar_want="$(shasum -a 256 "$AAR" | cut -d' ' -f1)"
fi
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

# --- iOS (WO-002 phase 2) -----------------------------------------------------------------------------------------
#
# app/ios is COMMITTED (D5): React Native 0.88.0's template (@react-native-community/template, renamed FetchLab, as
# `init` does) plus the lab's edits — AppDelegate.swift (the legacy-fetch forward, D3), SceneDelegate.swift (the
# embedded bundle), Info.plist (background modes, BGTask ids), the bundle id, the Podfile's pins, and the LabModule
# pod.  app/ios-spm (phase 2b) is the same template converted to Swift Package Manager by React Native's own tool,
# sharing app/ios's sources.  This script only checks them; it edits no tracked file, and refuses a pod install or a
# SwiftPM step that does.

if [ "$OS" = ios ]; then
  PB=/usr/libexec/PlistBuddy
  BUNDLE_ID="${LAB_BUNDLE:?set LAB_BUNDLE to the bundle id in the ios: block of lab.yaml}"
  XCFW="${LAB_XCFRAMEWORKS:?set LAB_XCFRAMEWORKS to the labelled xcframework store, as the ios: block of lab.yaml does}"
  SNAPSHOT="$XCFW/$native"
  IPROV="$XCFW/.provenance/$native.txt"
  [ -d "$SNAPSHOT/TSBackgroundFetch.xcframework" ] && [ -f "$SNAPSHOT/TSBackgroundFetch.podspec" ] && [ -f "$IPROV" ] \
    || die "no iOS snapshot labelled '$native' in $XCFW — publish it: labs/harness/publish_native_fetch_ios.sh $native <native checkout> (./labkit setup --os ios does)"
  iprov() { sed -n "s/^$1:[[:space:]]*//p" "$IPROV" | head -1; }
  marketing="$(iprov marketing)"; build_no="$(iprov build)"; sha_want="$(iprov sha256_sim)"
  [ -n "$marketing" ] && [ -n "$build_no" ] && [ -n "$sha_want" ] || die "$IPROV lacks marketing/build/sha256_sim"

  # The committed host is a template of THIS app's React Native: one generated for another version builds that
  # version's project against these node_modules.
  rn="$(node -p "require('react-native/package.json').version")"
  tmpl="$(sed -n 's/^react-native=//p' ios/.lab-template 2>/dev/null)"
  [ "$tmpl" = "$rn" ] || die "app/ios is React Native ${tmpl:-?}'s template (ios/.lab-template), this app is $rn — regenerate it from @react-native-community/template@$rn"
  PLIST=ios/FetchLab/Info.plist
  modes="$($PB -c 'Print :UIBackgroundModes' "$PLIST" 2>/dev/null || true)"
  tasks="$($PB -c 'Print :BGTaskSchedulerPermittedIdentifiers' "$PLIST" 2>/dev/null || true)"
  for mode in fetch processing; do
    grep -qx "[[:space:]]*$mode" <<<"$modes" || die "$PLIST's UIBackgroundModes lacks '$mode'"
  done
  for id in com.transistorsoft.fetch com.transistorsoft.fetchlab.task; do
    grep -qx "[[:space:]]*$id" <<<"$tasks" || die "$PLIST's BGTaskSchedulerPermittedIdentifiers lacks '$id'"
  done
  # (WO-002 phase 2b, D4) How the app gets TSBackgroundFetch: the variant's `channel:` (bg-forge WO-192 hands it over
  # as LAB_CHANNEL; lab.yaml's ios: block declares both).  Empty is this view's default, CocoaPods.
  CHANNEL="${LAB_CHANNEL:-pods}"
  plugin_real="$(cd node_modules/react-native-background-fetch && pwd -P)"
  # tracked_sums <dir>…: one line per tracked path under them, `<state>  <path>`, so a step that changes one is caught.
  # The state of a symlink is its target, never followed: ios-spm/FetchLab is a tracked symlink to a directory, which
  # `shasum` cannot read (MEASURED 2026-11-10: `shasum: ios-spm/FetchLab: Is a directory` stopped every iOS build
  # once ios-spm was committed).  A tracked file that is gone reads `missing`.
  tracked_sums() {
    local f
    git ls-files -z "$@" | while IFS= read -r -d '' f; do
      if [ -L "$f" ]; then printf 'link:%s  %s\n' "$(readlink "$f")" "$f"
      elif [ -f "$f" ]; then shasum -a 1 "$f"
      else printf 'missing  %s\n' "$f"
      fi
    done
  }
  refuse_rewrites() {   # <before> <what ran> <dir>…
    local before="$1" what="$2"; shift 2
    local rewritten
    # The path is what follows the state's two spaces, whatever the state (digest, link:<target>, missing).
    rewritten="$(diff <(echo "$before") <(tracked_sums "$@") | sed -n 's/^> .*  //p' | sort -u | tr '\n' ' ' || true)"
    [ -z "$rewritten" ] || die "$what rewrote tracked files under app/: ${rewritten}— review and commit what it wrote, then build again"
  }

  case "$CHANNEL" in
  pods)
  ids="$(sed -n 's/.*PRODUCT_BUNDLE_IDENTIFIER = \([^;]*\);.*/\1/p' ios/FetchLab.xcodeproj/project.pbxproj | sort -u)"
  [ "$ids" = "$BUNDLE_ID" ] || die "ios/FetchLab.xcodeproj builds bundle id(s) '$ids', lab.yaml names $BUNDLE_ID"
  grep -q "pod 'TSBackgroundFetch', :path => ENV.fetch('LAB_TSBF')" ios/Podfile || die "ios/Podfile does not pin TSBackgroundFetch to LAB_TSBF"

  export LAB_TSBF="$SNAPSHOT"
  DERIVED=ios/build
  PRODUCTS="$DERIVED/Build/Products/Debug-iphonesimulator"
  mkdir -p "$DERIVED"
  # pod install whenever the lock does not name this snapshot, no longer matches the sandbox, was made from another
  # Podfile, or from other development pods: bg-forge's rule, measured there (a label switch changes only LAB_TSBF,
  # so nothing else looks stale; a plugin checkout's lock entry reads ../node_modules/… for every checkout; a codegen
  # spec change touches neither the file list nor the podspec).  The plugin's real path, its codegenConfig and spec
  # files, its native file list and podspec, and LabModule's podspec are recorded beside the sandbox after each
  # install and compared here.
  codegen="$(node -p "JSON.stringify(require('$plugin_real/package.json').codegenConfig || null)")"
  specs_dir="$plugin_real/$(node -p "(require('$plugin_real/package.json').codegenConfig || {}).jsSrcsDir || 'src'")"
  inputs="plugin=$plugin_real $( ( echo "$codegen"
    { find "$specs_dir" -type f \( -name '*.js' -o -name '*.ts' -o -name '*.tsx' \) 2>/dev/null | sort \
        | while read -r f; do echo "${f#"$specs_dir"}"; cat "$f"; done; } || true
    ls "$plugin_real/ios/RNBackgroundFetch"
    cat "$plugin_real/RNBackgroundFetch.podspec" ios/LabModule/LabModule.podspec ) | shasum -a 1 | cut -d' ' -f1)"
  if ! grep -q "TSBackgroundFetch (from \`$SNAPSHOT\`)" ios/Podfile.lock 2>/dev/null \
     || ! cmp -s ios/Podfile.lock ios/Pods/Manifest.lock \
     || ! grep -q "^PODFILE CHECKSUM: $(shasum -a 1 ios/Podfile | cut -d' ' -f1)$" ios/Podfile.lock \
     || [ "$(cat ios/Pods/.lab-inputs 2>/dev/null)" != "$inputs" ]; then
    echo "[lab] pod install (snapshot, Podfile or development pods changed) — $DERIVED/pod-install.log"
    rm -rf "$PRODUCTS/XCFrameworkIntermediates/TSBackgroundFetch"
    # React Native's react_native_post_install writes into the project (project.pbxproj's build settings, the
    # privacy manifest): MEASURED on the template's first install.  ios/ is tracked, so its committed state is that
    # output, which a second install leaves alone (MEASURED).  An install that rewrites a tracked file means the
    # committed host is stale: refuse, rather than build from files git does not hold.
    tracked_before="$(tracked_sums ios)"
    (cd ios && LANG=en_US.UTF-8 pod install > build/pod-install.log 2>&1) \
      || { tail -30 "$DERIVED/pod-install.log"; die "pod install failed ($DERIVED/pod-install.log)"; }
    refuse_rewrites "$tracked_before" "pod install" ios
    echo "$inputs" > ios/Pods/.lab-inputs
  fi

  # JavaScript: the template's "Bundle React Native code and images" phase, told to run for a Debug Simulator build
  # (FORCE_BUNDLING) and to bundle `--dev false` like the Android lab (react-native-xcode.sh appends
  # EXTRA_PACKAGER_ARGS after its own --dev, and the last one wins).  The copy left beside the app goes first, so the
  # check below reads this build's.
  rm -f "$PRODUCTS/main.jsbundle"
  echo "[lab] xcodebuild (CocoaPods) — $DERIVED/xcodebuild.log"
  LANG=en_US.UTF-8 xcodebuild -workspace ios/FetchLab.xcworkspace -scheme FetchLab \
    -configuration Debug -sdk iphonesimulator -arch "$(uname -m)" -derivedDataPath "$DERIVED" \
    FORCE_BUNDLING=1 EXTRA_PACKAGER_ARGS="--dev false" build > "$DERIVED/xcodebuild.log" 2>&1 \
    || { grep -E 'error:|BUILD FAILED' "$DERIVED/xcodebuild.log" | tail -30 || true; die "xcodebuild failed ($DERIVED/xcodebuild.log)"; }
  SLICE="$PRODUCTS/XCFrameworkIntermediates/TSBackgroundFetch/TSBackgroundFetch.framework"
  grep -q "TSBackgroundFetch (from \`$SNAPSHOT\`)" ios/Podfile.lock \
    || die "Podfile.lock does not record the snapshot path as TSBackgroundFetch's source"
  source_proof="Podfile.lock: TSBackgroundFetch from the snapshot"
  ;;

  spm)
  # app/ios-spm is the SwiftPM host (committed, WO-002 phase 2b): the same template converted by React Native's own
  # `setup-apple-spm.js add --deintegrate --config-command '["node","ios-spm/lab-config.js"]'`, its sources a symlink
  # to app/ios/FetchLab so both hosts run one AppDelegate and SceneDelegate, and LabModule an app module
  # (package.json swiftpmConfig.modules) compiled from app/ios/LabModule.
  H=ios-spm
  [ "$(readlink "$H/FetchLab")" = ../ios/FetchLab ] || die "$H/FetchLab is not the symlink to ../ios/FetchLab: the two hosts would run different app code"
  tmpl="$(sed -n 's/^react-native=//p' "$H/.lab-template" 2>/dev/null)"
  [ "$tmpl" = "$rn" ] || die "app/$H was generated for React Native ${tmpl:-?} ($H/.lab-template), this app is $rn — regenerate it"
  ids="$(sed -n 's/.*PRODUCT_BUNDLE_IDENTIFIER = \([^;]*\);.*/\1/p' "$H/FetchLab.xcodeproj/project.pbxproj" | sort -u)"
  [ "$ids" = "$BUNDLE_ID" ] || die "$H/FetchLab.xcodeproj builds bundle id(s) '$ids', lab.yaml names $BUNDLE_ID"
  [ "$(node -p "JSON.stringify(require('./$H/FetchLab.xcodeproj/.spm-injected.json').configCommand)")" = '["node","ios-spm/lab-config.js"]' ] \
    || die "$H's injection record does not run ios-spm/lab-config.js: autolinking would read app/ios instead"
  [ -f "$plugin_real/ios/Package.swift" ] || die "the plugin checkout $want_real has no ios/Package.swift: it cannot be built with Swift Package Manager"

  DERIVED="$H/build/DerivedData"
  PRODUCTS="$DERIVED/Build/Products/Debug-iphonesimulator"
  SRCPKG="$H/build/SourcePackages"
  # The label as the package the plugin asks for.  The plugin's ios/Package.swift wants
  # .package(url: "https://github.com/transistorsoft/transistor-background-fetch.git", from: <x>); a SwiftPM mirror
  # maps that URL to a local git repo of the label, tagged with its iOS version, so the plugin's manifest is never
  # edited and its own version requirement is what resolves (MEASURED 2026-11-10: `Checking out 4.2.0`).  The repo
  # is named as the URL's package so the resolved identity is the one the plugin's `package:` names.  Remade, with
  # SwiftPM's checkouts and pins, when it does not hold this label's build (its .lab-source differs) or lacks its tag.
  # (WO-004) One mirror per label, its commit made with fixed dates.  SwiftPM records the revision of every version it
  # resolves from a location (~/Library/org.swift.swiftpm/security/fingerprints, keyed by the mirror's file URL) and
  # refuses that version at another revision later: every label is tagged with its iOS version, so a second label at
  # one mirror path failed with "does not match previously recorded value" (MEASURED 2026-11-11), and so would a remade
  # mirror of the same label, its commit time being new.  A path per label keeps each label's record apart, and the
  # fixed dates keep a mirror remade from the same snapshot and provenance at one revision.  A label re-published with
  # other content (another slice, commit or version) still gets a new revision at its path, which SwiftPM refuses:
  # publish such a build under a new label.
  MIRROR="$H/build/lab-mirror/$native/transistor-background-fetch"
  mirror_source="label=$native commit=$(iprov commit) sha256_sim=$sha_want marketing=$marketing"
  if [ "$(cat "$MIRROR.lab-source" 2>/dev/null)" != "$mirror_source" ] \
     || ! git -C "$MIRROR" rev-parse -q --verify "refs/tags/$marketing" >/dev/null 2>&1; then
    echo "[lab] SwiftPM mirror of $native (tag $marketing)"
    rm -rf "$MIRROR" "$MIRROR.lab-source" "$SRCPKG" "$H/FetchLab.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
    mkdir -p "$MIRROR"
    (cd "$SNAPSHOT" && tar --exclude .swiftpm --exclude .build -cf - .) | tar -x -C "$MIRROR"
    git -C "$MIRROR" init -q
    git -C "$MIRROR" add -A
    GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000" GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000" \
      git -C "$MIRROR" -c user.email=lab@fetch-forge.invalid -c user.name=labkit -c commit.gpgsign=false commit -qm "$mirror_source"
    git -C "$MIRROR" -c tag.gpgsign=false tag "$marketing"
    echo "$mirror_source" > "$MIRROR.lab-source"
  fi
  CFG="$H/FetchLab.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/configuration"
  mkdir -p "$CFG"
  mirrors="$(printf '{"object":[{"mirror":"file://%s","original":"https://github.com/transistorsoft/transistor-background-fetch.git"}],"version":1}' \
    "$(cd "$MIRROR" && pwd -P)")"
  # (WO-004) Another label's mirror in use before: SwiftPM's checkouts and pins name it, so they go too.
  if [ "$(cat "$CFG/mirrors.json" 2>/dev/null)" != "$mirrors" ]; then
    rm -rf "$SRCPKG" "$H/FetchLab.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
  fi
  printf '%s\n' "$mirrors" > "$CFG/mirrors.json"

  # React Native's SwiftPM packages under build/ (gitignored): `update` makes them when they are missing (a fresh
  # clone); otherwise the scheme's "Sync SPM Autolinking" pre-action refreshes autolinking and codegen at every build.
  # Neither may rewrite a tracked file (MEASURED: `update` on the committed host changes none).
  tracked_before="$(tracked_sums "$H" ios)"
  if [ ! -f "$H/build/xcframeworks/Package.swift" ] || [ ! -f "$H/build/generated/autolinking/Package.swift" ]; then
    echo "[lab] setup-apple-spm.js update — $H/build/spm-update.log"
    mkdir -p "$H/build"
    (cd "$H" && LANG=en_US.UTF-8 node ../node_modules/react-native/scripts/setup-apple-spm.js update --yes > build/spm-update.log 2>&1) \
      || { tail -30 "$H/build/spm-update.log"; die "setup-apple-spm.js update failed ($H/build/spm-update.log)"; }
  fi
  rm -f "$PRODUCTS/main.jsbundle"
  echo "[lab] xcodebuild (Swift Package Manager) — $H/build/xcodebuild.log"
  LANG=en_US.UTF-8 xcodebuild -project "$H/FetchLab.xcodeproj" -scheme FetchLab \
    -configuration Debug -sdk iphonesimulator -arch "$(uname -m)" -derivedDataPath "$DERIVED" \
    -clonedSourcePackagesDirPath "$SRCPKG" \
    FORCE_BUNDLING=1 EXTRA_PACKAGER_ARGS="--dev false" build > "$H/build/xcodebuild.log" 2>&1 \
    || { grep -E 'error:|BUILD FAILED' "$H/build/xcodebuild.log" | tail -30 || true; die "xcodebuild failed ($H/build/xcodebuild.log)"; }
  refuse_rewrites "$tracked_before" "the SwiftPM build" "$H" ios
  # Which package SwiftPM resolved: the pin for the plugin's URL is the mirror's tag, at the mirror's commit.
  RESOLVED="$H/FetchLab.xcodeproj/project.xcworkspace/xcshareddata/swiftpm/Package.resolved"
  pin="$(python3 - "$RESOLVED" <<'PY'
import json, sys
pins = [p for p in json.load(open(sys.argv[1])).get("pins", [])
        if p.get("location") == "https://github.com/transistorsoft/transistor-background-fetch.git"]
print(" ".join([pins[0]["identity"], pins[0]["state"].get("version") or "-", pins[0]["state"].get("revision") or "-"])
      if len(pins) == 1 else "none")
PY
)"
  [ "$pin" = "transistor-background-fetch $marketing $(git -C "$MIRROR" rev-parse HEAD)" ] \
    || die "Package.resolved pins transistor-background-fetch as '$pin', not the mirror of '$native' at $marketing"
  SLICE="$PRODUCTS/TSBackgroundFetch.framework"
  source_proof="Package.resolved: transistor-background-fetch $marketing from the mirror of the snapshot"
  ;;

  *) die "LAB_CHANNEL=$CHANNEL: this host builds pods or spm (lab.yaml's build.channels)" ;;
  esac

  # --- prove WHICH native SDK the app links, from the artifact, never from the label -------------------------------
  APP="$PRODUCTS/FetchLab.app"
  [ -d "$APP" ] || die "no $APP after the build"
  [ -f "$SLICE/TSBackgroundFetch" ] || die "the build left no TSBackgroundFetch slice under $SLICE"
  sha_got="$(shasum -a 256 "$SLICE/TSBackgroundFetch" | cut -d' ' -f1)"
  [ "$sha_got" = "$sha_want" ] || die "the slice the $CHANNEL build linked (sha256 $sha_got) is not the one labelled '$native' ($sha_want)"
  mk_got="$($PB -c 'Print CFBundleShortVersionString' "$SLICE/Info.plist")"
  [ "$mk_got" = "$marketing" ] || die "linked TSBackgroundFetch says $mk_got, the provenance for '$native' says $marketing"
  # What is in the executable: the linker keeps the framework's version string.  A Debug build keeps the code in
  # <exe>.debug.dylib beside a launcher stub.  (`grep -q` would SIGPIPE `strings` under pipefail.)
  exe="$($PB -c 'Print CFBundleExecutable' "$APP/Info.plist")"
  bin="$APP/$exe.debug.dylib"; [ -f "$bin" ] || bin="$APP/$exe"
  strs="$(strings - "$bin")"
  [ "$(grep -c "PROJECT:TSBackgroundFetch-$build_no" <<<"$strs")" -gt 0 ] \
    || die "$bin carries no PROJECT:TSBackgroundFetch-$build_no string — the app does not link the '$native' snapshot"
  [ "$(grep -c 'LAB point=legacyFetch' <<<"$strs")" -gt 0 ] \
    || die "$bin has no legacy-fetch forward (AppDelegate.swift's LAB point=legacyFetch) — simctl push would reach nothing"
  id_got="$($PB -c 'Print CFBundleIdentifier' "$APP/Info.plist")"
  [ "$id_got" = "$BUNDLE_ID" ] || die "the built app is $id_got, lab.yaml names $BUNDLE_ID"
  # ... and which JavaScript: the bundle is in the app, and it is the --dev false one.
  [ -f "$APP/main.jsbundle" ] || die "no main.jsbundle in $APP — the app would wait for Metro"
  grep -q '__DEV__=false' "$PRODUCTS/main.jsbundle" || die "$PRODUCTS/main.jsbundle is not a --dev false bundle"
  # (WO-002 phase 2b, bg-forge WO-192 D2) One artifact for the view, whichever channel built it: lab.yaml's ios: app.
  OUT="build/lab-ios/FetchLab.app"
  rm -rf "$OUT" && mkdir -p "$(dirname "$OUT")" && ditto "$APP" "$OUT"
  echo "[lab] built ios channel=$CHANNEL native=$native ($marketing/$build_no, commit $(iprov commit | cut -c1-12) dirty=$(iprov dirty))" \
       "slice=sha256:${sha_got:0:12} ($source_proof) plugin=$want_real app=app/$OUT"
  exit 0
fi

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
