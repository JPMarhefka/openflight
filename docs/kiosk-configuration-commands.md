# Kiosk configuration commands

The kiosk should be stopped before changing radar acquisition settings. Start
it again with the new arguments so every capture in that run uses one known
configuration. The effective configuration is written into each SIGMF
metadata file under `openflight:configuration`.

## Inspect the generated server command

```bash
./scripts/start-kiosk.sh --dry-run
```

This prints the command without starting the radar server or Chromium.

## Recommended sensor-test command

```bash
./scripts/start-kiosk.sh \
  --trigger hardware \
  --trigger-threshold 25 \
  --pre-trigger-segments 4 \
  --sample-rate 30 \
  --sigmf-capture-dir ~/openflight_sessions/sigmf-s4 \
  --radar-log
```

At 30 ksps, `--pre-trigger-segments 4` sends `S#4`: approximately 17 ms
before the trigger and 119 ms after it.

## Trigger and capture settings

| Argument | Example | Meaning |
|---|---:|---|
| `--trigger hardware` | `hardware` | OPS243 internal speed trigger; production default |
| `--trigger speed` | `speed` | Host-polled club-speed trigger; uses `S#0` during capture |
| `--trigger polling` | `polling` | Repeated rolling-buffer polling; diagnostic/development use |
| `--trigger threshold` | `threshold` | Legacy threshold trigger |
| `--trigger sound` | `sound` | Legacy sound-trigger path |
| `--trigger-threshold 25` | `25` | Speed threshold in mph for `hardware` or `speed` |
| `--trigger-magnitude 40` | `1`–`2000` | OPS243 `SMn` magnitude threshold for the internal `hardware` trigger |
| `--pre-trigger-segments 4` | `0`–`32` | `S#n` pre-trigger blocks for hardware/rolling-buffer paths |
| `--buffer-split post-heavy` | `post-heavy` | Preset for the pre/post split; raw numbers are also accepted |
| `--sample-rate 30` | `30` | Radar sample rate in ksps; hardware/speed require 30 |
| `--sigmf-capture-dir PATH` | `~/openflight_sessions/sigmf-s4` | Save raw I/Q captures and metadata |

The buffer contains 32 segments total. At 30 ksps, each segment is about
4.27 ms, so the post-trigger portion is `32 - S#n` segments.

## Hardware and display settings

```bash
./scripts/start-kiosk.sh \
  --kld7 \
  --kld7-mount-tilt 0.0 \
  --kld7-ball-distance 5.5 \
  --net-distance 10 \
  --session-location range \
  --ballistics
```

Useful switches include:

- `--kld7`, `--kld7-port`, `--kld7-angle-offset`
- `--kld7-mount-tilt`, `--kld7-ball-distance`, `--net-distance`
- `--kld7-horizontal`, `--kld7-horizontal-port`, `--kld7-horizontal-offset`
- `--kld7-vertical-raw`
- `--ballistics`, `--calculated-spin`, `--sim`
- `--mock`, `--debug`, `--radar-log`, `--no-camera`
- `--session-location LOCATION`, `--port PORT`, `--hotspot`

## Comparing configurations

Use a separate SIGMF directory for each run:

```bash
./scripts/start-kiosk.sh --trigger hardware --pre-trigger-segments 4 \
  --sigmf-capture-dir ~/openflight_sessions/sigmf-s4

# Stop the kiosk, then restart with the next configuration.
./scripts/start-kiosk.sh --trigger hardware --pre-trigger-segments 8 \
  --sigmf-capture-dir ~/openflight_sessions/sigmf-s8
```

The metadata records both the requested CLI configuration and the effective
capture split. In particular, host `speed` triggering is recorded as `S#0`
because that path intentionally switches to a post-trigger-only capture.
