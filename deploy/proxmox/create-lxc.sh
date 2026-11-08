#!/usr/bin/env bash
# Create a Debian 12 LXC container on a Proxmox VE host and install Shabbat Elevator Tracker.
# Run on the Proxmox host as root. With no options it asks (default or advanced settings):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/YuvalWS/shabbat-elevator-tracker/main/deploy/proxmox/create-lxc.sh)"
# Or with options, no questions:
#   ./create-lxc.sh --ctid 230 --ip 10.0.0.50/24 --gw 10.0.0.1
#   ./create-lxc.sh --src /root/shabbat-elevator-tracker.tar.gz   # from a local tarball
#   ./create-lxc.sh --dry-run                                     # print the commands only
# Updates: `update` inside the container, or `pct exec <ctid> -- update` on the host.
# Running this script again with an existing --ctid reinstalls the app in it.
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
DISK=3
REF="main"
SRC=""
PORT=8080
DRY=0
RAW="https://raw.githubusercontent.com/YuvalWS/shabbat-elevator-tracker"

usage() { cat <<EOF
Create a Debian 12 LXC container and install Shabbat Elevator Tracker (run on the Proxmox host).
With no options it asks; with options it doesn't.
Options:
  --ctid N            container id (default: next free)
  --hostname NAME     (default: $HOSTNAME_)
  --storage NAME      rootfs storage (default: $STORAGE)
  --template-storage  storage for the Debian template (default: $TEMPLATE_STORAGE)
  --bridge NAME       (default: $BRIDGE)
  --ip CIDR|dhcp      (default: dhcp)   --gw ADDRESS   gateway for a static ip
  --cores N (1)  --memory MB (256)  --swap MB (256)  --disk GB (3)
  --ref REF           git tag/branch to install (default: main)
  --src PATH          install from a local source tarball/directory instead of GitHub
  --port N            web/API port (default: 8080)
  --dry-run           print what would be done
EOF
}

ASK=0
if [ $# -eq 0 ] && [ -t 0 ] && command -v whiptail >/dev/null 2>&1; then ASK=1; fi

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

if [ -z "$CTID" ]; then CTID="$(pvesh get /cluster/nextid)"; fi

# ------------------------------------------------------------------ questions
TITLE="Shabbat Elevator Tracker"
ask() {  # ask VAR "question" -> sets VAR, exits on Cancel
  local v
  v=$(whiptail --title "$TITLE" --inputbox "$2" 9 64 "${!1}" 3>&1 1>&2 2>&3) || { echo "cancelled"; exit 1; }
  printf -v "$1" '%s' "$v"
}
if [ "$ASK" -eq 1 ]; then
  CHOICE=$(whiptail --title "$TITLE" --menu "Create an LXC container for the tracker" 12 64 2 \
    default "Default settings (DHCP, next free id $CTID)" \
    advanced "Advanced settings" 3>&1 1>&2 2>&3) || { echo "cancelled"; exit 1; }
  if [ "$CHOICE" = advanced ]; then
    ask CTID "Container id"
    ask HOSTNAME_ "Hostname"
    ask CORES "CPU cores"
    ask MEMORY "Memory (MB)"
    ask DISK "Disk size (GB)"
    ask STORAGE "Storage for the container disk"
    ask BRIDGE "Network bridge"
    ask IP "IPv4 address with prefix (e.g. 192.168.1.50/24), or dhcp"
    if [ "$IP" != "dhcp" ]; then ask GW "Gateway"; fi
    ask PORT "Web/API port"
  fi
  whiptail --title "$TITLE" --yesno "Create container $CTID?

  hostname  $HOSTNAME_
  cpu/mem   $CORES core(s), $MEMORY MB
  disk      $DISK GB on $STORAGE
  network   $BRIDGE, $IP${GW:+ via $GW}
  port      $PORT
  source    ${SRC:-GitHub $REF}" 16 64 || { echo "cancelled"; exit 1; }
fi

# install.sh: next to this script when it runs from a checkout, else from GitHub
SELF="${BASH_SOURCE[0]:-}"
INSTALL_SH=""
if [ -n "$SELF" ] && [ -f "$SELF" ]; then
  INSTALL_SH="$(cd "$(dirname "$SELF")" && pwd)/../lxc/install.sh"
  [ -f "$INSTALL_SH" ] || INSTALL_SH=""
fi
if [ -z "$INSTALL_SH" ]; then
  INSTALL_SH="$(mktemp)"
  run curl -fsSL -o "$INSTALL_SH" "$RAW/$REF/deploy/lxc/install.sh"
fi

if pct status "$CTID" >/dev/null 2>&1; then
  echo "==> container $CTID exists: reinstalling the app inside it"
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
    --onboot 1 --timezone host --tags shabbat-elevator \
    --description "Shabbat Elevator Tracker - https://github.com/YuvalWS/shabbat-elevator-tracker
Update: run \`update\` in the container console."
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
run pct exec "$CTID" -- rm -f /root/install.sh /root/src.tar.gz

if [ "$DRY" -eq 0 ]; then
  ADDR="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
  cat <<EOF

Done. Container $CTID ($HOSTNAME_) is running.
  Web UI / API:   http://${ADDR}:${PORT}/
  Config:         pct exec $CTID -- nano /etc/shabbat-elevator-tracker/config.yaml
  Credentials:    pct exec $CTID -- nano /etc/shabbat-elevator-tracker/env
  Restart:        pct exec $CTID -- systemctl restart shabbat-elevator-tracker
  Logs:           pct exec $CTID -- journalctl -u shabbat-elevator-tracker -f
  Update:         pct exec $CTID -- update        (or \`update\` in the container console)
  Roll back:      pct exec $CTID -- update --rollback
EOF
fi
