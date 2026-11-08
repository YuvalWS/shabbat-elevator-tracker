# Running on Proxmox (LXC)

The tracker runs well in a small unprivileged Debian 12 container: 1 core, 256 MB RAM,
2 GB disk. One script on the Proxmox host creates the container and installs everything.

## 1. Get the scripts onto the Proxmox host

Open a shell on the Proxmox host (web UI → node → Shell, or `ssh root@proxmox`).

Once the repository is public:

```bash
curl -fsSLO https://raw.githubusercontent.com/YuvalWS/shabbat-elevator-tracker/main/deploy/proxmox/create-lxc.sh
chmod +x create-lxc.sh
```

Before it is public (or to install your own working copy), copy the source over from
your PC and install from it:

```bash
# on your PC, in the repository folder (test-data/ and data/ are excluded):
tar -czf shabbat-elevator-tracker.tar.gz --exclude=./.git --exclude=./test-data --exclude=./data .
scp shabbat-elevator-tracker.tar.gz root@proxmox:/root/
# on the Proxmox host:
mkdir -p /root/set && tar -xzf /root/shabbat-elevator-tracker.tar.gz -C /root/set
```

## 2. Create the container

```bash
# from GitHub, DHCP, next free container id:
./create-lxc.sh
# or from the copied source, with a fixed address:
/root/set/deploy/proxmox/create-lxc.sh --src /root/shabbat-elevator-tracker.tar.gz \
    --ctid 230 --ip 192.168.1.50/24 --gw 192.168.1.1
```

Options (all optional): `--ctid`, `--hostname` (shabbat-elevator), `--storage`
(local-lvm), `--template-storage` (local), `--bridge` (vmbr0), `--ip` (dhcp), `--gw`,
`--cores` (1), `--memory` (256), `--disk` (2), `--port` (8080), `--ref` (git tag/branch),
`--src` (local tarball or directory), `--dry-run` (print the commands only).

The script downloads the Debian 12 template if needed, creates an unprivileged container
that starts on boot and uses the host's time zone, installs Python, ffmpeg and the app
(in `/opt/shabbat-elevator-tracker`), creates a systemd service, and prints the URL.

Running it again with the same `--ctid` updates the app inside the existing container.

## 3. Configure

Two files inside the container:

- `/etc/shabbat-elevator-tracker/config.yaml` - building, route, timings, audio source,
  calendar (started from `config.example.yaml`, which is the reference building with the
  location set to Givat Shmuel).
- `/etc/shabbat-elevator-tracker/env` - stream credentials (readable only by root and the
  service).

```bash
pct exec 230 -- nano /etc/shabbat-elevator-tracker/config.yaml
pct exec 230 -- nano /etc/shabbat-elevator-tracker/env
pct exec 230 -- systemctl restart shabbat-elevator-tracker
```

Example for Frigate's go2rtc restream with RTSP credentials. In the Frigate config:

```yaml
go2rtc:
  rtsp:
    username: elevator
    password: "{FRIGATE_RTSP_PASSWORD}"
```

In the tracker's `config.yaml`:

```yaml
audio:
  source: "rtsp://frigate.local:8554/front_door_audio"
  auth:
    type: basic
```

and in `/etc/shabbat-elevator-tracker/env`:

```bash
SET_STREAM_USERNAME=elevator
SET_STREAM_PASSWORD=the-password
```

## 4. Check it

```bash
pct exec 230 -- journalctl -u shabbat-elevator-tracker -f     # logs
curl http://192.168.1.50:8080/api/v1/health                   # from your LAN
```

Open `http://192.168.1.50:8080/` and choose your floor. "Audio: live" means the stream
works; the first sync needs two elevator stops at the microphone floor (about 7 minutes
while the Shabbat elevator runs). Outside Shabbat the status is "idle".

Enable *Advanced / debug* in the web client's settings to see the detector score,
candidates and events.

## 5. Home Assistant

Settings → Devices & services → Add integration → *Shabbat Elevator Tracker* →
`http://192.168.1.50:8080`. (Install the integration first: HACS → Custom repositories →
this repository, category Integration; or copy `custom_components/shabbat_elevator_tracker`
into HA's `config/custom_components/` and restart HA.)

## 6. Access from outside (Cloudflare Tunnel)

Point a tunnel's public hostname at `http://192.168.1.50:8080`. In `config.yaml` set:

```yaml
server:
  expose_debug: false
  allow_manual_sync: false
```

Over HTTPS the web client can keep phone screens on with the Wake Lock API.

## Update, backup, remove

```bash
pct exec 230 -- /opt/shabbat-elevator-tracker/update.sh          # latest main
pct exec 230 -- /opt/shabbat-elevator-tracker/update.sh v0.2.0   # a tag
./create-lxc.sh --ctid 230 --src /root/shabbat-elevator-tracker.tar.gz   # from a tarball
```

Back up `/etc/shabbat-elevator-tracker` and `/var/lib/shabbat-elevator-tracker` (or the
whole container with `vzdump 230`). Remove with `pct stop 230 && pct destroy 230`.

## Other LXC / VM / bare Debian

`deploy/lxc/install.sh` works on any Debian 12 or Ubuntu 22.04+ system with systemd:

```bash
sudo bash deploy/lxc/install.sh            # from GitHub
sudo bash deploy/lxc/install.sh --src .    # from this checkout
```
