# OpenFlight SigMF extension

Version: 1.0.0

This extension records OPS243 trigger context for a SigMF capture.
All fields below belong in a capture object.

| Field | Type | Meaning |
| --- | --- | --- |
| `openflight:sample_time` | number | OPS243 sampling-start clock in seconds since radar power-on. |
| `openflight:trigger_time` | number | OPS243 trigger clock in seconds since radar power-on. |
| `openflight:host_trigger_timestamp` | number | Host Unix timestamp inferred for the board or host trigger. |
| `openflight:trigger_speed_mph` | number, optional | Outbound reading that initiated a host-polled speed capture; absent for an internal OPS243 trigger. |
| `openflight:capture_latency_ms` | number | Host elapsed time from trigger wait to completed UART capture. |
| `openflight:trigger_threshold_mph` | number | Configured internal or host speed threshold in mph. |
| `openflight:sample_count` | integer | Number of complex I/Q pairs in the recording. |
