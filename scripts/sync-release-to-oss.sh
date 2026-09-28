#!/usr/bin/env bash
# sync-release-to-oss.sh — mirror a GitHub release's installers to the
# China download site's OSS bucket.
#
# The Chinese website (github.com/zqiren/orbital-website, `/`) serves its
# download buttons from Aliyun OSS (Hong Kong region, no ICP filing needed) and
# reads the version + SHA256 it shows from releases/latest.json there. Nothing
# updates that bucket automatically, so after publishing a release run this
# once (the English page, `/en/`, reads GitHub Releases directly):
#
#   bash scripts/sync-release-to-oss.sh            # latest release
#   bash scripts/sync-release-to-oss.sh v0.14.2    # a specific tag (must be latest)
#   bash scripts/sync-release-to-oss.sh --check    # verify only, uploads nothing
#
# It downloads the versioned installers (Orbital-X.Y.Z-macOS.dmg,
# Orbital-Setup-X.Y.Z.exe), checks each against GitHub's sha256 digest, and
# uploads them under the site's stable names (Orbital-macOS.dmg,
# Orbital-Setup.exe) plus latest.json. It then re-reads the bucket over HTTPS
# and fails unless the public files match the release.
#
# Needs: `gh` logged in, ossutil 2.x configured (~/.ossutilconfig, a RAM
# AccessKey scoped to this bucket), shasum, python3, curl. ~800 MB each way.
set -euo pipefail

REPO="zqiren/Orbital"
BUCKET="oss://orbital-release"
PUBLIC_BASE="https://orbital-release.oss-cn-hongkong.aliyuncs.com"
PREFIX="releases"
MAC_FILE="Orbital-macOS.dmg"
WIN_FILE="Orbital-Setup.exe"

CHECK_ONLY=0
TAG=""
for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) TAG="$arg" ;;
  esac
done

LATEST="$(gh release view --repo "$REPO" --json tagName -q .tagName)"
TAG="${TAG:-$LATEST}"
# The site always advertises "the current version": mirroring an older tag
# would roll Chinese users back while the English page stays on latest.
if [[ "$TAG" != "$LATEST" ]]; then
  echo "Refusing: $TAG is not the latest release ($LATEST). Publish it with --latest first." >&2
  exit 1
fi

# name<TAB>size<TAB>sha256 of the versioned installers on the release.
ASSETS="$(gh release view "$TAG" --repo "$REPO" --json assets \
  -q '.assets[] | [.name, .size, (.digest // "" | sub("^sha256:"; ""))] | @tsv')"
TAB=$'\t'
pick() {  # pick <regex> -> the one versioned asset row whose name matches
  local row
  row="$(grep -E "^$1$TAB" <<< "$ASSETS" || true)"
  if [[ -n "$row" && "$(wc -l <<< "$row")" -eq 1 ]]; then echo "$row"; fi
}
MAC_ROW="$(pick "Orbital-[0-9][^$TAB]*-macOS\\.dmg")"
WIN_ROW="$(pick "Orbital-Setup-[0-9][^$TAB]*\\.exe")"
if [[ -z "$MAC_ROW" || -z "$WIN_ROW" ]]; then
  echo "Release $TAG needs exactly one Orbital-X.Y.Z-macOS.dmg and one Orbital-Setup-X.Y.Z.exe" >&2
  exit 1
fi
IFS="$TAB" read -r MAC_SRC MAC_SIZE MAC_SHA <<< "$MAC_ROW"
IFS="$TAB" read -r WIN_SRC WIN_SIZE WIN_SHA <<< "$WIN_ROW"

verify_public() {  # exit non-zero unless the bucket serves exactly this release
  local ok=0 meta
  meta="$(curl -fsS -m 30 -H 'Cache-Control: no-cache' "$PUBLIC_BASE/$PREFIX/latest.json")" || {
    echo "  latest.json: unreachable" >&2; return 1; }
  python3 - "$meta" "$TAG" "$MAC_SIZE" "$MAC_SHA" "$WIN_SIZE" "$WIN_SHA" << 'PY' || ok=1
import json, sys
meta, tag, mac_size, mac_sha, win_size, win_sha = sys.argv[1:7]
m = json.loads(meta)
want = {"version": tag,
        "mac": (int(mac_size), mac_sha), "win": (int(win_size), win_sha)}
got = {"version": m.get("version"),
       "mac": (m["files"]["mac"]["size"], m["files"]["mac"]["sha256"]),
       "win": (m["files"]["win"]["size"], m["files"]["win"]["sha256"])}
bad = [k for k in want if want[k] != got[k]]
for k in bad:
    print(f"  latest.json {k}: have {got[k]}, want {want[k]}", file=sys.stderr)
if not bad:
    print(f"  latest.json: {tag}, sizes + sha256 match the release")
sys.exit(1 if bad else 0)
PY
  local f want len
  for f in "$MAC_FILE:$MAC_SIZE" "$WIN_FILE:$WIN_SIZE"; do
    want="${f##*:}"; f="${f%%:*}"
    len="$(curl -fsSI -m 30 "$PUBLIC_BASE/$PREFIX/$f" | tr -d '\r' \
      | awk 'tolower($1)=="content-length:"{print $2}' | tail -1)"
    if [[ "$len" == "$want" ]]; then echo "  $f: $len bytes"
    else echo "  $f: serves ${len:-nothing}, want $want bytes" >&2; ok=1; fi
  done
  return "$ok"
}

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  echo "Checking $PUBLIC_BASE/$PREFIX against $TAG"
  verify_public && echo "In sync." && exit 0
  echo "OUT OF SYNC — run: bash scripts/sync-release-to-oss.sh" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "Release $TAG -> $BUCKET/$PREFIX"
gh release download "$TAG" --repo "$REPO" --dir "$WORK" --clobber \
  --pattern "$MAC_SRC" --pattern "$WIN_SRC"
mv "$WORK/$MAC_SRC" "$WORK/$MAC_FILE"
mv "$WORK/$WIN_SRC" "$WORK/$WIN_FILE"

sha_of() { shasum -a 256 "$WORK/$1" | awk '{print $1}'; }
check_sha() {  # check_sha <file> <github digest, may be empty on old releases>
  local have; have="$(sha_of "$1")"
  if [[ -n "$2" && "$have" != "$2" ]]; then
    echo "  $1: downloaded sha256 $have != GitHub digest $2" >&2
    exit 1
  fi
  echo "$have"
}
MAC_SHA="$(check_sha "$MAC_FILE" "$MAC_SHA")"
WIN_SHA="$(check_sha "$WIN_FILE" "$WIN_SHA")"

python3 - "$TAG" "$MAC_SIZE" "$MAC_SHA" "$WIN_SIZE" "$WIN_SHA" > "$WORK/latest.json" << 'PY'
import json, sys, datetime
tag, mac_size, mac_sha, win_size, win_sha = sys.argv[1:6]
print(json.dumps({
    "version": tag,
    "synced_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    "files": {
        "mac": {"name": "Orbital-macOS.dmg", "size": int(mac_size), "sha256": mac_sha},
        "win": {"name": "Orbital-Setup.exe", "size": int(win_size), "sha256": win_sha},
    },
}, indent=2))
PY

# Installers first, metadata last: the page never advertises a version whose
# files aren't there yet. Installers cache 5 min (stable names get overwritten);
# latest.json never caches.
for f in "$MAC_FILE" "$WIN_FILE"; do
  echo "  uploading $f"
  ossutil cp -f "$WORK/$f" "$BUCKET/$PREFIX/$f" \
    --storage-class Standard \
    --cache-control "max-age=300" \
    --content-disposition "attachment"
done
ossutil cp -f "$WORK/latest.json" "$BUCKET/$PREFIX/latest.json" \
  --storage-class Standard \
  --cache-control "no-cache" \
  --content-type "application/json"

echo "Verifying the public bucket…"
verify_public
echo "Done. $TAG is live on the China download site."
