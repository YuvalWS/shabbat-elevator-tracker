#!/usr/bin/env bash
# Update Shabbat Elevator Tracker in place (keeps config, credentials and data).
#   /opt/shabbat-elevator-tracker/update.sh            # latest main
#   /opt/shabbat-elevator-tracker/update.sh v0.2.0     # a tag
set -euo pipefail
REF="${1:-main}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git clone --quiet --depth 1 --branch "$REF" https://github.com/YuvalWS/shabbat-elevator-tracker.git "$TMP/src"
exec bash "$TMP/src/deploy/lxc/install.sh" --src "$TMP/src"
