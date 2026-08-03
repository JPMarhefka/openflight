#!/usr/bin/env python3
"""Listen for the X1202/Pi power-key input and start OpenFlight kiosk."""

from __future__ import annotations

import argparse
import fcntl
import glob
import os
import select
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

KEY_POWER = 116
EV_KEY = 1
EVENT_STRUCT = struct.Struct("llHHI")
HOTSPOT_CONNECTION = "OpenFlight-Launch-Dev"


def _ioc_read(event: int, size: int) -> int:
    # Linux _IOC(_IOC_READ, 'E', 0x20 + event, size)
    return (2 << 30) | (size << 16) | (ord("E") << 8) | (0x20 + event)


def _device_name(fd: int) -> str:
    buffer = bytearray(256)
    request = (2 << 30) | (len(buffer) << 16) | (ord("E") << 8) | 0x06
    try:
        fcntl.ioctl(fd, request, buffer, True)
    except OSError:
        return "unknown"
    return bytes(buffer).split(b"\0", 1)[0].decode(errors="replace")


def _supports_power_key(fd: int) -> bool:
    size = (KEY_POWER // 8) + 1
    bits = bytearray(size)
    try:
        fcntl.ioctl(fd, _ioc_read(EV_KEY, size), bits, True)
    except OSError:
        return False
    return bool(bits[KEY_POWER // 8] & (1 << (KEY_POWER % 8)))


def find_power_devices() -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    for path in sorted(glob.glob("/dev/input/event*")):
        try:
            with open(path, "rb", buffering=0) as stream:
                if _supports_power_key(stream.fileno()):
                    devices.append((path, _device_name(stream.fileno())))
        except OSError:
            continue
    return devices


def _nmcli(*command: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["sudo", "-n", "nmcli", *command], check=False, capture_output=True)


def _hotspot_is_active() -> bool:
    result = _nmcli("-t", "--escape", "no", "-f", "NAME", "connection", "show", "--active")
    return any(line == HOTSPOT_CONNECTION for line in result.stdout.decode(errors="replace").splitlines())


def _disable_hotspot() -> None:
    down = _nmcli("connection", "down", HOTSPOT_CONNECTION)
    if down.returncode != 0:
        print(
            f"[X1202] Could not disable hotspot: {down.stderr.decode(errors='replace').strip()}",
            flush=True,
        )
        return
    reconnect = _nmcli("device", "connect", "wlan0")
    if reconnect.returncode == 0:
        print("[X1202] Hotspot disabled; reconnecting to a known Wi-Fi network", flush=True)
    else:
        print(
            "[X1202] Hotspot disabled, but NetworkManager could not reconnect wlan0: "
            f"{reconnect.stderr.decode(errors='replace').strip()}",
            flush=True,
        )


def _stop_kiosk(process: subprocess.Popen[bytes]) -> None:
    print("[X1202] Stopping kiosk and disabling hotspot", flush=True)
    try:
        os.killpg(process.pid, signal.SIGINT)
        process.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _disable_hotspot()


def _start_kiosk(args: argparse.Namespace, process: subprocess.Popen[bytes] | None) -> subprocess.Popen[bytes] | None:
    if process is not None and process.poll() is None:
        print("[X1202] Kiosk is already running; ignoring button press", flush=True)
        return process
    command = [
        str(Path(args.project_dir) / "scripts" / "start-kiosk.sh"),
        "--hotspot",
        "--sigmf-capture-dir",
        str(Path(args.sigmf_dir).expanduser()),
    ]
    print(f"[X1202] Starting kiosk: {' '.join(command)}", flush=True)
    return subprocess.Popen(command, cwd=args.project_dir, start_new_session=True)


def listen(args: argparse.Namespace) -> int:
    process = None
    last_press = 0.0
    while True:
        streams = []
        for path, _ in find_power_devices():
            try:
                streams.append(open(path, "rb", buffering=0))
            except OSError:
                continue
        if not streams:
            print("[X1202] No KEY_POWER input device found; retrying", flush=True)
            time.sleep(2.0)
            continue
        try:
            while True:
                readable, _, _ = select.select(streams, [], [], 2.0)
                if process is not None and process.poll() is not None:
                    process = None
                for stream in readable:
                    data = stream.read(EVENT_STRUCT.size)
                    if len(data) != EVENT_STRUCT.size:
                        continue
                    _, _, event_type, code, value = EVENT_STRUCT.unpack(data)
                    if event_type == EV_KEY and code == KEY_POWER and value == 1:
                        now = time.monotonic()
                        if now - last_press >= 1.0:
                            if process is not None and process.poll() is None:
                                _stop_kiosk(process)
                                process = None
                            elif _hotspot_is_active():
                                _disable_hotspot()
                            else:
                                process = _start_kiosk(args, process)
                            last_press = now
        finally:
            for stream in streams:
                stream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--project-dir", default="/home/admin/Desktop/Open Flight Golf/openflight")
    parser.add_argument("--sigmf-dir", default="/home/admin/openflight_sessions/range-sigmf")
    args = parser.parse_args()
    devices = find_power_devices()
    if args.discover:
        for path, name in devices:
            print(f"{path}\t{name}")
        return 0 if devices else 1
    if not devices:
        print("No input device advertising KEY_POWER was found", file=sys.stderr)
        return 1
    return listen(args)


if __name__ == "__main__":
    raise SystemExit(main())
