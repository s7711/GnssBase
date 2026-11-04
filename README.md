# GnssBase

An RTK base station: a Raspberry Pi Zero W with a u-blox ZED-F9P
(ArduSimple simpleRTK2B), serving NTRIP corrections on the home network,
with a web interface. See [gnssbase-prd.md](gnssbase-prd.md).

| Service | Port | |
|---|---|---|
| `manager` | 8000 | home page, services (start/stop/journal, power off), config editor |
| `base` | 8001 web, 2101 NTRIP | owns the F9P: Locate / Survey-in / Base modes, NTRIP client and caster, status page |

## Modes

- **Locate**: the F9P is a rover on the configured caster's corrections
  (OXTS1). Every epoch is recorded to `base/data/locate-epochs.bin` from
  "Reset average" on, across restarts, so a run can last a week. The
  Analysis page shows E/N/U histograms, the time series and the density
  peaks of the RTK-fixed epochs: over a long baseline a wrong integer fix
  can hold steady for a while, so the right answer is the peak that keeps
  coming back. "Use this position" on a peak saves its mean as the base
  position and switches to Base.
- **Survey-in (bench)**: the F9P's own survey-in, for bench tests only.
  Never saved as the base position.
- **Base**: fixed at the stored position, RTCM 1005, MSM4 and 1230 out
  to the caster's mountpoint.

The base service restarts in the last mode used. The F9P is configured
in RAM at every start, and every VALSET's ACK/NAK is checked and shown.

## Privacy

`config.yaml` (caster logins) and `base/data/` (the base position, its
history and the NTRIP backoff state) are gitignored. Never commit
coordinates. Test fixtures use made-up positions.

## Install on the Zero

```sh
sudo apt install git           # optional
cd ~/GnssBase
python3 -m venv venv
venv/bin/pip install -r requirements.txt
cp config.yaml.example config.yaml   # then fill in the caster login
sudo cp manager/gnssbase-manager.service base/gnssbase-base.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gnssbase-manager gnssbase-base
```

## Development loop

Edit on the desktop Pi, then copy across and restart:

```sh
rsync -a --exclude venv --exclude .git --exclude base/data --exclude __pycache__ \
  --exclude .pytest_cache --exclude manager/config-backup --exclude logs \
  ./ pi@GnssBase.local:GnssBase/
ssh pi@GnssBase.local sudo systemctl restart gnssbase-base
```

To debug, stop the unit (`sudo systemctl stop gnssbase-base`) and run
`venv/bin/python base/app.py` by hand.

Tests: `venv/bin/pytest` (from the repo root).

Check the caster from another machine with `str2str -in ntrip://GnssBase.local:2101/GNSSBASE`
or `curl -s --http0.9 http://GnssBase.local:2101/` (source table).
