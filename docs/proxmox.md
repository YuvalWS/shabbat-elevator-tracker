# Running on Proxmox (LXC)

The tracker runs well in a small unprivileged Debian 12 container: 1 core, 256 MB RAM,
3 GB disk. One command on the Proxmox host creates the container and installs everything;
one command inside it updates it.

## 1. Create the container

Open a shell on the Proxmox host (web UI → node → Shell, or `ssh root@proxmox`) and run:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/YuvalWS/shabbat-elevator-tracker/main/deploy/proxmox/create-lxc.sh)"
```

It asks for *Default settings* (DHCP, next free container id) or *Advanced settings*
(id, hostname, cores, memory, disk, storage, bridge, static address, port), shows a
summary, and then:

- downloads the Debian 12 template if needed;
- creates an unprivileged container that starts on boot, uses the host's time zone and is
  tagged `shabbat-elevator`;
- installs Python, ffmpeg and the app, and a systemd service;
- prints the URL.

To script it instead, download the file and pass options (no questions are asked when
options are given):

```bash
curl -fsSLO https://raw.githubusercontent.com/YuvalWS/shabbat-elevator-tracker/main/deploy/proxmox/create-lxc.sh
bash create-lxc.sh --ctid 230 --ip 192.168.1.50/24 --gw 192.168.1.1
```

Options: `--ctid`, `--hostname` (shabbat-elevator), `--storage` (local-lvm),
`--template-storage` (local), `--bridge` (vmbr0), `--ip` (dhcp), `--gw`, `--cores` (1),
`--memory` (256), `--swap` (256), `--disk` (3), `--port` (8080), `--ref` (git branch or
tag, default main), `--src` (a local source tarball or directory instead of GitHub),
`--dry-run` (print the commands only).

## 2. Configure

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

## 3. Check it

```bash
pct exec 230 -- journalctl -u shabbat-elevator-tracker -f     # logs
curl http://192.168.1.50:8080/api/v1/health                   # from your LAN
```

Open `http://192.168.1.50:8080/` and choose your floor. "Sensor OK" means the audio stream
works. Outside Shabbat the tracker is idle. During Shabbat it shows "Listening for the
elevator" until it hears the elevator stop at the microphone floor; once it has learned
the elevator's sound, one stop gives a provisional prediction and the next confirms it
(otherwise the first prediction needs two stops, about 7 minutes).

Enable *Advanced / debug* in the web client's settings to see the detector score,
candidates and events.

## 4. Update

Inside the container (Proxmox → the container → Console, or `pct enter 230`), the login
banner shows the running version and the commands:

```bash
update              # install the latest main, if it is newer than what runs
update --check      # only say whether there is an update, and list its commits
update --rollback   # go back to the previous version
update --status     # the running version and the versions kept
update v0.2.0       # a tag or branch instead of main
```

From the Proxmox host the same works as `pct exec 230 -- update`.

Each update is installed as a new release next to the running one
(`/opt/shabbat-elevator-tracker/releases/<date>-<commit>`), and
`/opt/shabbat-elevator-tracker/current` is switched to it. If the service does not answer
`/api/v1/health` within 30 seconds, it is switched back to the previous release and the
new one is deleted, so a bad update leaves the tracker running. The current and the
previous releases are kept (about 120 MB each). Config, credentials and the learned state
are outside the releases and are never touched.

`update` updates the app only; update Debian itself with `apt update && apt upgrade`.

To install a local working copy instead of GitHub (for example a change not pushed yet):

```bash
# on your PC, in the repository folder:
git archive --format=tar.gz -o /tmp/set.tar.gz HEAD && scp /tmp/set.tar.gz root@proxmox:/root/
# on the Proxmox host:
pct push 230 /root/set.tar.gz /root/set.tar.gz
pct exec 230 -- /opt/shabbat-elevator-tracker/install.sh --src /root/set.tar.gz
```

## 5. Home Assistant

Install the integration with HACS: HACS → ⋮ → Custom repositories →
`https://github.com/YuvalWS/shabbat-elevator-tracker`, type *Integration* → download
*Shabbat Elevator Tracker* → restart HA. (Or copy `custom_components/shabbat_elevator_tracker`
into HA's `config/custom_components/` and restart HA.)

Then Settings → Devices & services → Add integration → *Shabbat Elevator Tracker* →
`http://192.168.1.50:8080`, and choose your floor.

## 6. Access from outside (Cloudflare Tunnel)

Point a tunnel's public hostname at `http://192.168.1.50:8080`. In `config.yaml` set:

```yaml
server:
  expose_debug: false
  allow_manual_sync: false
```

Over HTTPS the web client can keep phone screens on with the Wake Lock API.

## Backup and remove

Back up `/etc/shabbat-elevator-tracker` and `/var/lib/shabbat-elevator-tracker` (or the
whole container with `vzdump 230`). Remove with `pct stop 230 && pct destroy 230`.

## Other LXC / VM / bare Debian

`deploy/lxc/install.sh` works on any Debian 12 or Ubuntu 22.04+ system with systemd, and
installs the same `update` command:

```bash
sudo bash -c "$(curl -fsSL https://raw.githubusercontent.com/YuvalWS/shabbat-elevator-tracker/main/deploy/lxc/install.sh)"
sudo bash deploy/lxc/install.sh --src .    # from this checkout
```
