#!/bin/bash
# vendor_check.sh — hold the vendored harness to bg-forge, byte for byte (WO-002, decision D1; labs/VENDORED.md).
#
#   labs/harness/vendor_check.sh            check every row of labs/VENDORED.md
#   BG_FORGE=/path/to/bg-forge labs/harness/vendor_check.sh
#
# For each vendored row: the local file's sha256 is the one recorded, the file is byte-identical to
# `git -C $BG_FORGE show <commit>:<path>` (re-extracted, never a working-tree copy), and its mode is bg-forge's.
# Then no file under labs/harness/ or labs/platforms/_shared/ (or the root labkit) sits at a path bg-forge has at
# that commit unless VENDORED.md lists it: vendored, or fetch-owned on purpose.
#
# Exit 0: identical.  1: a difference (each printed).  2: cannot check (no bg-forge, no such commit, no table).
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
LABS="$(cd "$HERE/.." && pwd)"
REPO="$(cd "$LABS/.." && pwd)"
TABLE="$LABS/VENDORED.md"

if [[ -z "${BG_FORGE:-}" ]]; then
  # Beside the main fetch-forge checkout (a worktree's common dir is the main checkout's .git).
  common="$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
  BG_FORGE="$(cd "$(dirname "${common:-$REPO/.git}")/.." 2>/dev/null && pwd)/bg-forge"
fi
[[ -f "$TABLE" ]] || { echo "vendor_check: no $TABLE" >&2; exit 2; }
git -C "$BG_FORGE" rev-parse --git-dir >/dev/null 2>&1 \
  || { echo "vendor_check: no bg-forge checkout at $BG_FORGE — set BG_FORGE" >&2; exit 2; }

COMMIT="$(sed -n 's/^Commit: `\([0-9a-f]\{40\}\)`.*/\1/p' "$TABLE" | head -1)"
[[ -n "$COMMIT" ]] || { echo "vendor_check: $TABLE names no 40-hex \`Commit:\`" >&2; exit 2; }
git -C "$BG_FORGE" cat-file -e "$COMMIT^{commit}" 2>/dev/null \
  || { echo "vendor_check: bg-forge at $BG_FORGE has no commit $COMMIT (git fetch there)" >&2; exit 2; }

# Rows of the vendored table: | `path` | `sha256` | mode | lines |
ROWS="$(awk '/^## Vendored files/{v=1; next} /^## /{v=0} v && /^\| `/' "$TABLE" \
        | sed -n 's/^| `\([^`]*\)` | `\([0-9a-f]\{64\}\)` | \([0-7]\{6\}\) |.*/\1 \2 \3/p')"
OWNED="$(awk '/^## Fetch-owned files/{v=1; next} /^## /{v=0} v && /^\| `/' "$TABLE" | sed -n 's/^| `\([^`]*\)`.*/\1/p')"
[[ -n "$ROWS" ]] || { echo "vendor_check: no rows under '## Vendored files' in $TABLE" >&2; exit 2; }

tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
bad=0; n=0
while read -r path sha mode; do
  n=$((n + 1))
  local_file="$REPO/$path"
  if [[ ! -f "$local_file" ]]; then echo "MISSING   $path"; bad=1; continue; fi
  if ! git -C "$BG_FORGE" show "$COMMIT:$path" > "$tmp/upstream" 2>/dev/null; then
    echo "UPSTREAM  $path is not in bg-forge at ${COMMIT:0:9}"; bad=1; continue
  fi
  up_mode="$(git -C "$BG_FORGE" ls-tree "$COMMIT" -- "$path" | awk '{print $1}')"
  have="$(shasum -a 256 "$local_file" | cut -d' ' -f1)"
  want="$(shasum -a 256 "$tmp/upstream" | cut -d' ' -f1)"
  if ! cmp -s "$local_file" "$tmp/upstream"; then
    echo "DIFFERS   $path (local $have, bg-forge ${COMMIT:0:9} $want)"; bad=1
  elif [[ "$have" != "$sha" || "$want" != "$sha" ]]; then
    echo "SHA256    $path (VENDORED.md says $sha, the file is $have)"; bad=1
  fi
  if [[ "$mode" != "$up_mode" ]]; then
    echo "MODE      $path (VENDORED.md says $mode, bg-forge has $up_mode)"; bad=1
  fi
  if [[ "$up_mode" == 100755 && ! -x "$local_file" ]] || [[ "$up_mode" == 100644 && -x "$local_file" ]]; then
    echo "MODE      $path (bg-forge has $up_mode; the local executable bit differs)"; bad=1
  fi
done <<< "$ROWS"

# Unlisted files at a vendored path: a copy that skipped the table, or a fetch file that shadows bg-forge's.
listed="$(printf '%s\n%s\n' "$(awk '{print $1}' <<< "$ROWS")" "$OWNED")"
upstream_paths="$(git -C "$BG_FORGE" ls-tree -r --name-only "$COMMIT" -- labs/harness labs/platforms/_shared labkit)"
while read -r rel; do
  [[ -z "$rel" ]] && continue
  if grep -qxF "$rel" <<< "$upstream_paths" && ! grep -qxF "$rel" <<< "$listed"; then
    echo "UNLISTED  $rel is at a bg-forge path but in neither table of VENDORED.md"; bad=1
  fi
done < <(cd "$REPO" && { [[ -e labkit ]] && echo labkit
                         find labs/harness labs/platforms/_shared -type f \
                           -not -path '*/__pycache__/*' -not -path '*/.claims/*' 2>/dev/null; })

if [[ $bad -ne 0 ]]; then
  echo "vendor_check: FAIL — vendored files differ from bg-forge ${COMMIT:0:9} (labs/VENDORED.md: change bg-forge first, then re-vendor)"
  exit 1
fi
echo "vendor_check: PASS — $n vendored files byte-identical to bg-forge ${COMMIT:0:9} ($BG_FORGE)"
