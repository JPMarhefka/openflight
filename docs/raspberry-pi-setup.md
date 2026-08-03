# Raspberry Pi Setup Guide

Complete guide for setting up OpenFlight on a Raspberry Pi 5 with the 7" touchscreen display.

## Prerequisites

Make sure you have all the hardware. See the **[Parts List](PARTS.md)** for what to buy.

**Required:**
- Raspberry Pi 5 (4GB+ recommended)
- 7" Touchscreen Display
- MicroSD Card (32GB+)
- 27W USB-C Power Supply (official Pi 5 PSU recommended)
- OPS243-A Doppler Radar + USB cable

**Optional:**
- K-LD7 + FTDI adapter (×2) — for launch angle and club path (see [Parts List](PARTS.md)). **Deprecated** — superseded by a more capable radar chip; don't buy for a new build. Supported for existing builds only.
- SparkFun SEN-14262 sound detector — legacy sound-trigger option

## Setup

### 1. Install Raspberry Pi OS

Use Raspberry Pi Imager to flash **Raspberry Pi OS (64-bit)** to your SD card.

### 2. Run the setup script

Plug in the OPS243-A (and the K-LD7 adapters if you have them), then:

```bash
cd ~
git clone https://github.com/jewbetcha/openflight.git
cd openflight
./scripts/setup/setup.sh
```

The script installs everything, then walks you through the one-time hardware
configuration with prompts:

1. **Dependencies** — Python venv, packages, UI build, test run
2. **OPS243-A radar** — saves rolling buffer mode to the radar's flash
   (you'll be asked to unplug/replug the radar once)
3. **K-LD7 radars** (deprecated; if you have them) — identifies each radar by
   plugging them in one at a time, so OpenFlight always knows which is which
4. **Auto-start on boot** — optional systemd service
5. **Desktop shortcut** — optional

Every step can be skipped and the script is **safe to re-run** any time —
it picks up where you left off.

### 3. Start hitting balls

```bash
./scripts/start-kiosk.sh                # Default: internal OPS243 trigger + rolling buffer
./scripts/start-kiosk.sh --kld7         # With K-LD7 angle radars (deprecated)
./scripts/start-kiosk.sh --mock         # Mock mode (no hardware)
```

Then open `http://localhost:8080` or use the touchscreen.

### 4. (Optional) Stream to a golf simulator

To send shots to GSPro, OpenGolfSim, or another supported sim, copy the
example config and enable your simulator:

```bash
cp config/sim.example.json config/sim.json   # then edit host/port + "enabled": true
```

See **[Simulator Connectors](simulator/README.md)** for the full guide.

---

## What the Script Configures (Reference)

You don't need this section unless something went wrong or you prefer to do
things by hand.

### OPS243-A Rolling Buffer Mode

Legacy hardware sound-trigger installations need a one-time configuration that
saves rolling-buffer mode to flash. The default speed-trigger path does not use
HOST_INT and does not require the SEN-14262.

> **Why?** The OPS243-A has a firmware bug where the HOST_INT pin mode switches
> unexpectedly when entering rolling buffer mode at runtime. Saving to flash and
> power cycling bypasses this. Confirmed by OmniPreSense engineering.

<details>
<summary>Manual steps</summary>

```bash
# 1. Configure and save to flash
uv run python scripts/hardware-test/test_rolling_buffer_persist.py --setup

# 2. Power cycle the radar, wait 3 seconds, reconnect (see note below)

# 3. Verify — make a sound near the SEN-14262, you should see I/Q trigger data
uv run python scripts/hardware-test/test_rolling_buffer_persist.py --test
```

How you power cycle depends on how the OPS243 is connected:

| Connection | Power cycle by |
|------------|----------------|
| USB | Unplugging the USB cable |
| Pi GPIO UART | Disconnecting 5V from OPS `J3` pin 9 — **not** by rebooting the Pi, which does not necessarily drop the header rail |

If the OPS243 is on the GPIO UART, add `--port /dev/ttyAMA0` to both commands
above.

</details>

### IWR6843 Angle Radar

The setup script does not configure the IWR6843 — it needs custom firmware
flashed over the ROM bootloader, which requires physically moving a switch on
the board. That is covered end to end in the
**[IWR6843 Operator Guide](iwr6843/README.md)**.

Do it in this order, and confirm each step works before starting the next:

1. **Move the OPS243 to the Pi GPIO UART** —
   [migration guide](ops243-uart-migration.md). The Pi cannot power both radars
   over USB, so the OPS243 has to vacate the USB port. Validate the OPS on its
   own after rewiring, before the TI board is involved at all.
2. **Flash the IWR6843** — operator guide, *Flash The IWR6843 Firmware*. A
   validated prebuilt image is in `firmware/releases/`, so the TI toolchain is
   not required. You only need the
   [firmware developer guide](../firmware/README.md) to build from source.
3. **Mount, aim, and measure geometry** — operator guide. The geometry values
   are passed on the command line and a wrong one silently biases the launch
   angle instead of erroring, so measure rather than estimate.

Re-flashing is only needed if the image in `firmware/releases/` changes. A
software update alone does not require it — compare the release filename against
what you flashed.

> [!WARNING]
> A **WiFi-equipped OPS243-A cannot use the GPIO UART.** Its WiFi module already
> drives the radar processor's UART receive line, so the Pi cannot send it
> commands, and OpenFlight must be able to reconfigure and rearm the OPS after
> every capture. Use a separately powered USB hub for both radars instead
> (operator guide, Option B).

### K-LD7 Device Names (Deprecated Hardware)

USB serial adapters can swap between `/dev/ttyUSB0` and `/dev/ttyUSB1` after a
reboot, so OpenFlight needs fixed names (`/dev/kld7_vertical` and
`/dev/kld7_horizontal`) to tell the two radars apart. The wizard handles this —
you just plug each radar in when asked:

```bash
./scripts/setup/setup_kld7_devices.sh          # run / redo the mapping
./scripts/setup/setup_kld7_devices.sh --show   # check the current mapping
```

It also installs the FTDI low-latency rule (the K-LD7 RADC stream runs at
3 Mbaud and needs `latency_timer=1ms` instead of the Linux default 16ms).
On startup, the server logs should show both radars at `1ms`:

```text
[KLD7:vertical] USB serial latency_timer=1ms ...
[KLD7:horizontal] USB serial latency_timer=1ms ...
```

<details>
<summary>Manual steps (what the wizard does)</summary>

Find each adapter's serial number:

```bash
udevadm info -a /dev/ttyUSB0 | grep '{serial}' | head -1
udevadm info -a /dev/ttyUSB1 | grep '{serial}' | head -1
```

Create a udev rule with the serial numbers (replace `FTXXXXXX`/`FTYYYYYY`):

```bash
sudo tee /etc/udev/rules.d/99-kld7.rules << 'EOF'
SUBSYSTEM=="tty", ATTRS{serial}=="FTXXXXXX", SYMLINK+="kld7_vertical"
SUBSYSTEM=="tty", ATTRS{serial}=="FTYYYYYY", SYMLINK+="kld7_horizontal"
EOF

sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then install the latency rule:

```bash
sudo scripts/setup/setup_kld7_latency.sh
```

Use `--dry-run` to preview the rule, or `--all-ftdi` if the `/dev/kld7_*`
names aren't set up yet.

</details>

### Auto-Start on Boot

The setup script installs and enables a systemd service configured for your
username and install path.

<details>
<summary>Manual steps and service management</summary>

```bash
# Install (adjust User= and paths in the file if your username isn't the default)
sudo cp ~/openflight/scripts/setup/openflight.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable openflight
sudo systemctl start openflight
```

Management:

```bash
sudo systemctl status openflight --no-pager   # Check status
journalctl -u openflight -f                   # View logs
sudo systemctl stop openflight                # Stop
sudo systemctl restart openflight             # Restart
sudo systemctl disable openflight             # Disable auto-start
```

To modify the service:

```bash
sudo nano /etc/systemd/system/openflight.service
sudo systemctl daemon-reload
sudo systemctl restart openflight
```

</details>

---

## K-LD7 Physical Setup (Deprecated Hardware)

> **⚠️ DEPRECATED:** The K-LD7 angle radars are deprecated — this section applies to existing K-LD7 builds only.

### Mounting

- **Vertical unit** — measures launch angle. Mount with the antenna plane vertical, aimed at the hitting area.
- **Horizontal unit** — measures club path / aim direction. Mount with the antenna plane horizontal.

Both should be positioned near the OPS243-A, 3-5 feet behind the tee.

### Geometry Calibration

The vertical K-LD7 geometry estimator needs the physical mount tilt,
ball-to-radar distance, and boresight offset to match real launch angles.

1. Start a session with `--kld7-geometry`
2. Hit 5-10 shots with a known club (7-iron recommended)
3. Compare reported launch angles to expected values:
   - Wedge: 24-30°, 7-iron: 16-18°, 5-iron: 12-14°, Driver: 10-14°
4. Keep `--kld7-mount-tilt` and `--kld7-ball-distance` matched to the physical
   setup; adjust `--kld7-angle-offset` for a stable boresight bias.

The current field preset is mount tilt `10°`, ball distance `5ft`, and angle
offset `+2.5°`. The exact values depend on your mounting position.

See [K-LD7 Troubleshooting](kld7-troubleshooting.md) for more details.

## Running OpenFlight

### Kiosk Mode (Fullscreen — Recommended)

```bash
./scripts/start-kiosk.sh                    # Default: rolling buffer + sound trigger
./scripts/start-kiosk.sh --kld7-geometry    # With K-LD7 launch-angle geometry defaults
./scripts/start-kiosk.sh --mock             # Mock mode (no hardware needed)
```

### Manual Start

```bash
openflight-server                # With radar
openflight-server --mock         # No hardware
```

Then open `http://localhost:8080`.

### Running Over SSH

```bash
DISPLAY=:0 ./scripts/start-kiosk.sh
```

## Observability (Grafana Cloud)

OpenFlight can ship session logs to Grafana Cloud for long-term analysis.

```bash
sudo ./scripts/setup/setup_alloy.sh
sudo vim /etc/alloy/credentials.env
```

See [observability.md](observability.md) for full setup and LogQL queries.

## Troubleshooting

### Radar Not Detected

```bash
ls /dev/ttyACM* /dev/ttyUSB*
openflight --port /dev/ttyACM0 --info
```

### Sound Trigger Not Working

See the [Sound Trigger Wiring Guide — Troubleshooting](sound-trigger-wiring.md#troubleshooting).

### K-LD7 Not Connecting

```bash
# Check the device mapping
./scripts/setup/setup_kld7_devices.sh --show

# Test standalone
uv run python scripts/hardware-test/test_kld7.py
```

If the mapping is missing or points at the wrong radar, re-run the wizard:
`./scripts/setup/setup_kld7_devices.sh`. Look for `[KLD7] Connected on
/dev/ttyUSB...` in the server logs. See [K-LD7 Troubleshooting](kld7-troubleshooting.md)
for "Wrong length reply" and other connection issues.

### Service Won't Start

```bash
journalctl -u openflight --no-pager -n 50

# If service is masked
sudo systemctl unmask openflight
sudo cp ~/openflight/scripts/setup/openflight.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable openflight
```

### Slow UI Updates

Check for WebSocket instability:
```bash
journalctl -u openflight -f
```

Look for "Client disconnected/connected" messages.

### Display Issues Over SSH

Use `DISPLAY=:0` prefix for commands that need the Pi's display.

## CLI Reference

### Kiosk

```bash
./scripts/start-kiosk.sh                                      # Default
./scripts/start-kiosk.sh --mock                                # No hardware
./scripts/start-kiosk.sh --kld7-geometry                       # With angle radar
./scripts/start-kiosk.sh --port 3000                           # Custom port
```

### Server

```bash
openflight-server                    # Start with radar
openflight-server --mock             # Mock mode
openflight-server --web-port 3000    # Custom port
```

### Setup

```bash
./scripts/setup/setup.sh                       # Full interactive setup (re-run safe)
./scripts/setup/setup.sh --deps-only           # Dependencies only
./scripts/setup/setup_kld7_devices.sh          # K-LD7 device naming wizard
./scripts/setup/setup_kld7_devices.sh --show   # Show current K-LD7 mapping
```

### Testing

```bash
uv run python scripts/hardware-test/test_rolling_buffer_persist.py --test    # Sound trigger
uv run python scripts/hardware-test/test_sound_trigger_hardware.py           # Direct trigger test
uv run python scripts/hardware-test/test_kld7.py                             # K-LD7 standalone
uv run pytest tests/ -v                                                      # Full test suite
```
