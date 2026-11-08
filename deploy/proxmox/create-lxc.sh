#!/usr/bin/env bash
# Create a Debian 12 LXC container on a Proxmox VE host and install Shabbat Elevator Tracker.
# Run on the Proxmox host as root. Examples:
#   ./create-lxc.sh                                   # DHCP, next free CT id
#   ./create-lxc.sh --ctid 230 --ip 10.0.0.50/24 --gw 10.0.0.1
#   ./create-lxc.sh --src /root/shabbat-elevator-tracker.tar.gz   # install from a local tarball
#   ./create-lxc.sh --dry-run                         # print the commands only
# If the container already exists (same id), the app is (re)installed in it: an update.
set -euo pipefail

CTID=""
HOSTNAME_="shabbat-elevator"
STORAGE="local-lvm"
TEMPLATE_STORAGE="local"
BRIDGE="vmbr0"
IP="dhcp"
GW=""
CORES=1
MEMORY=256
SWAP=256
DISK=2
REF="main"
SRC=""
PORT=8080
DRY=0

usage() { sed -n '2,10p' "$0"; cat <<EOF
Options:
  --ctid N            container id (default: next free)
  --hostname NAME     (default: $HOSTNAME_)
  --storage NAME      rootfs storage (default: $STORAGE)
  --template-storage  storage for the Debian template (default: $TEMPLATE_STORAGE)
  --bridge NAME       (default: $BRIDGE)
  --ip CIDR|dhcp      (default: dhcp)   --gw ADDRESS   gateway for a static ip
  --cores N (1)  --memory MB (256)  --swap MB (256)  --disk GB (2)
  --ref REF           git tag/branch to install (default: main)
  --src PATH          install from a local source tarball/directory instead of GitHub
  --port N            web/API port (default: 8080)
  --dry-run           print what would be done
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --ctid) CTID="$2"; shift 2 ;;
    --hostname) HOSTNAME_="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --template-storage) TEMPLATE_STORAGE="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --ip) IP="$2"; shift 2 ;;
    --gw) GW="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --swap) SWAP="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --src) SRC="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage; exit 2 ;;
  esac
done

run() {
  echo "+ $*"
  if [ "$DRY" -eq 0 ]; then "$@"; fi
}

need() { command -v "$1" >/dev/null 2>&1 || { echo "this must run on a Proxmox VE host ($1 not found)" >&2; exit 1; }; }
need pct; need pveam; need pvesh
[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_SH="$HERE/../lxc/install.sh"
if [ ! -f "$INSTALL_SH" ]; then
  # running the script on its own: fetch install.sh from GitHub
  INSTALL_SH="$(mktemp)"
  run curl -fsSL -o "$INSTALL_SH" "https://raw.githubusercontent.com/YuvalWS/shabbat-elevator-tracker/$REF/deploy/lxc/install.sh"
fi

if [ -z "$CTID" ]; then CTID="$(pvesh get /cluster/nextid)"; fi

if pct status "$CTID" >/dev/null 2>&1; then
  echo "==> container $CTID exists: updating the app inside it"
  EXISTS=1
else
  EXISTS=0
fi

if [ "$EXISTS" -eq 0 ]; then
  echo "==> Debian 12 template"
  run pveam update >/dev/null
  TEMPLATE="$(pveam available --section system | awk '{print $2}' | grep -E '^debian-12-standard_.*_amd64\.tar\.zst$|^debian-12-standard_.*_arm64\.tar\.zst$' | sort -V | tail -1)"
  [ -n "$TEMPLATE" ] || { echo "no debian-12-standard template available" >&2; exit 1; }
  if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
    run pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
  fi

  NET="name=eth0,bridge=$BRIDGE,ip=$IP"
  if [ "$IP" != "dhcp" ] && [ -n "$GW" ]; then NET="$NET,gw=$GW"; fi

  echo "==> creating container $CTID"
  run pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
    --hostname "$HOSTNAME_" --cores "$CORES" --memory "$MEMORY" --swap "$SWAP" \
    --rootfs "$STORAGE:$DISK" --net0 "$NET" --unprivileged 1 --features nesting=1 \
    --onboot 1 --timezone host --description "Shabbat Elevator Tracker"
  run pct start "$CTID"
fi

echo "==> waiting for the network"
if [ "$DRY" -eq 0 ]; then
  for _ in $(seq 1 60); do
    pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
    sleep 2
  done
fi

echo "==> installing the app"
run pct push "$CTID" "$INSTALL_SH" /root/install.sh --perms 755
ARGS=(--port "$PORT")
if [ -n "$SRC" ]; then
  if [ -d "$SRC" ]; then
    TARBALL="$(mktemp --suffix=.tar.gz)"
    run tar -czf "$TARBALL" -C "$SRC" --exclude=./.git --exclude=./test-data --exclude=./data .
  else
    TARBALL="$SRC"
  fi
  run pct push "$CTID" "$TARBALL" /root/src.tar.gz
  ARGS+=(--src /root/src.tar.gz)
else
  ARGS+=(--ref "$REF")
fi
run pct exec "$CTID" -- bash /root/install.sh "${ARGS[@]}"

if [ "$DRY" -eq 0 ]; then
  ADDR="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
  cat <<EOF

Done. Container $CTID ($HOSTNAME_) is running.
  Web UI / API:   http://${ADDR}:${PORT}/
  Config:         pct exec $CTID -- nano /etc/shabbat-elevator-tracker/config.yaml
  Credentials:    pct exec $CTID -- nano /etc/shabbat-elevator-tracker/env
  Restart:        pct exec $CTID -- systemctl restart shabbat-elevator-tracker
  Logs:           pct exec $CTID -- journalctl -u shabbat-elevator-tracker -f
EOF
fi
