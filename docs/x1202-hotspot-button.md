# X1202 Hotspot Button

OpenFlight can create a local Wi-Fi network using Raspberry Pi NetworkManager:

```bash
./scripts/start-kiosk.sh \
  --hotspot \
  --sigmf-capture-dir ~/openflight_sessions/range-sigmf
```

The access point is `Launch_Dev` with WPA2 password `Jetson1$`. The Pi uses
`10.42.0.1`; connect a Mac to that SSID and open `http://10.42.0.1:8080`.
The hotspot intentionally remains enabled after Ctrl-C so logs and SigMF data
can be retrieved. Starting over SSH through `wlan0` may disconnect that SSH
session; Ethernet or the local X1202 button is preferred.

## X1202 button listener

Run the diagnostic first:

```bash
sudo scripts/setup/setup_x1202_button.sh --diagnose
```

Install the listener only when the diagnostic reports a `KEY_POWER` device:

```bash
sudo scripts/setup/setup_x1202_button.sh --install --confirm
```

A short press starts kiosk with hotspot mode and the default SigMF directory:
`/home/admin/openflight_sessions/range-sigmf`. A second short press stops kiosk,
disables the OpenFlight hotspot, and asks NetworkManager to reconnect `wlan0`
to a saved Wi-Fi network. The installer disables the normal systemd-logind
power-button action while installed, so the button will not shut down the Pi.
Restore the original behavior with:

```bash
sudo scripts/setup/setup_x1202_button.sh --rollback
```

The installer refuses to modify anything unless it can detect a Linux input
device advertising `KEY_POWER` and `--confirm` is supplied.
