# PRD: GnssBase

An RTK base station on a Raspberry Pi Zero W with a u-blox ZED-F9P,
serving NTRIP corrections on a home network, with a web interface for
headless control. Written 2026-10-31 by the Claude working on GR6-v2
(Ben's robot), for the Claude that builds this. You don't have that
conversation, so everything you need from it is here.

## Why it exists

Ben's robot mower GR6 (repo `s7711/GR6-v2`, public on GitHub; the robot's
Pi is called `amundsen`) navigates with an OxTS xNAV650 INS. Its RTK
corrections currently come from OxTS's NTRIP caster (`ntrip.oxts.com`,
mountpoint `OXTS1`), a base about 38 km away. A month of testing (see
GR6-v2's `oxts-nav/GNSS-EMI-problems.md`) found the long baseline is the
main factor in bad RTK behaviour, with a poor local GNSS environment as
the trigger. A local base, on the house chimney with a 20 m antenna
cable, should fix the slow drift and make the robot re-fix faster.
GnssBase is that base. It's a separate project because it will be used
for other things too.

## Hardware and what's already set up

- **Raspberry Pi Zero W**, hostname `GnssBase` (`GnssBase.local` via
  mDNS), user `pi`. Raspberry Pi OS 13 (trixie) Lite, **32-bit armv6l**,
  426 MB RAM, 29 GB SD card, Python 3.13.5 with venv. No git yet
  (`sudo apt install git` if you want it).
- Already done: passwordless sudo for `pi`
  (`/etc/sudoers.d/010_pi-nopasswd`); wifi power-saving off
  (`/etc/NetworkManager/conf.d/wifi-powersave-off.conf`); fully upgraded.
  Wifi: about −58 dBm on the bench. `iw` is in `/usr/sbin`.
- **ArduSimple simpleRTK2B** (ZED-F9P), **firmware HPG 1.51**, on factory
  settings. Its "POWER+GPS" USB-C goes to the Zero's USB (OTG) port and
  appears as `/dev/ttyACM0` (`1546:01a9`). The board's antenna port
  powers the antenna's LNA.
- **Hot-plugging the F9P rebooted the Zero** (inrush on the 5 V supply).
  They're always powered together, so it doesn't matter in use. But if
  `vcgencmd get_throttled` ever shows undervoltage, suspect the micro-USB
  power cable first.
- **Antenna**: tripleband, with a 20 m RG58 extension for the chimney
  (roughly 10–14 dB of loss at L1/L2). The F9P's MON-RF antenna status
  and AGC level show whether the LNA power and gain are OK. Show them
  on the status page.
- **The Zero W can't run VS Code Remote or Claude Code** (both need ARMv7
  or later). You run on Ben's desktop Pi 5 (`rpi5-ssd`) and reach the
  Zero by SSH (`ssh pi@GnssBase.local`). Copy code across with `rsync`
  for the edit-and-test loop; don't make a commit for each test run.

## What it does

One app owns the F9P and has two modes. They're modes of one app, not
two apps, because each one is a different configuration of the F9P
itself, and only one process can own the serial port.

### Locate mode: find the base antenna's position

The base's coordinates set the frame of everything that uses its
corrections. GR6's maps, boundaries and surveys are all in the frame the
OXTS1 corrections give. A survey-in, where the F9P averages its own
standalone fix, can be a metre or more out, and that would shift
everything GR6 has recorded. So the base position must come from an
**RTK fix on the same corrections GR6 uses now**:

1. Configure the F9P as a rover (TMODE disabled).
2. Run an NTRIP client to the configured caster (OXTS1 to start with),
   feeding the RTCM into the F9P. Send it a GGA from the F9P's own
   solution every 10 s or so, in case the caster wants one.
3. Average the position over **RTK fixed (carrier solution 2) epochs
   only**. Use UBX-NAV-HPPOSECEF, which gives ECEF to 0.1 mm. ECEF goes
   straight into the F9P's fixed-mode configuration, so no geodesy
   conversion is needed. Show progress live: the epoch count, the SD of
   each axis, and the time spent fixed against time spent not fixed.
4. Ben decides when it's good enough (probably 30–60 min, longer is
   better) and presses "Use this position". That saves it to the local
   config and switches to Base mode.

The antenna moves from a ground test to the chimney later, so Locate is
run again then. Keep the previous positions with their date, SD and
duration, so it's visible when one was replaced.

Be gentle with the caster. A rejected or dropped connection backs off
(30 s, doubling to at least 10 min). Never reconnect in a tight loop.
GR6 got its IP banned for a day by RTK2go over exactly this.

Also offer a **survey-in** start (CFG-TMODE survey-in, with a minimum
duration and accuracy) for quick bench tests where the frame doesn't
matter. Label it as such. It must never quietly become the base
position GR6 uses.

### Base mode: serve corrections

1. Configure the F9P in fixed mode (CFG-TMODE-MODE = FIXED, ECEF with
   the high-precision parts) at the stored position.
2. Enable RTCM 3 output on USB: 1005 (antenna position), MSM4 for each
   constellation in use (1074 GPS, 1084 GLONASS, 1094 Galileo, 1124
   BeiDou) at 1 Hz, 1230 (GLONASS code-phase biases), and 1005 every
   10 s or so. The xNAV's own GNSS receivers are ZED-F9Ps (HPG 1.50),
   so this is the standard u-blox base-to-rover setup.
3. Run a small **NTRIP caster** in the same app on port 2101, with one
   configurable mountpoint, streaming the RTCM to every connected
   client. It needs to handle:
   - **NTRIP v1**: `GET /<mount>` gets `ICY 200 OK` then the stream.
     Answer v2 requests sensibly too (`HTTP/1.1 200 OK`, with
     `Ntrip-Version: Ntrip/2.0` if asked); check what the xNAV actually
     sends.
   - **Source-table requests.** `GET /` must return a valid source
     table. RTK2go reported that the xNAV's OxTS NTRIP client asks for
     one anonymously on every connect.
   - **Ignore whatever the client sends** after connecting (the xNAV
     sends GGA). Don't let it back up.
   - **Optional credentials** (Basic auth) from the local config. If
     none are configured, accept any, since the xNAV's client may insist
     on sending some.
   - **A slow or dead client must never stall the others or the F9P
     reader.** Give each client its own bounded queue and drop it when
     the queue overflows.
   - **Log connects and disconnects** with the client's IP, and show the
     current clients on the status page.
4. The F9P is configured in its RAM layer at every app start, from the
   config. Don't rely on settings saved in its flash. That makes a
   factory-fresh or swapped receiver just work. (The xNAV works the
   same way: its config file is sent to the receiver at each start.)
   Check every VALSET's ACK or NAK and show a NAK as an error. It's how
   GR6 found that HPG 1.50 rejects some settings.

### Status: what the web page shows

Enable UBX NAV-PVT, NAV-SAT, NAV-HPPOSECEF, NAV-SVIN (survey-in only)
and MON-RF at 1 Hz alongside the RTCM. Show:

- **Mode** (Locate, Survey-in or Base) and the receiver's own view of
  it: fix type and carrier solution in Locate, TMODE in Base.
- **Satellites**: used and tracked per constellation, and C/N0 per
  satellite (bars or a sky plot; whatever's clear and cheap).
- **Receiver**: MON-RF antenna status (OK, short, open), jamming
  indicator, AGC count, firmware version (MON-VER at start).
- **Corrections**:
  - In Base: RTCM messages out per type and rate, and connected clients.
  - In Locate: the NTRIP client's state, the age of the corrections,
    and bytes received.
- **The Pi**: CPU temperature, `get_throttled`, wifi signal, uptime.

Live updates over a websocket, about 1 Hz.

## Architecture: copy GR6-v2's

Reuse GR6-v2's patterns and code rather than designing new ones. Read
these in `s7711/GR6-v2` on GitHub (public):

- `top-prd.md`: one systemd unit per service, a shared config, IPC by
  data shape, and the debugging workflow.
- `manager/` (`manager-prd.md`, `app.py`, templates): a small Flask app
  with a home page of icons, a services page (start, stop, restart,
  journal), and a config editor. Copy it and adapt the unit prefix:
  units here are `gnssbase-<service>.service`.
- `shared/config.py`, `shared/web.py`, `shared/web/` (base template,
  vendored Bootstrap, so no internet is needed at runtime),
  `shared/sysstats.py`, and `shared/feed_server.py` / `feed_client.py`.
- `ui-style.md`: Bootstrap 5.3, contextual colours for status, and light
  and dark mode from the OS.

Services:

| Service | Port | Job |
|---|---|---|
| `manager` | 8000 | as in GR6 |
| `base` | 8001 web, 2101 NTRIP | owns `/dev/ttyACM0`: modes, F9P config, UBX parsing, NTRIP client and caster, status page |

`base` also publishes its parsed status on a Unix-socket feed
(`shared/feed_server.py`). A later app, such as a logger, can then use
the stream without owning the port. That's the cheap version of GR6's
camera "sharing app" split.

Dependencies: Flask, flask-sock, PyYAML, pyserial, and **pyubx2** (plus
pyrtcm, which it pulls in) for parsing the mixed UBX/NMEA/RTCM stream
and building UBX messages. All pure Python, so they're fine on armv6.
Don't copy GR6's `requirements.txt`: OpenCV, SciPy and the rest have no
place on a Zero. Watch memory: 426 MB in total.

The services run under systemd as `pi`, with the venv in the repo, the
same as GR6. Ideally only the manager starts at boot. But the base
should come back by itself after a power cut, so `base` is enabled
too, starting in whichever mode it was last in (Base, normally).

## Privacy: the repo is public

The base position is Ben's house to the centimetre. Never commit it.

- Everything site-specific goes in **`config.yaml`, which is gitignored**:
  the base position and its history, the caster login (the OXTS1 user
  and password are secrets), the mountpoint name and caster password,
  and any IP addresses. Commit `config.yaml.example` with placeholders,
  as GR6 does.
- Logs, recorded receiver data, and test fixtures taken from the real
  receiver contain the position (in 1005 messages, PVT and GGA). Keep
  logs out of git, and build test fixtures with a made-up position.
- Check each commit's diff for coordinates before committing.

## Testing

pytest, alongside the code, as in GR6. Unit-test what can go wrong
without hardware:
- stream framing across reads that split messages;
- the averaging (fixed epochs only);
- the caster's request handling (v1, v2, source table, auth, slow
  clients);
- VALSET building and ACK/NAK matching;
- the backoff.

Then test on the real F9P over SSH. The caster can be tested with
`str2str` (RTKLIB) or `curl` before the xNAV is involved.

## Not in scope

- Changes on the xNAV or GR6 side. Pointing the xNAV at the new caster
  is done from GR6 by its Claude.
- Radio links, and pushing to public casters (RTK2go etc.).
- Raw data logging and RINEX for post-processing. It's a likely next
  app, and the feed is there for it.

## Practical notes for Ben's side

- GnssBase needs a **fixed IP**, a DHCP reservation on the home router,
  because the xNAV's NTRIP client needs an address and probably can't
  resolve `.local` names. The xNAV reaches the home network through
  amundsen (NAT on amundsen's ethernet).
- **Ground test first, then the chimney.** Run Locate at each position.

## Working with Ben

- Ben has deep INS, GNSS and Kalman filter expertise, so talk to him at
  that level. He dislikes web and HTML work, and leaves frontend
  judgement to you.
- Ben leaves git to you: commits, pushes, `.gitignore`. Commit or push
  only when he asks, and give commits clear messages.
- Discuss design questions one at a time, in prose, not as option
  menus.
- Prefer a mechanism that already exists (in GR6 or a standard tool) over
  inventing a new one.
