# OPS243 raw I/Q SigMF collection

Normal kiosk startup saves raw I/Q captures to
`~/openflight_sessions/range-sigmf` automatically. To use a different
directory, pass an override:

```bash
cd ~/openflight
./scripts/start-kiosk.sh --sigmf-capture-dir ~/range-data/custom-session
```

Kiosk remains the only process that opens the OPS243 serial port. Every
complete I/Q response after a speed trigger is saved before the normal
ball-speed filter runs, so the directory contains both accepted shots and
UI-rejected/miss buffers. The kiosk UI, FFT processing, session logging, and
shot pipeline otherwise continue normally. Kiosk startup also enables the
modified OPS243 `SZ` continuous-sampling frame-window processor. The reader
waits for the complete combined response after the Q array, so board
speed/magnitude records that follow the raw I/Q arrays are preserved without
issuing an `S!` command in the OPS hardware-trigger path.

Each kiosk listener run creates a timestamped session directory. Every
complete capture gets an ordered, timestamped hit directory with four
files: the SigMF data, matching SigMF metadata, and raw I/Q ADC samples as
Human-readable text:

```text
range-sigmf/
  session-YYYYMMDDTHHMMSSZ/
    hit-0001-YYYYMMDDTHHMMSS-microsecondsZ/
      capture.sigmf-data
      capture.sigmf-meta
      iq.txt
      board-speed-magnitude.jsonl
```

`iq.txt` contains an `I:` block with comma-separated I values, a blank line,
and a `Q:` block with comma-separated Q values. Each block wraps to a new line
after every 13 values. Values are unmodified OPS243 12-bit ADC counts. The hit
counter restarts at `0001` for every kiosk run.

The board JSONL file contains one header followed by each SZ-produced
speed/magnitude record. It preserves board-native values and adds a
capture-relative timestamp. If the board returns only I/Q, the file still
contains a zero-record status header. Pass `--no-board-processing` to
`start-kiosk.sh` to disable SZ for a diagnostic run.

Use the standalone collector only for raw-only sessions without kiosk, UI, or
simulator services:

```bash
cd ~/openflight
uv run python scripts/hardware-test/capture_ops243_sigmf.py --port /dev/ttyACM0
```

The collector waits for the normal outbound speed trigger (strictly greater
than 25 mph), captures the OPS243 rolling buffer, and prints one status line
per attempt. Stop it with `Ctrl-C`.

Each complete I/Q response creates a matching pair in
`~/openflight_sessions/ops243_sigmf` by default:

```text
ops243-YYYYMMDDTHHMMSS-microsecondsZ-0001.sigmf-data
ops243-YYYYMMDDTHHMMSS-microsecondsZ-0001.sigmf-meta
```

The `.sigmf-data` file contains raw, interleaved `ci16_le` values in I/Q
order. The matching metadata derives static fields from
`config/ops243-iq.sigmf-meta` and adds the trigger context.

Useful overrides:

```bash
uv run python scripts/hardware-test/capture_ops243_sigmf.py \
  --port /dev/ttyACM0 \
  --output-dir ~/range-data/driver-session \
  --trigger-speed 30
```

The command saves every complete parsed I/Q capture after a speed trigger,
including captures with no usable ball-speed peak. UART timeouts and malformed
responses are reported in the terminal but do not create SigMF files.

Do not run the standalone collector at the same time as kiosk: two programs
reading `/dev/ttyACM0` can cause the transient serial-read error observed on
the Pi.
