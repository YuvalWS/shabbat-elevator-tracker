#!/usr/bin/env bash
# Install or update Shabbat Elevator Tracker on Debian 12 / Ubuntu 22.04+ (LXC, VM or host).
# Run as root inside the container:
#   ./install.sh                       # latest from GitHub (main)
#   ./install.sh --ref v0.1.0          # a tag or branch
#   ./install.sh --src /root/src.tar.gz   # a local source tarball or directory
#   ./install.sh --no-service          # install only (no systemd), e.g. for testing
# Re-running updates the code and keeps the config, credentials and data.
set -euo pipefail

REPO="https://github.com/YuvalWS/shabbat-elevator-tracker.git"
REF="main"
SRC=""
PORT="8080"
SERVICE=1
APP=shabbat-elevator-tracker
USER_NAME=shabbat-elevator
PREFIX=/opt/$APP
CONF_DIR=/etc/$APP
DATA_DIR=/var/lib/$APP

while [ $# -gt 0 ]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --src) SRC="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
export DEBIAN_FRONTEND=noninteractive

echo "==> installing packages"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg git ca-certificates tzdata curl >/dev/null

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "Python >= 3.11 required (found $PYV)" >&2; exit 1; }

echo "==> user and directories"
id -u "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$USER_NAME"
install -d -m 0755 "$PREFIX" "$CONF_DIR"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" "$DATA_DIR"

echo "==> fetching source"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
if [ -n "$SRC" ]; then
  if [ -d "$SRC" ]; then
    cp -a "$SRC" "$WORK/src"
  else
    mkdir "$WORK/x"
    tar -xzf "$SRC" -C "$WORK/x"
    if [ -f "$WORK/x/pyproject.toml" ]; then
      mv "$WORK/x" "$WORK/src"
    else
      # a tarball with one top-level directory (e.g. a GitHub archive)
      mv "$WORK/x/"*/ "$WORK/src"
    fi
  fi
else
  git clone --quiet --depth 1 --branch "$REF" "$REPO" "$WORK/src"
fi
[ -f "$WORK/src/pyproject.toml" ] || { echo "no pyproject.toml in the source" >&2; exit 1; }

echo "==> python environment"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$WORK/src"
install -m 0644 "$WORK/src/config.example.yaml" "$CONF_DIR/config.example.yaml"
install -m 0755 "$WORK/src/deploy/lxc/update.sh" "$PREFIX/update.sh" 2>/dev/null || true

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

if [ "$SERVICE" -eq 0 ]; then
  echo "==> installed (no service). Run:"
  echo "    sudo -u $USER_NAME env SET_CONFIG=$CONF_DIR/config.yaml $PREFIX/venv/bin/python -m shabbat_elevator_tracker serve"
  exit 0
fi

echo "==> systemd service"
install -m 0644 "$WORK/src/deploy/lxc/$APP.service" "/etc/systemd/system/$APP.service"
systemctl daemon-reload
systemctl enable --quiet "$APP"
systemctl restart "$APP"

sleep 3
if systemctl is-active --quiet "$APP"; then
  IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  echo "==> running: http://${IP:-localhost}:$PORT/"
else
  echo "==> the service did not start; see: journalctl -u $APP -n 50" >&2
  exit 1
fi
