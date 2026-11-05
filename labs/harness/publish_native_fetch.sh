#!/bin/bash
# Publish a tsbackgroundfetch snapshot into this lab's m2/, the only repository the lab apps resolve it from.
# (WO-002) Fetch's counterpart of bg-forge's labs/harness/publish_native.sh (the vendored commit is in
# labs/VENDORED.md): the same m2/ layout, the same -I lab-publish.init.gradle, the same provenance keys.
#
# Usage: harness/publish_native_fetch.sh LABEL [NATIVE_DIR]
#   LABEL       snapshot version, e.g. 4.1.1-master-5de5242
#   NATIVE_DIR  a native/background-fetch checkout to build (default: this repo's native/background-fetch).
#               Gradle finds the Android SDK through its android/local.properties, else ANDROID_HOME /
#               ANDROID_SDK_ROOT, else ~/Library/Android/sdk.  Nothing is written into the checkout.
#
# Then an app selects it (React Native: react-native-background-fetch/android/build.gradle reads
# safeExtGet('backgroundFetchVersion', '4.1.+')), in the app's android/build.gradle:
#   buildscript { ext { backgroundFetchVersion = "LABEL" } }
#   allprojects { repositories { maven { url "<labs>/m2" } } }
set -euo pipefail

LAB="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$LAB/.." && pwd)"          # labs/ lives at the repo root, so one level, not two
LABEL="${1:?usage: harness/publish_native_fetch.sh LABEL [NATIVE_DIR]}"
NATIVE="$(cd "${2:-$REPO/native/background-fetch}" && pwd)"
B="$NATIVE/android"
die() { echo "[lab] $*" >&2; exit 1; }

# The label is a Maven version and a file name.  -SNAPSHOT is refused: Gradle treats such a version as changing,
# and a label must name one build.
[[ "$LABEL" =~ ^[0-9A-Za-z][0-9A-Za-z._+-]*$ ]] || die "label '$LABEL': use letters, digits and . _ + - only"
[[ "$LABEL" != *-SNAPSHOT ]] || die "label '$LABEL': a lab label is a fixed snapshot, not a -SNAPSHOT"
grep -q "tsbackgroundfetch" "$B/settings.gradle" 2>/dev/null \
  || die "$NATIVE is not a native/background-fetch checkout (no :tsbackgroundfetch in android/settings.gradle)"

if [ ! -f "$B/local.properties" ] && [ -z "${ANDROID_HOME:-}" ] && [ -z "${ANDROID_SDK_ROOT:-}" ]; then
  export ANDROID_HOME="$HOME/Library/Android/sdk"
  [ -d "$ANDROID_HOME" ] || die "no Android SDK: set ANDROID_HOME, or sdk.dir in $B/local.properties"
fi

echo "native: $NATIVE ($(git -C "$NATIVE" branch --show-current 2>/dev/null) $(git -C "$NATIVE" log --oneline -1 2>/dev/null))"

# Record WHICH native source this snapshot is built from.  A capture names the snapshot by label; without this,
# nothing ties the label to a commit, and a re-publish under the same label silently changes what every earlier
# PASS meant.  Read before the build (it describes the source Gradle read), written only after the published POM
# is verified: an old record is removed first, so a failed re-publish leaves the label unpublished, never a
# record of one commit beside the artifacts of another.  The label's old artifacts go too (a signed publish's .asc
# files would otherwise outlive an unsigned re-publish).
PROV="$LAB/m2/.provenance/$LABEL.txt"
DIR="$LAB/m2/com/transistorsoft/tsbackgroundfetch/$LABEL"
mkdir -p "$LAB/m2/.provenance"
rm -f "$PROV"
rm -rf "$DIR"
branch="$(git -C "$NATIVE" branch --show-current 2>/dev/null || true)"
commit="$(git -C "$NATIVE" rev-parse HEAD 2>/dev/null || true)"
dirty="$(test -n "$(git -C "$NATIVE" status --porcelain 2>/dev/null)" && echo yes || echo no)"

# -I lab-publish.init.gradle (vendored from bg-forge) makes publication signing optional, so a machine without
# Transistor's GPG key, keystore or Sonatype properties publishes here: it signs where a key is set up and skips the
# signature, not fails, where none is.  Nothing in labs/m2 is ever verified against a signature.
# The label overrides android/versioning/tsbackgroundfetch.properties only as a COMMAND-LINE -P:
# tsbackgroundfetch/build.gradle loads the properties file first, then lets gradle.startParameter's -P win.
"$B/gradlew" -p "$B" -I "$LAB/harness/lab-publish.init.gradle" :tsbackgroundfetch:publishToMavenLocal \
    -Dmaven.repo.local="$LAB/m2" \
    -PTS_BACKGROUND_FETCH_VERSION_NAME="$LABEL"

# Prove the label took, from what was published.  A checkout whose build.gradle ignores the -P publishes its
# properties-file version instead, and an app pinned to the label would then not resolve at all.
POM="$DIR/tsbackgroundfetch-$LABEL.pom"
[ -s "$POM" ] && [ -s "$DIR/tsbackgroundfetch-$LABEL.aar" ] \
  || die "no tsbackgroundfetch-$LABEL.pom/.aar under $DIR — did the checkout's build.gradle ignore -PTS_BACKGROUND_FETCH_VERSION_NAME?"
grep -q "<version>$LABEL</version>" "$POM" || die "$POM does not declare version $LABEL"

{
  echo "version: $LABEL"
  echo "native:  $NATIVE"
  echo "branch:  $branch"
  echo "commit:  $commit"
  echo "dirty:   $dirty"
  echo "when:    $(date '+%Y-%m-%d %H:%M:%S')"
} > "$PROV.tmp" && mv "$PROV.tmp" "$PROV"
echo "published com.transistorsoft:tsbackgroundfetch:$LABEL -> $LAB/m2"
