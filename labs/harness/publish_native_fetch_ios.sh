#!/bin/bash
# Build a TSBackgroundFetch.xcframework snapshot, its SIMULATOR slice only, and publish it into this lab's
# xcframeworks/, the only place the lab's iOS apps link it from.
# (WO-002 phase 2) Fetch's counterpart of bg-forge's labs/harness/build_native_ios.sh + publish_native_ios.sh
# (bg-forge 0fe0eb14c): the same xcframeworks/ layout, the same provenance keys, the same ad-hoc signature.  Unlike
# bg-forge's publish, which copies a prebuilt artifact, this builds from NATIVE_DIR itself, as publish_native_fetch.sh
# does for Android, so `commit` is the build's, not an operator's claim about it.
#
# Usage: harness/publish_native_fetch_ios.sh LABEL [NATIVE_DIR]
#   LABEL       the variant's `native:`, e.g. 4.1.1-master-5de5242.  One label names one native commit on both OSes;
#               each OS's provenance records that OS's own version (WO-002 D6: at 5de5242, iOS 4.2.0 / 4200 while
#               Android is 4.1.1).
#   NATIVE_DIR  a native/background-fetch checkout to build (default: this repo's native/background-fetch).  The build
#               writes into a scratch directory under labs/xcframeworks, never into the checkout.
#
# Why not native/background-fetch/scripts/build-ios.sh: that is the RELEASE build.  It also archives for devices and
# signs the xcframework with Transistor's Apple Distribution certificate, which a new machine does not have.  The lab
# runs only on the Simulator, so this is build-ios.sh's Simulator pass alone, with its flags (build-ios.sh:131-139 at
# 5de5242), code signing off for the archive, and an ad-hoc signature after.  If build-ios.sh's Simulator flags
# change, change them here.  The checkout's committed ios/TSBackgroundFetch/TSBackgroundFetch.xcframework is never
# read: it is the last release's binary, not this commit's.
#
# Output, all gitignored:
#   labs/xcframeworks/<label>/TSBackgroundFetch.xcframework   the Simulator slice built from NATIVE_DIR
#   labs/xcframeworks/<label>/TSBackgroundFetch.podspec       the checkout's podspec, its version the slice's marketing
#                                                             version: `pod 'TSBackgroundFetch', :path => …` is the
#                                                             pin, and a plugin's `~> 4.2` resolves (a :path pod
#                                                             ignores `s.source`)
#   labs/xcframeworks/<label>/Package.swift                   the checkout's manifest, its binaryTarget pointed at the
#   labs/xcframeworks/<label>/ios/TSBackgroundFetch/          label's xcframework by path, and its BackgroundFetch
#                             SwiftInterface/                 target's sources at the path the manifest names: what
#                                                             WO-002 2b's SwiftPM channel links
#   labs/xcframeworks/.provenance/<label>.txt                 the checkout (branch, commit, dirty) and what the slice
#                                                             says about itself (marketing, build, sha256_sim)
#
# The app build asserts the built app against the provenance: the slice's sha256, and the PROJECT:TSBackgroundFetch-
# <build> string the linker keeps in the app binary (fetch_pack.Simctl.native_resolved; run.assert_native).
set -euo pipefail

LAB="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$LAB/.." && pwd)"          # labs/ lives at the repo root, so one level, not two
LABEL="${1:?usage: harness/publish_native_fetch_ios.sh LABEL [NATIVE_DIR]}"
NATIVE="$(cd "${2:-$REPO/native/background-fetch}" && pwd)"
PROJECT="$NATIVE/ios/TSBackgroundFetch/TSBackgroundFetch.xcodeproj"
VERSIONS="$NATIVE/versioning/TSBackgroundFetch.properties"
die() { echo "[lab] $*" >&2; exit 1; }

# The same label rule as publish_native_fetch.sh: a directory name, and one fixed build.
[[ "$LABEL" =~ ^[0-9A-Za-z][0-9A-Za-z._+-]*$ ]] || die "label '$LABEL': use letters, digits and . _ + - only"
[[ "$LABEL" != *-SNAPSHOT ]] || die "label '$LABEL': a lab label is a fixed snapshot, not a -SNAPSHOT"
[ -d "$PROJECT" ] && [ -f "$VERSIONS" ] && [ -f "$NATIVE/TSBackgroundFetch.podspec" ] && [ -f "$NATIVE/Package.swift" ] \
  || die "$NATIVE is not a native/background-fetch checkout (no ios/TSBackgroundFetch/TSBackgroundFetch.xcodeproj," \
         "versioning/TSBackgroundFetch.properties, TSBackgroundFetch.podspec and Package.swift)"
MARKETING="$(grep -E '^MARKETING_VERSION=' "$VERSIONS" | cut -d= -f2- | tr -d '\r')"
BUILD="$(grep -E '^CURRENT_PROJECT_VERSION=' "$VERSIONS" | cut -d= -f2- | tr -d '\r')"
[ -n "$MARKETING" ] && [ -n "$BUILD" ] || die "no MARKETING_VERSION / CURRENT_PROJECT_VERSION in $VERSIONS"

echo "native: $NATIVE ($(git -C "$NATIVE" branch --show-current 2>/dev/null) $(git -C "$NATIVE" log --oneline -1 2>/dev/null))"
echo "[native-ios] $MARKETING ($BUILD), label $LABEL"
[[ "$LABEL" == "$MARKETING"* ]] || echo "[native-ios] note: label $LABEL does not start with the iOS version" \
                                        "$MARKETING; the provenance records $MARKETING (WO-002 D6)"

# Read before the build (it describes the source Xcode read), written only after every check below: an old record
# and the label's old files go first, so a failed re-publish leaves the label unpublished, never a record of one
# commit beside the binary of another.
OUT="$LAB/xcframeworks/$LABEL"
PROV="$LAB/xcframeworks/.provenance/$LABEL.txt"
mkdir -p "$LAB/xcframeworks/.provenance"
rm -f "$PROV"
rm -rf "$OUT"
branch="$(git -C "$NATIVE" branch --show-current 2>/dev/null || true)"
commit="$(git -C "$NATIVE" rev-parse HEAD 2>/dev/null || true)"
dirty="$(test -n "$(git -C "$NATIVE" status --porcelain 2>/dev/null)" && echo yes || echo no)"

TMP="$(mktemp -d "$LAB/xcframeworks/.build-$LABEL.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
echo "[native-ios] archiving the simulator slice (derived data in $TMP)"
xcodebuild archive -quiet \
  -project "$PROJECT" -scheme TSBackgroundFetch -configuration Release \
  -destination "generic/platform=iOS Simulator" -archivePath "$TMP/simulator.xcarchive" -sdk iphonesimulator \
  -derivedDataPath "$TMP/DerivedData" \
  SKIP_INSTALL=NO BUILD_LIBRARY_FOR_DISTRIBUTION=YES ENABLE_USER_SCRIPT_SANDBOXING=NO \
  GCC_PREPROCESSOR_DEFINITIONS="\$(inherited) TSL_TAG=$(date -u +%Y%m%d)" \
  MARKETING_VERSION="$MARKETING" CURRENT_PROJECT_VERSION="$BUILD" \
  CODE_SIGNING_ALLOWED=NO
xcodebuild -create-xcframework -output "$TMP/TSBackgroundFetch.xcframework" \
  -framework "$TMP/simulator.xcarchive/Products/Library/Frameworks/TSBackgroundFetch.framework"

# The slice's directory is named by its architectures (ios-arm64_x86_64-simulator, or ios-arm64-simulator where an
# Xcode builds no x86_64): found, not assumed, and exactly one.
slices=("$TMP"/TSBackgroundFetch.xcframework/ios-*-simulator)
[ "${#slices[@]}" -eq 1 ] && [ -d "${slices[0]}" ] || die "expected one simulator slice in the xcframework, found: ${slices[*]}"
SLICE_DIR="$(basename "${slices[0]}")"
FW="${slices[0]}/TSBackgroundFetch.framework"
BIN="$FW/TSBackgroundFetch"
[ -f "$BIN" ] && [ -f "$FW/Info.plist" ] || die "no TSBackgroundFetch binary and Info.plist in $FW"
archs="$(lipo -archs "$BIN")"
[[ " $archs " == *" arm64 "* ]] || die "the simulator slice is $archs, with no arm64"
got_m="$(/usr/libexec/PlistBuddy -c 'Print CFBundleShortVersionString' "$FW/Info.plist")"
got_b="$(/usr/libexec/PlistBuddy -c 'Print CFBundleVersion' "$FW/Info.plist")"
[ "$got_m" = "$MARKETING" ] && [ "$got_b" = "$BUILD" ] \
  || die "the slice says $got_m ($got_b), not the checkout's $MARKETING ($BUILD)"
# The linker keeps this string in every app that links the slice; the app build and run.assert_native look for it.
# (`grep -q` would close the pipe early and SIGPIPE `strings` under pipefail.)
[ "$(strings - "$BIN" | grep -c "PROJECT:TSBackgroundFetch-$BUILD")" -gt 0 ] \
  || die "the simulator slice carries no PROJECT:TSBackgroundFetch-$BUILD string"
for h in TSBackgroundFetch.h TSBGTask.h TSBGAppRefreshSubscriber.h; do      # the lab app's AppDelegate imports these
  [ -f "$FW/Headers/$h" ] || die "the slice has no public $h"
done
sha="$(shasum -a 256 "$BIN" | cut -d' ' -f1)"
codesign --force --sign - "$TMP/TSBackgroundFetch.xcframework"

mkdir -p "$OUT"
cp -R "$TMP/TSBackgroundFetch.xcframework" "$OUT/TSBackgroundFetch.xcframework"

# The podspec, verbatim but for the version.
sed -E "s/^(  s\.version +=[[:space:]]+)'[^']*'/\1'$MARKETING'/" "$NATIVE/TSBackgroundFetch.podspec" > "$OUT/TSBackgroundFetch.podspec"
grep -Eq "^  s\.version +=[[:space:]]+'$MARKETING'" "$OUT/TSBackgroundFetch.podspec" \
  || die "could not stamp the podspec version $MARKETING"
grep -q "s.vendored_frameworks = 'TSBackgroundFetch.xcframework'" "$OUT/TSBackgroundFetch.podspec" \
  || die "the checkout's podspec does not vendor TSBackgroundFetch.xcframework"

# The manifest, verbatim but for the binary target: the release zip and its checksum become the label's own
# xcframework, by path.
perl -0pe 's/url:\s*"[^"]*TSBackgroundFetch\.xcframework\.zip",\s*checksum:\s*"[^"]*"/path: "TSBackgroundFetch.xcframework"/' \
    "$NATIVE/Package.swift" > "$OUT/Package.swift"
grep -q 'path: "TSBackgroundFetch.xcframework"' "$OUT/Package.swift" && ! grep -q 'checksum:' "$OUT/Package.swift" \
  || die "could not point the checkout's Package.swift at the label's xcframework"
SWIFT_REL="ios/TSBackgroundFetch/SwiftInterface"
grep -q "path: \"$SWIFT_REL\"" "$OUT/Package.swift" || die "the checkout's Package.swift has no target at $SWIFT_REL"
[ -f "$NATIVE/$SWIFT_REL/BackgroundFetch.swift" ] || die "no $SWIFT_REL/BackgroundFetch.swift in $NATIVE"
mkdir -p "$OUT/$(dirname "$SWIFT_REL")"
cp -R "$NATIVE/$SWIFT_REL" "$OUT/$SWIFT_REL"
n="$(find "$OUT/$SWIFT_REL" -name '*.swift' | wc -l | tr -d ' ')"
sum="$( (cd "$OUT/$SWIFT_REL" && find . -name '*.swift' | LC_ALL=C sort | xargs shasum -a 256) | shasum -a 256 | cut -d' ' -f1)"

{
  echo "version: $LABEL"
  echo "native:  $NATIVE"
  echo "branch:  $branch"
  echo "commit:  $commit"
  echo "dirty:   $dirty"
  echo "when:    $(date '+%Y-%m-%d %H:%M:%S')"
  echo "built:       from source, by harness/publish_native_fetch_ios.sh"
  echo "slice:       $SLICE_DIR ($archs)"
  echo "marketing:   $MARKETING"
  echo "build:       $BUILD"
  echo "sha256_sim:  $sha"
  echo "swift_api:   $n files sha256 $sum"
} > "$PROV.tmp" && mv "$PROV.tmp" "$PROV"
echo "published TSBackgroundFetch $MARKETING ($BUILD) as $LABEL -> $OUT"
