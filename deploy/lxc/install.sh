#!/usr/bin/env bash
# Install or update Shabbat Elevator Tracker on Debian 12 / Ubuntu 22.04+ (LXC, VM or host).
# Run as root inside the container:
#   ./install.sh                          # latest main from GitHub
#   ./install.sh --ref v0.2.0             # a tag or branch
#   ./install.sh --src /root/src.tar.gz   # a local source tarball or directory
#   ./install.sh --no-service             # install only (no systemd), e.g. for testing
#   ./install.sh --rollback               # switch back to the previous release
# Every run installs a new release next to the running one, switches to it, and switches
# back if the service does not answer its health check. Config, credentials and data are
# kept. Afterwards, `update` (inside the container) installs the latest main.
set -euo pipefail

REPO="${SET_UPDATE_REPO:-https://github.com/YuvalWS/shabbat-elevator-tracker.git}"
REF="main"
SRC=""
PORT="8080"
SERVICE=1
ROLLBACK=0
APP=shabbat-elevator-tracker
USER_NAME=shabbat-elevator
PREFIX=/opt/$APP
CONF_DIR=/etc/$APP
DATA_DIR=/var/lib/$APP
RELEASES=$PREFIX/releases
UNIT=/etc/systemd/system/$APP.service

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --src) SRC="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

# ------------------------------------------------------------------ helpers
current() { readlink -e "$PREFIX/current" 2>/dev/null || true; }

# the newest release that sorts before $1 (release names start with a timestamp)
previous_of() {
  local cur="$1" r best=""
  for r in "$RELEASES"/*/; do
    r="${r%/}"
    [ -d "$r" ] || continue
    if [ -z "$cur" ] || [[ "$(basename "$r")" < "$(basename "$cur")" ]]; then best="$r"; fi
  done
  printf '%s' "$best"
}

field() { sed -n "s/^$1=//p" "$2/SOURCE" 2>/dev/null | head -1; }

describe() { echo "$(field version "$1") ($(field commit "$1" | cut -c1-7), $(basename "$1"))"; }

switch_to() {
  ln -sfn "$1" "$PREFIX/current.new"
  mv -Tf "$PREFIX/current.new" "$PREFIX/current"
}

health_port() {
  local p
  p=$(sed -n 's/^SET_CFG__server__port=//p' "$CONF_DIR/env" 2>/dev/null | tail -1)
  [ -n "$p" ] || p=$("$PREFIX/current/venv/bin/python" - "$CONF_DIR/config.yaml" <<'PY' 2>/dev/null || true
import sys, yaml
try:
    c = yaml.safe_load(open(sys.argv[1])) or {}
except Exception:
    c = {}
print((c.get("server") or {}).get("port") or "")
PY
)
  echo "${p:-8080}"
}

healthy() {
  local port _
  port=$(health_port)
  for _ in $(seq 1 30); do
    curl -fsS --max-time 2 "http://127.0.0.1:$port/api/v1/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# point `current` at release $1, install its unit, restart, wait for the health check
activate() {
  local rel="$1"
  switch_to "$rel"
  [ "$SERVICE" -eq 1 ] || return 0
  [ -f "$rel/$APP.service" ] && install -m 0644 "$rel/$APP.service" "$UNIT"
  systemctl daemon-reload
  systemctl enable --quiet "$APP"
  systemctl restart "$APP"
  healthy
}

# keep the current release and the one before it
prune() {
  local cur prev r
  cur=$(current)
  prev=$(previous_of "$cur")
  for r in "$RELEASES"/*/; do
    r="${r%/}"
    [ -d "$r" ] || continue
    [ "$r" = "$cur" ] || [ "$r" = "$prev" ] || rm -rf "$r"
  done
  # layout before releases: a single venv in $PREFIX
  [ -d "$PREFIX/venv" ] && rm -rf "$PREFIX/venv"
  return 0
}

# ------------------------------------------------------------------ rollback
if [ "$ROLLBACK" -eq 1 ]; then
  CUR=$(current)
  PREV=$(previous_of "$CUR")
  [ -n "$PREV" ] || { echo "no previous release to go back to" >&2; exit 1; }
  echo "==> switching from $(describe "$CUR") to $(describe "$PREV")"
  if activate "$PREV"; then
    echo "==> rolled back; running $(describe "$PREV")"
  else
    echo "==> the previous release is not healthy either; see: journalctl -u $APP -n 50" >&2
    exit 1
  fi
  exit 0
fi

# ------------------------------------------------------------------ install
echo "==> packages"
PKGS="python3 python3-venv python3-pip ffmpeg git ca-certificates tzdata curl"
# shellcheck disable=SC2086
if ! dpkg -s $PKGS >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq $PKGS >/dev/null
fi

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python >= 3.11 required (found $PYV)" >&2; exit 1; }

echo "==> user and directories"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$USER_NAME"
install -d -m 0755 "$PREFIX" "$RELEASES" "$CONF_DIR"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" "$DATA_DIR"

echo "==> fetching source"
WORK=$(mktemp -d)
REL=""
cleanup() {
  rm -rf "$WORK"
  # a release that never became current is removed
  if [ -n "$REL" ] && [ -d "$REL" ] && [ "$(current)" != "$REL" ]; then rm -rf "$REL"; fi
}
trap cleanup EXIT
COMMIT=""
ORIGIN="local"
if [ -n "$SRC" ]; then
  if [ -d "$SRC" ]; then
    cp -a "$SRC" "$WORK/src"
    if git -C "$SRC" rev-parse HEAD >/dev/null 2>&1; then
      COMMIT=$(git -C "$SRC" rev-parse HEAD)
      ORIGIN="$(git -C "$SRC" remote get-url origin 2>/dev/null || echo local)#$(git -C "$SRC" rev-parse --abbrev-ref HEAD)"
    fi
  else
    mkdir "$WORK/x"
    tar -xzf "$SRC" -C "$WORK/x"
    # `git archive` tarballs carry their commit id
    COMMIT=$(gzip -dc "$SRC" 2>/dev/null | git get-tar-commit-id 2>/dev/null || true)
    if [ -f "$WORK/x/pyproject.toml" ]; then
      mv "$WORK/x" "$WORK/src"
    else
      # a tarball with one top-level directory (e.g. a GitHub archive)
      mv "$WORK/x/"*/ "$WORK/src"
    fi
  fi
else
  git clone --quiet --depth 1 --branch "$REF" "$REPO" "$WORK/src"
  COMMIT=$(git -C "$WORK/src" rev-parse HEAD)
  ORIGIN="$REPO#$REF"
fi
[ -f "$WORK/src/pyproject.toml" ] || { echo "no pyproject.toml in the source" >&2; exit 1; }

REL="$RELEASES/$(date +%Y%m%d-%H%M%S)-${COMMIT:0:7}"
[ -n "$COMMIT" ] || REL="${REL}local"
echo "==> building release $(basename "$REL")"
mkdir -p "$REL"
python3 -m venv "$REL/venv"
"$REL/venv/bin/pip" install --quiet --disable-pip-version-check --upgrade pip
"$REL/venv/bin/pip" install --quiet --disable-pip-version-check "$WORK/src"
install -m 0644 "$WORK/src/deploy/lxc/$APP.service" "$REL/$APP.service"
cat > "$REL/SOURCE" <<EOF
version=$("$REL/venv/bin/python" -c 'import shabbat_elevator_tracker as m; print(m.__version__)')
commit=$COMMIT
origin=$ORIGIN
installed=$(date -Is)
EOF

# tools that run outside a release
install -m 0644 "$WORK/src/config.example.yaml" "$CONF_DIR/config.example.yaml"
install -m 0755 "$WORK/src/deploy/lxc/install.sh" "$PREFIX/install.sh"
install -m 0755 "$WORK/src/deploy/lxc/update.sh" "$PREFIX/update.sh"
ln -sfn "$PREFIX/update.sh" /usr/bin/update   # /usr/bin: `pct exec` with a cleared environment searches only /bin:/usr/bin
[ "$(readlink /usr/local/bin/update 2>/dev/null)" = "$PREFIX/update.sh" ] && rm -f /usr/local/bin/update
install -m 0644 "$WORK/src/deploy/lxc/motd.sh" /etc/profile.d/$APP.sh

echo "==> configuration"
if [ ! -f "$CONF_DIR/config.yaml" ]; then
  sed -e "s#data_dir: \"/data\"#data_dir: \"$DATA_DIR\"#" -e "s#port: 8080#port: $PORT#" \
    "$CONF_DIR/config.example.yaml" > "$CONF_DIR/config.yaml"
  echo "    created $CONF_DIR/config.yaml - set audio.source"
fi
if [ ! -f "$CONF_DIR/env" ]; then
  cat > "$CONF_DIR/env" <<'EOF'
# Environment for shabbat-elevator-tracker (credentials live here, not in config.yaml).
# SET_AUDIO_SOURCE=rtsp://frigate.local:8554/doorbell_audio
# SET_AUTH_TYPE=basic
# SET_STREAM_USERNAME=
# SET_STREAM_PASSWORD=
EOF
fi
chown root:"$USER_NAME" "$CONF_DIR/env" "$CONF_DIR/config.yaml"
chmod 0640 "$CONF_DIR/env" "$CONF_DIR/config.yaml"

PREV=$(current)
if [ "$SERVICE" -eq 0 ]; then
  switch_to "$REL"
  prune
  echo "==> installed $(describe "$REL") (no service). Run:"
  echo "    sudo -u $USER_NAME env SET_CONFIG=$CONF_DIR/config.yaml $PREFIX/current/venv/bin/python -m shabbat_elevator_tracker serve"
  exit 0
fi

echo "==> starting $(describe "$REL")"
if activate "$REL"; then
  prune
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "==> running $(describe "$REL"): http://${IP:-localhost}:$(health_port)/"
  exit 0
fi

echo "==> the new release did not pass the health check:" >&2
journalctl -u "$APP" -n 20 --no-pager >&2 || true
if [ -n "$PREV" ] && [ -d "$PREV" ]; then
  echo "==> switching back to $(describe "$PREV")" >&2
  if activate "$PREV"; then
    echo "==> rolled back; still running $(describe "$PREV")" >&2
  else
    echo "==> the previous release does not start either; see: journalctl -u $APP -n 50" >&2
  fi
  rm -rf "$REL"
else
  echo "==> see: journalctl -u $APP -n 50" >&2
fi
exit 1
