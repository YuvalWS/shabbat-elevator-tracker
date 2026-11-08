#!/usr/bin/env bash
# Update Shabbat Elevator Tracker inside its container (config, credentials and data are kept).
# Installed as `update`:
#   update              install the latest main, if it is newer than what runs
#   update v0.2.0       a tag or branch instead of main
#   update --check      only say whether an update is available
#   update --force      reinstall even if up to date
#   update --rollback   switch back to the previous release
#   update --status     show the running release and the ones kept
# A new release that fails its health check is switched back automatically.
set -euo pipefail

APP=shabbat-elevator-tracker
PREFIX=/opt/$APP
REPO="${SET_UPDATE_REPO:-https://github.com/YuvalWS/shabbat-elevator-tracker.git}"
REF=main
MODE=update
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check) MODE=check; shift ;;
    --force) FORCE=1; shift ;;
    --rollback) MODE=rollback; shift ;;
    --status) MODE=status; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1" >&2; exit 2 ;;
    *) REF="$1"; shift ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

CUR=$(readlink -f "$PREFIX/current" 2>/dev/null || true)
field() { sed -n "s/^$1=//p" "$CUR/SOURCE" 2>/dev/null | head -1; }
INSTALLED=$(field commit)

case "$MODE" in
  status)
    echo "running:  $(field version) ($(field commit | cut -c1-7)) from $(field origin), installed $(field installed)"
    echo "releases: $(ls -1 "$PREFIX/releases" 2>/dev/null | tr '\n' ' ')"
    systemctl is-active "$APP" >/dev/null 2>&1 && echo "service:  active" || echo "service:  NOT active"
    exit 0 ;;
  rollback)
    exec bash "$PREFIX/install.sh" --rollback ;;
esac

# the commit REF points to (a branch, or a tag peeled to its commit)
REMOTE=$(git ls-remote "$REPO" "refs/heads/$REF" "refs/tags/$REF" "refs/tags/$REF^{}" 2>/dev/null \
  | awk 'NR==1 {f=$1} /\^\{\}$/ {p=$1} END {print (p ? p : f)}')
[ -n "$REMOTE" ] || { echo "can't find '$REF' in $REPO" >&2; exit 1; }

if [ "$REMOTE" = "$INSTALLED" ] && [ "$FORCE" -eq 0 ]; then
  echo "already up to date: $(field version) ($(echo "$INSTALLED" | cut -c1-7), $REF)"
  exit 0
fi

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git clone --quiet --filter=blob:none --no-checkout "$REPO" "$TMP/src"
git -C "$TMP/src" checkout --quiet -B "$REF" "$REMOTE"

echo "update available: $(echo "${INSTALLED:-none}" | cut -c1-7) -> $(echo "$REMOTE" | cut -c1-7) ($REF)"
if [ -n "$INSTALLED" ] && git -C "$TMP/src" merge-base --is-ancestor "$INSTALLED" "$REMOTE" 2>/dev/null; then
  git -C "$TMP/src" log --format='  %h %s' "$INSTALLED..$REMOTE" | head -20
fi
[ "$MODE" = check ] && exit 0

bash "$TMP/src/deploy/lxc/install.sh" --src "$TMP/src"
