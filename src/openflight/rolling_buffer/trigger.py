"""
Trigger strategies for rolling buffer capture.

Defines different methods for determining when to capture the rolling buffer.
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, List, Optional

from .processor import RollingBufferProcessor
from .types import BoardProcessedTimeline, IQCapture, SpeedTimeline

if TYPE_CHECKING:
    from ..ops243 import OPS243Radar

# Incremented per capture for log sequencing (not shot number)

logger = logging.getLogger("openflight.rolling_buffer.trigger")


@dataclass
class RawCaptureResult:
    """Outcome of one triggered rolling-buffer acquisition.

    ``capture`` is ``None`` when the OPS243 triggered but did not return a
    complete, parseable I/Q response.  This keeps raw acquisition separate
    from the production monitor's later ball-speed acceptance decision.
    """

    capture: Optional[IQCapture]
    trigger_speed_mph: Optional[float]
    trigger_timestamp: float
    response_bytes: int
    capture_latency_ms: float
    trigger_threshold_mph: Optional[float]
    failure_reason: Optional[str] = None
    board_timeline: Optional[BoardProcessedTimeline] = None


# Retained for callers that use the original speed-trigger public type.
SpeedTriggeredCaptureResult = RawCaptureResult
RawCaptureCallback = Callable[[RawCaptureResult], None]


class TriggerStrategy(ABC):
    """
    Base class for trigger strategies.

    A trigger strategy determines when to capture the rolling buffer.
    Different strategies trade off between simplicity, reliability, and efficiency.
    """

    MIN_VALID_OUTBOUND_MPH = 15.0

    def __init__(self, pre_trigger_segments: int = 12):
        self._diagnostics: List[dict] = []
        self.pre_trigger_segments = pre_trigger_segments
        # The monitor sets this explicitly so the parser follows the same
        # board-processing mode as the radar configuration.
        self.board_processing_enabled: Optional[bool] = None

    def _wait_for_hardware_trigger(
        self,
        radar: "OPS243Radar",
        timeout: float,
    ) -> str:
        """Wait for a dump while preserving auto-detection for direct callers."""
        wait_kwargs = {"timeout": timeout}
        if self.board_processing_enabled is not None:
            wait_kwargs["board_processing_enabled"] = self.board_processing_enabled
        return radar.wait_for_hardware_trigger(**wait_kwargs)

    def _parse_capture(
        self,
        processor: RollingBufferProcessor,
        response: str,
        *,
        first_byte_timestamp: Optional[float] = None,
    ) -> Optional[IQCapture]:
        """Parse a capture using this trigger's explicit board mode."""
        parse_kwargs = {}
        if first_byte_timestamp is not None:
            parse_kwargs["first_byte_timestamp"] = first_byte_timestamp
        if self.board_processing_enabled is not None:
            parse_kwargs["board_processing_enabled"] = self.board_processing_enabled
        return processor.parse_capture(response, **parse_kwargs)

    def drain_diagnostics(self) -> List[dict]:
        """Return and clear accumulated diagnostic entries.

        Diagnostics are accumulated during wait_for_trigger() calls.
        The monitor drains these after each call to log and emit them.
        """
        diagnostics = self._diagnostics
        self._diagnostics = []
        return diagnostics

    def _append_diagnostic(
        self,
        accepted: bool,
        reason: str,
        response_bytes: int = 0,
        total_readings: int = 0,
        outbound_readings: int = 0,
        inbound_readings: int = 0,
        peak_outbound_mph: float = 0.0,
        peak_inbound_mph: float = 0.0,
        all_outbound_speeds: Optional[List[float]] = None,
        all_inbound_speeds: Optional[List[float]] = None,
        peak_outbound_magnitude: float = 0.0,
        peak_inbound_magnitude: float = 0.0,
        trigger_latency_ms: Optional[float] = None,
    ):
        """Append a diagnostic entry for the current trigger event."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "accepted": accepted,
            "reason": reason,
            "response_bytes": response_bytes,
            "total_readings": total_readings,
            "outbound_readings": outbound_readings,
            "inbound_readings": inbound_readings,
            "peak_outbound_mph": peak_outbound_mph,
            "peak_inbound_mph": peak_inbound_mph,
            "all_outbound_speeds": all_outbound_speeds or [],
            "all_inbound_speeds": all_inbound_speeds or [],
            "peak_outbound_magnitude": peak_outbound_magnitude,
            "peak_inbound_magnitude": peak_inbound_magnitude,
        }
        if trigger_latency_ms is not None:
            entry["trigger_latency_ms"] = trigger_latency_ms
        self._diagnostics.append(entry)

    def _summarize_capture_activity(
        self,
        processor: RollingBufferProcessor,
        capture: IQCapture,
    ) -> dict:
        """Summarize movement in a capture before accepting a sound trigger."""
        timeline = processor.process_standard(capture)
        all_readings = timeline.readings
        all_outbound = [r for r in all_readings if r.is_outbound]
        all_inbound = [r for r in all_readings if not r.is_outbound]
        outbound_speeds = [r.speed_mph for r in all_outbound]
        inbound_speeds = [r.speed_mph for r in all_inbound]
        valid_outbound = [r for r in all_outbound if r.speed_mph >= self.MIN_VALID_OUTBOUND_MPH]

        return {
            "total_readings": len(all_readings),
            "outbound_readings": len(all_outbound),
            "inbound_readings": len(all_inbound),
            "peak_outbound_mph": max(outbound_speeds, default=0),
            "peak_inbound_mph": max(inbound_speeds, default=0),
            "all_outbound_speeds": outbound_speeds,
            "all_inbound_speeds": inbound_speeds,
            "peak_outbound_magnitude": max((r.magnitude for r in all_outbound), default=0),
            "peak_inbound_magnitude": max((r.magnitude for r in all_inbound), default=0),
            "valid_outbound_count": len(valid_outbound),
            "valid_peak_outbound_mph": max(
                (r.speed_mph for r in valid_outbound),
                default=0,
            ),
        }

    def _append_activity_diagnostic(
        self,
        summary: dict,
        *,
        accepted: bool,
        reason: str,
        response_bytes: int,
        trigger_latency_ms: Optional[float] = None,
    ):
        """Append a diagnostic entry using capture-activity summary fields."""
        self._append_diagnostic(
            accepted=accepted,
            reason=reason,
            response_bytes=response_bytes,
            total_readings=summary["total_readings"],
            outbound_readings=summary["outbound_readings"],
            inbound_readings=summary["inbound_readings"],
            peak_outbound_mph=summary["peak_outbound_mph"],
            peak_inbound_mph=summary["peak_inbound_mph"],
            all_outbound_speeds=summary["all_outbound_speeds"],
            all_inbound_speeds=summary["all_inbound_speeds"],
            peak_outbound_magnitude=summary["peak_outbound_magnitude"],
            peak_inbound_magnitude=summary["peak_inbound_magnitude"],
            trigger_latency_ms=trigger_latency_ms,
        )

    @abstractmethod
    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
    ) -> Optional[IQCapture]:
        """
        Wait for trigger condition and capture buffer.

        Args:
            radar: Connected OPS243Radar instance in rolling buffer mode
            processor: Processor for parsing capture response
            timeout: Maximum time to wait for trigger

        Returns:
            IQCapture if triggered and captured, None if timeout or error
        """
        pass

    @abstractmethod
    def reset(self):
        """Reset trigger state for next capture."""
        pass


class PollingTrigger(TriggerStrategy):
    """
    Simple polling-based trigger.

    Continuously captures and checks for activity. Simple but
    less efficient than threshold-based triggers.

    Best for testing and development.
    """

    def __init__(
        self,
        poll_interval: float = 0.3,
        min_readings: int = 1,
        min_speed_mph: float = 15,
        pre_trigger_segments: int = 12,
    ):
        """
        Initialize polling trigger.

        Args:
            poll_interval: Seconds between poll attempts (default 0.3s for faster response)
            min_readings: Minimum outbound readings above min_speed (default 1)
            min_speed_mph: Minimum speed to consider activity (default 15 mph)
            pre_trigger_segments: Number of pre-trigger segments for re-arm (0-32)
        """
        super().__init__(pre_trigger_segments=pre_trigger_segments)
        self.poll_interval = poll_interval
        self.min_readings = min_readings
        self.min_speed_mph = min_speed_mph

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
    ) -> Optional[IQCapture]:
        """Poll for activity and return capture when detected."""
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            try:
                # Trigger capture (10s timeout for large I/Q data transfer)
                response = radar.trigger_capture(timeout=10.0)

                # Re-arm for next capture (sensor goes to idle after output)
                radar.rearm_rolling_buffer(self.pre_trigger_segments)

                # Parse response
                capture = self._parse_capture(processor, response)

                if capture is None:
                    time.sleep(self.poll_interval)
                    continue

                # Quick check for activity using standard processing
                timeline = processor.process_standard(capture)

                # Check for significant activity
                outbound = [
                    r
                    for r in timeline.readings
                    if r.is_outbound and r.speed_mph >= self.min_speed_mph
                ]

                if len(outbound) >= self.min_readings:
                    peak = max(r.speed_mph for r in outbound)
                    logger.info(
                        "[TRIGGER] Activity detected: %d readings, peak %.1f mph",
                        len(outbound),
                        peak,
                    )
                    return capture

                time.sleep(self.poll_interval)

            except Exception as e:
                logger.warning("[TRIGGER] Poll error: %s", e, exc_info=True)
                time.sleep(self.poll_interval)

        logger.info("[TRIGGER] Polling trigger timeout")
        return None

    def reset(self):
        """No state to reset for polling trigger."""
        pass


class ThresholdTrigger(TriggerStrategy):
    """
    Speed threshold-based trigger.

    Uses a brief streaming check to detect when speed exceeds threshold,
    then immediately captures the rolling buffer.

    More efficient than polling but requires threshold tuning.
    """

    def __init__(
        self,
        speed_threshold_mph: float = 50,
        check_interval: float = 0.1,
        settling_time: float = 0.05,
        pre_trigger_segments: int = 12,
    ):
        """
        Initialize threshold trigger.

        Args:
            speed_threshold_mph: Speed that triggers capture
            check_interval: Seconds between threshold checks
            settling_time: Time to wait after threshold before capture
            pre_trigger_segments: Number of pre-trigger segments for re-arm (0-32)
        """
        super().__init__(pre_trigger_segments=pre_trigger_segments)
        self.speed_threshold_mph = speed_threshold_mph
        self.check_interval = check_interval
        self.settling_time = settling_time
        self._triggered = False

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
    ) -> Optional[IQCapture]:
        """
        Wait for speed to exceed threshold, then capture.

        Note: This implementation uses polling-style capture since the
        radar's internal threshold trigger may not be available in G1 mode.
        For production use, consider external GPIO trigger.
        """
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            try:
                # Capture and check for threshold (10s timeout for large I/Q data)
                response = radar.trigger_capture(timeout=10.0)

                # Re-arm for next capture
                radar.rearm_rolling_buffer(self.pre_trigger_segments)

                capture = self._parse_capture(processor, response)

                if capture is None:
                    time.sleep(self.check_interval)
                    continue

                # Check for threshold speed
                timeline = processor.process_standard(capture)

                peak = timeline.peak_speed
                if peak and peak.is_outbound and peak.speed_mph >= self.speed_threshold_mph:
                    logger.info(
                        "[TRIGGER] Threshold triggered: %.1f mph >= %.1f mph",
                        peak.speed_mph,
                        self.speed_threshold_mph,
                    )
                    self._triggered = True

                    # Brief settling time for ball to clear
                    time.sleep(self.settling_time)

                    # Capture again for complete swing data
                    response = radar.trigger_capture(timeout=10.0)
                    radar.rearm_rolling_buffer(self.pre_trigger_segments)
                    final_capture = self._parse_capture(processor, response)

                    return final_capture or capture

                time.sleep(self.check_interval)

            except Exception as e:
                logger.warning("[TRIGGER] Threshold check error: %s", e, exc_info=True)
                time.sleep(self.check_interval)

        logger.info("[TRIGGER] Threshold trigger timeout")
        return None

    def reset(self):
        """Reset triggered state."""
        self._triggered = False


class ManualTrigger(TriggerStrategy):
    """
    Manual trigger for testing.

    Waits for external signal (e.g., keyboard input, GPIO) before capturing.
    Useful for controlled testing scenarios.
    """

    def __init__(self, pre_trigger_segments: int = 12):
        super().__init__(pre_trigger_segments=pre_trigger_segments)
        self._trigger_requested = False

    def request_trigger(self):
        """Request a capture (called externally)."""
        self._trigger_requested = True

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
    ) -> Optional[IQCapture]:
        """Wait for manual trigger request."""
        start_time = time.time()

        while (time.time() - start_time) < timeout:
            if self._trigger_requested:
                self._trigger_requested = False
                logger.info("[TRIGGER] Manual trigger activated")

                response = radar.trigger_capture(timeout=10.0)
                radar.rearm_rolling_buffer(self.pre_trigger_segments)
                return self._parse_capture(processor, response)

            time.sleep(0.1)

        logger.info("[TRIGGER] Manual trigger timeout")
        return None

    def reset(self):
        """Reset trigger request."""
        self._trigger_requested = False


class SpeedTriggeredCapture(TriggerStrategy):
    """
    Speed-triggered rolling buffer capture per OmniPreSense recommendation.

    This implements the manufacturer's recommended approach for golf:
    1. Run in fast speed detection mode (~150-200Hz report rate)
    2. When outbound speed >25mph detected, immediately switch to rolling buffer
    3. Capture ball impact and flight with S#0 (no pre-trigger history)

    Advantages over polling:
    - Much faster trigger response (~5-6ms vs 300ms+ polling)
    - Captures club speed in speed mode, ball in rolling buffer
    - Minimal data loss during mode switch

    Per manufacturer:
    "You'll lose a little data (~5-6ms) from the initial club speed detection
    time while the sensor determines there's probably a golf swing event to
    capture with the Rolling Buffer. But assuming the club speed detected to
    ball impact is around 20-40ms, that should be ok."
    """

    # The 4,096-sample I/Q response is acquired in ~136.5 ms at 30 ksps,
    # but its JSON representation can take substantially longer to traverse
    # the 57,600-baud UART.  Do not confuse acquisition time with download
    # time: the old 5 s window truncated real board captures before Q arrived.
    CAPTURE_RECEIVE_TIMEOUT_SEC = 30.0

    def __init__(
        self,
        min_trigger_speed_mph: float = 25.0,
        min_ball_speed_mph: float = 35.0,
        trigger_to_capture_delay_ms: float = 15.0,
        pre_trigger_segments: int = 0,  # Speed trigger defaults to 0 (no pre-trigger)
    ):
        """
        Initialize speed-triggered capture.

        Args:
            min_trigger_speed_mph: Speed must be strictly greater than this value
                to trigger capture (default 25mph)
            min_ball_speed_mph: Minimum ball speed to consider valid shot (default 35mph)
            trigger_to_capture_delay_ms: Delay after trigger before capture (default 15ms)
                This allows ball impact to happen before we dump the buffer.
            pre_trigger_segments: Pre-trigger segments (default 0 for speed trigger)
        """
        super().__init__(pre_trigger_segments=pre_trigger_segments)
        self.min_trigger_speed_mph = min_trigger_speed_mph
        self.min_ball_speed_mph = min_ball_speed_mph
        self.trigger_to_capture_delay_ms = trigger_to_capture_delay_ms
        self._last_trigger_speed: float = 0
        self._speed_mode_active = False

    def _configure_speed_mode(self, radar: "OPS243Radar", settle: bool = False) -> None:
        """Put the radar in normal speed-reporting mode for the next swing."""
        self._speed_mode_active = False
        radar.configure_for_speed_trigger(self.min_trigger_speed_mph)
        if radar.serial:
            radar.serial.reset_input_buffer()
        if settle:
            time.sleep(0.1)
        self._speed_mode_active = True

    def _restore_speed_mode(self, radar: "OPS243Radar") -> None:
        """Attempt restoration without hiding a completed capture outcome."""
        try:
            self._configure_speed_mode(radar)
        except Exception:  # pylint: disable=broad-except
            # If the USB device is temporarily unhealthy, preserve the parsed
            # capture/UART failure result and let the next loop retry setup.
            self._speed_mode_active = False
            logger.exception("[TRIGGER] Failed to restore speed trigger mode; will retry")

    def _append_capture_diagnostic(
        self,
        *,
        reason: str,
        response_bytes: int,
        trigger_latency_ms: float,
        timeline: Optional[SpeedTimeline] = None,
    ) -> None:
        """Record a rejected speed-trigger capture for the existing debug UI.

        The speed-mode reading that started the capture is not part of the
        rolling-buffer FFT timeline. Include it in the existing outbound-speed
        diagnostic fields so a club-only test still appears in Diagnostics,
        without being promoted to a shot.
        """
        readings = timeline.readings if timeline else []
        captured_outbound = [reading for reading in readings if reading.is_outbound]
        captured_inbound = [reading for reading in readings if not reading.is_outbound]
        outbound_speeds = [self._last_trigger_speed] + [
            reading.speed_mph for reading in captured_outbound
        ]
        inbound_speeds = [reading.speed_mph for reading in captured_inbound]

        self._append_diagnostic(
            accepted=False,
            reason=reason,
            response_bytes=response_bytes,
            total_readings=1 + len(readings),
            outbound_readings=1 + len(captured_outbound),
            inbound_readings=len(captured_inbound),
            peak_outbound_mph=max(outbound_speeds, default=0),
            peak_inbound_mph=max(inbound_speeds, default=0),
            all_outbound_speeds=outbound_speeds,
            all_inbound_speeds=inbound_speeds,
            peak_outbound_magnitude=max(
                (reading.magnitude for reading in captured_outbound), default=0
            ),
            peak_inbound_magnitude=max(
                (reading.magnitude for reading in captured_inbound), default=0
            ),
            trigger_latency_ms=trigger_latency_ms,
        )

    def wait_for_raw_capture(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
        raw_capture_callback: Optional[RawCaptureCallback] = None,
    ) -> Optional[SpeedTriggeredCaptureResult]:
        """
        Wait for a speed trigger and return its raw I/Q acquisition outcome.

        Unlike :meth:`wait_for_trigger`, this method does not reject a valid
        I/Q buffer because it lacks a ball-speed peak.  Offline collection
        tools need those captures too.
        """
        # Configure for speed trigger mode if needed
        if not self._speed_mode_active:
            self._configure_speed_mode(radar, settle=True)

        start_time = time.time()
        logger.info("[TRIGGER] Waiting for speed trigger > %.1f mph...", self.min_trigger_speed_mph)

        while (time.time() - start_time) < timeout:
            # Non-blocking speed read
            reading = radar.read_speed_nonblocking()

            if reading and reading.speed > self.min_trigger_speed_mph:
                # Speed detected - this is likely the club
                self._last_trigger_speed = reading.speed
                trigger_time = time.time()

                logger.info(
                    "[TRIGGER] Speed trigger: %.1f mph %s detected, switching to rolling buffer...",
                    reading.speed,
                    reading.direction.value,
                )

                response = ""
                result: Optional[SpeedTriggeredCaptureResult] = None
                self._speed_mode_active = False
                try:
                    # Immediately switch to rolling buffer mode. S#0 causes the
                    # OPS243 to collect a full 4096-sample post-trigger buffer.
                    if getattr(self, "board_processing_enabled", False):
                        radar.switch_to_rolling_buffer(process_continuous_sampling=True)
                    else:
                        radar.switch_to_rolling_buffer()

                    # Preserve the established club-to-S! timing. At 30ksps,
                    # the capture then stops automatically after ~136.5ms.
                    delay_sec = self.trigger_to_capture_delay_ms / 1000.0
                    time.sleep(delay_sec)
                    response = radar.trigger_capture(timeout=self.CAPTURE_RECEIVE_TIMEOUT_SEC)
                    capture = self._parse_capture(processor, response)

                    # Capture timing ends when the full UART response has been
                    # parsed.  Save the complete raw buffer before restoring
                    # speed mode or applying the production ball-speed gate.
                    capture_latency_ms = (time.time() - trigger_time) * 1000
                    if capture:
                        result = SpeedTriggeredCaptureResult(
                            capture=capture,
                            trigger_speed_mph=reading.speed,
                            trigger_timestamp=trigger_time,
                            response_bytes=len(response),
                            capture_latency_ms=capture_latency_ms,
                            trigger_threshold_mph=self.min_trigger_speed_mph,
                            board_timeline=capture.board_timeline,
                        )
                        if raw_capture_callback is not None:
                            try:
                                raw_capture_callback(result)
                            except Exception:  # pylint: disable=broad-except
                                # Raw-data persistence is diagnostic only; it
                                # must never prevent the normal kiosk shot path.
                                logger.exception("[TRIGGER] Raw-capture callback failed")
                    else:
                        receive_error = getattr(radar, "last_capture_receive_error", None)
                        if isinstance(receive_error, str) and receive_error:
                            reason = "uart_read_failed"
                        else:
                            reason = "no_response" if not response else "parse_failed"
                        result = SpeedTriggeredCaptureResult(
                            capture=None,
                            trigger_speed_mph=reading.speed,
                            trigger_timestamp=trigger_time,
                            response_bytes=len(response),
                            capture_latency_ms=capture_latency_ms,
                            trigger_threshold_mph=self.min_trigger_speed_mph,
                            failure_reason=reason,
                        )
                finally:
                    # The raw response is local by this point, so restore normal
                    # speed reporting before FFT processing.
                    self._restore_speed_mode(radar)

                # An exception from the radar or parser propagates after the
                # finally block restores speed mode. Otherwise the result is
                # always set by the complete/failed parse branches above.
                assert result is not None
                logger.info("[TRIGGER] Buffer captured %.1fms after trigger", result.capture_latency_ms)
                return result

            # Brief sleep to avoid busy-waiting but stay responsive
            time.sleep(0.002)  # 2ms = 500Hz poll rate

        logger.info("[TRIGGER] Speed trigger timeout")
        return None

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
        raw_capture_callback: Optional[RawCaptureCallback] = None,
    ) -> Optional[IQCapture]:
        """Return a confirmed ball capture for the production monitor.

        The raw acquisition mechanics are shared with the SigMF collector,
        while the monitor retains its existing minimum-ball-speed gate and
        diagnostic entries.
        """
        result = self.wait_for_raw_capture(
            radar,
            processor,
            timeout,
            raw_capture_callback=raw_capture_callback,
        )
        if result is None:
            return None

        if result.capture is None:
            logger.info("[TRIGGER] Speed capture rejected: %s", result.failure_reason)
            self._append_capture_diagnostic(
                reason=result.failure_reason or "parse_failed",
                response_bytes=result.response_bytes,
                trigger_latency_ms=result.capture_latency_ms,
            )
            return None

        capture = result.capture

        # Validate capture has ball speed.
        timeline = processor.process_standard(capture)
        outbound = [
            r
            for r in timeline.readings
            if r.is_outbound and r.speed_mph >= self.min_ball_speed_mph
        ]

        if outbound:
            peak = max(r.speed_mph for r in outbound)
            logger.info("[TRIGGER] Ball detected: %.1f mph in capture", peak)
            return capture

        logger.info("[TRIGGER] No speed >= %.1f mph in capture", self.min_ball_speed_mph)
        self._append_capture_diagnostic(
            reason="no_ball_speed",
            response_bytes=result.response_bytes,
            trigger_latency_ms=result.capture_latency_ms,
            timeline=timeline,
        )
        return None

    def reset(self):
        """Clear the saved club reading without reconfiguring active speed mode."""
        self._last_trigger_speed = 0

    @property
    def last_trigger_speed(self) -> float:
        """Get the speed that triggered the last capture (likely club speed)."""
        return self._last_trigger_speed


class GPIOSoundTrigger(TriggerStrategy):
    """
    GPIO-assisted sound trigger using SparkFun SEN-14262.

    Wiring: SEN-14262 GATE → Pi GPIO pin (default: GPIO17, physical pin 11)

    IMPORTANT: Rolling buffer mode must be configured BEFORE using this trigger.
    Call radar.configure_for_rolling_buffer() or radar.enter_rolling_buffer_mode()
    before calling wait_for_trigger().

    This is a workaround for voltage level issues where the SEN-14262 GATE
    doesn't reach the 3.3V threshold required by HOST_INT. The Pi GPIO has
    a lower voltage threshold (~1.8V vs ~2.0V), making it more reliable.

    How it works:
        1. Pi GPIO detects rising edge on GATE (lower voltage threshold)
        2. Python sends S! command to radar to trigger buffer dump
        3. Script reads and processes I/Q data

    Requires: gpiozero library (uv pip install gpiozero lgpio)
    """

    def __init__(
        self,
        gpio_pin: int = 17,
        pre_trigger_segments: int = 32,
        debounce_ms: int = 20,
    ):
        """
        Initialize GPIO-assisted sound trigger.

        Args:
            gpio_pin: GPIO pin (BCM numbering) for GATE input (default: 17)
            pre_trigger_segments: Number of pre-trigger segments for S# command.
                Each segment = 128 samples = ~4.27ms at 30ksps.
                Default 20 gives ~85ms pre-trigger, ~51ms post-trigger.
                NOTE: This is passed to enter_rolling_buffer_mode() by the caller.
                The trigger does NOT configure rolling buffer mode itself.
            debounce_ms: Debounce time in ms to ignore rapid triggers (default: 200)
        """
        super().__init__(pre_trigger_segments=pre_trigger_segments)
        self.gpio_pin = gpio_pin
        self.debounce_ms = debounce_ms
        self._button = None
        self._trigger_event = {"triggered": False, "edge_time": 0.0}
        self._gpio_initialized = False

    def _init_gpio(self):
        """Initialize GPIO - called lazily on first wait_for_trigger."""
        if self._gpio_initialized:
            return True

        try:
            from gpiozero import Button  # pylint: disable=import-outside-toplevel

            # Must precede the first gpiozero device: on a Pi 5 gpiozero's own
            # pin-factory auto-detection fails outright. See gpio_factory.
            from ..gpio_factory import (  # pylint: disable=import-outside-toplevel
                ensure_lgpio_pin_factory,
            )

            ensure_lgpio_pin_factory()
        except ImportError:
            logger.error(
                "[TRIGGER] gpiozero not available. Install with: uv pip install gpiozero lgpio"
            )
            return False
        except RuntimeError as exc:
            logger.error("[TRIGGER] %s", exc)
            return False

        def on_trigger():
            self._trigger_event["edge_time"] = time.time()
            self._trigger_event["triggered"] = True

        self._button = Button(self.gpio_pin, pull_up=False, bounce_time=self.debounce_ms / 1000.0)
        self._button.when_pressed = on_trigger
        self._gpio_initialized = True

        logger.info(
            "[TRIGGER] GPIO%d configured for sound trigger (debounce=%dms)",
            self.gpio_pin,
            self.debounce_ms,
        )
        return True

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
    ) -> Optional[IQCapture]:
        """
        Wait for GPIO sound trigger and capture buffer.

        PREREQUISITE: Rolling buffer mode must already be configured via
        radar.configure_for_rolling_buffer() or radar.enter_rolling_buffer_mode().

        Unlike direct SoundTrigger (HOST_INT), this uses Pi GPIO to detect
        the SEN-14262 GATE signal, then sends S! to trigger the capture.
        """
        if not self._init_gpio():
            logger.error("[TRIGGER] GPIO initialization failed")
            return None

        logger.info(
            "[TRIGGER] Waiting for GPIO sound trigger on GPIO%d (timeout=%.0fs, S#%s)...",
            self.gpio_pin,
            timeout,
            self.pre_trigger_segments,
        )

        start_time = time.time()
        self._trigger_event["triggered"] = False

        while (time.time() - start_time) < timeout:
            if self._trigger_event["triggered"]:
                edge_time = self._trigger_event["edge_time"]
                self._trigger_event["triggered"] = False

                # Measure edge-to-S! latency (from GPIO callback to now)
                trigger_latency = (time.time() - edge_time) * 1000
                logger.info(
                    "[TRIGGER] GPIO edge detected on GPIO%d (%.1fms ago), sending S! trigger...",
                    self.gpio_pin,
                    trigger_latency,
                )
                response = radar.trigger_capture(timeout=5.0)

                if not response:
                    logger.warning(
                        "[TRIGGER] No response from radar after S! (%.1fms after edge)",
                        trigger_latency,
                    )
                    self._append_diagnostic(
                        accepted=False,
                        reason="no_response",
                        response_bytes=0,
                        trigger_latency_ms=trigger_latency,
                    )
                    radar.rearm_rolling_buffer(self.pre_trigger_segments)
                    logger.debug("[TRIGGER] Discarding GPIO edges during re-arm")
                    self._trigger_event["triggered"] = False  # discard edges during rearm
                    continue

                response_len = len(response)
                logger.info(
                    "[TRIGGER] Capture received, %d bytes (S! sent %.1fms after edge)",
                    response_len,
                    trigger_latency,
                )
                if response_len < 5000:
                    logger.debug("[TRIGGER] Response content: %s", repr(response))
                else:
                    logger.debug("[TRIGGER] Response preview: %s...", repr(response[:500]))

                # Re-arm for next capture
                radar.rearm_rolling_buffer(self.pre_trigger_segments)
                logger.debug("[TRIGGER] Discarding GPIO edges during re-arm")
                self._trigger_event["triggered"] = False  # discard edges during rearm

                capture = self._parse_capture(processor, response)

                if not capture:
                    logger.warning("[TRIGGER] Failed to parse capture (%d bytes)", response_len)
                    self._append_diagnostic(
                        accepted=False,
                        reason="parse_failed",
                        response_bytes=response_len,
                        trigger_latency_ms=trigger_latency,
                    )
                    continue

                # Quick validation: does the capture contain any real swing data?
                summary = self._summarize_capture_activity(processor, capture)

                if not summary["valid_outbound_count"]:
                    logger.info(
                        "[TRIGGER] GPIO trigger rejected — no outbound speed >= %.0f mph "
                        "(peak=%.1f mph, %d readings)",
                        self.MIN_VALID_OUTBOUND_MPH,
                        summary["peak_outbound_mph"],
                        summary["total_readings"],
                    )
                    self._append_activity_diagnostic(
                        summary,
                        accepted=False,
                        reason="no_outbound_speed",
                        response_bytes=response_len,
                        trigger_latency_ms=trigger_latency,
                    )
                    continue

                logger.info(
                    "[TRIGGER] GPIO trigger accepted — peak %.1f mph, %d outbound readings",
                    summary["valid_peak_outbound_mph"],
                    summary["valid_outbound_count"],
                )
                self._append_activity_diagnostic(
                    summary,
                    accepted=True,
                    reason="accepted",
                    response_bytes=response_len,
                    trigger_latency_ms=trigger_latency,
                )

                return capture

            time.sleep(0.001)  # 1ms poll interval — latency-critical path

        logger.info("[TRIGGER] GPIO sound trigger timeout — no trigger received")
        return None

    def reset(self):
        """Reset trigger state."""
        self._trigger_event["triggered"] = False

    def cleanup(self):
        """Clean up GPIO resources."""
        if self._button:
            self._button.close()
            self._button = None
            self._gpio_initialized = False


class SoundTrigger(TriggerStrategy):
    """
    Hardware sound trigger using SparkFun SEN-14262.

    IMPORTANT: Rolling buffer mode must be configured BEFORE using this trigger.
    Call radar.configure_for_rolling_buffer() or radar.enter_rolling_buffer_mode()
    before calling wait_for_trigger().

    Wiring: SEN-14262 GATE → OPS243-A J3 Pin 3 (HOST_INT)
    The GATE output goes HIGH on loud sound (club impact).
    OPS243-A uses rising edge detection on HOST_INT as trigger.

    No software trigger (S!) needed — the radar triggers itself
    via hardware. We just need to wait for data to appear on serial.

    Note: If GATE voltage doesn't reach 3.3V threshold, use GPIOSoundTrigger
    instead, which uses Pi GPIO (lower threshold) + software S! trigger.
    """

    CLOCK_SYNC_SAMPLES = 36
    CLOCK_SYNC_MAX_ROLLOVER_UNCERTAINTY_MS = 40.0
    CLOCK_SYNC_MAX_TIMEOUT_READ_MS = 50.0
    CLOCK_SYNC_MAX_FALLBACK_AGE_S = 60.0

    def __init__(
        self,
        pre_trigger_segments: int = 12,
    ):
        """
        Initialize sound trigger.

        Args:
            pre_trigger_segments: Number of pre-trigger segments for S# command.
                Each segment = 128 samples = ~4.27ms at 30ksps.
                Default 12 gives ~51ms pre-trigger, ~85ms post-trigger.
                NOTE: This is passed to enter_rolling_buffer_mode() by the caller.
                The trigger does NOT configure rolling buffer mode itself.
        """
        super().__init__(pre_trigger_segments=pre_trigger_segments)

    @staticmethod
    def _clock_sync_last_read_host_time(clock_sync: dict) -> Optional[float]:
        """Return the host time of the last C? read in a clock-sync summary."""
        reads = clock_sync.get("reads") or []
        if not reads or not isinstance(reads[-1], dict):
            return None
        return reads[-1].get("host_after") or reads[-1].get("host_mid")

    @classmethod
    def _clock_sync_age_s(cls, clock_sync: dict) -> Optional[float]:
        """Return age in seconds for a clock-sync summary."""
        last_host_time = cls._clock_sync_last_read_host_time(clock_sync)
        if last_host_time is None:
            return None
        try:
            return time.time() - float(last_host_time)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _clock_sync_quality(cls, clock_sync: object) -> tuple[bool, str]:
        """Return whether a clock sync is trustworthy enough for shot timing."""
        if not isinstance(clock_sync, dict):
            return False, "missing"

        if not clock_sync.get("usable_for_trigger_timestamps"):
            return False, f"unusable_method:{clock_sync.get('clock_sync_method', 'unknown')}"

        if clock_sync.get("best_offset_s") is None:
            return False, "missing_best_offset"

        reads = clock_sync.get("reads") or []
        slow_invalid_reads = [
            read
            for read in reads
            if isinstance(read, dict)
            and read.get("radar_clock_s") is None
            and (read.get("read_latency_ms") or 0.0) >= cls.CLOCK_SYNC_MAX_TIMEOUT_READ_MS
        ]
        if slow_invalid_reads:
            return False, f"timeout_reads:{len(slow_invalid_reads)}"

        method = clock_sync.get("clock_sync_method")
        if method == "integer_rollover":
            uncertainty = clock_sync.get("rollover_uncertainty_ms")
            if uncertainty is None:
                return False, "missing_rollover_uncertainty"
            try:
                if float(uncertainty) > cls.CLOCK_SYNC_MAX_ROLLOVER_UNCERTAINTY_MS:
                    return False, f"rollover_uncertainty:{float(uncertainty):.1f}ms"
            except (TypeError, ValueError):
                return False, "invalid_rollover_uncertainty"
            return True, "valid_integer_rollover"

        if method == "fractional_clock":
            return True, "valid_fractional_clock"

        return False, f"unsupported_method:{method or 'unknown'}"

    @classmethod
    def _clock_sync_summary_for_log(cls, clock_sync: object) -> Optional[dict]:
        """Return a compact JSONL-safe summary of a clock-sync candidate."""
        if not isinstance(clock_sync, dict):
            return None
        valid, reason = cls._clock_sync_quality(clock_sync)
        return {
            "valid": valid,
            "reason": reason,
            "source": clock_sync.get("source"),
            "samples": clock_sync.get("samples"),
            "valid_samples": clock_sync.get("valid_samples"),
            "clock_sync_method": clock_sync.get("clock_sync_method"),
            "best_offset_s": clock_sync.get("best_offset_s"),
            "raw_best_offset_s": clock_sync.get("raw_best_offset_s"),
            "best_read_latency_ms": clock_sync.get("best_read_latency_ms"),
            "offset_spread_ms": clock_sync.get("offset_spread_ms"),
            "rollover_uncertainty_ms": clock_sync.get("rollover_uncertainty_ms"),
            "age_s": (
                round(cls._clock_sync_age_s(clock_sync), 3)
                if cls._clock_sync_age_s(clock_sync) is not None
                else None
            ),
        }

    def _select_clock_sync_for_capture(
        self,
        radar: "OPS243Radar",
        capture: IQCapture,
    ) -> Optional[dict]:
        """Choose and apply the best OPS clock sync for this capture."""
        previous_sync = getattr(radar, "last_clock_sync", None)
        previous_valid, previous_reason = self._clock_sync_quality(previous_sync)
        previous_age_s = (
            self._clock_sync_age_s(previous_sync) if isinstance(previous_sync, dict) else None
        )

        fresh_sync = None
        fresh_error = None
        if hasattr(radar, "read_clock_sync"):
            try:
                fresh_sync = radar.read_clock_sync(
                    samples=self.CLOCK_SYNC_SAMPLES,
                    store=False,
                )
                if isinstance(fresh_sync, dict):
                    fresh_sync["source"] = "per_shot"
            except Exception as exc:  # pylint: disable=broad-except
                fresh_error = str(exc)
                logger.warning("[TRIGGER] Per-shot OPS clock sync failed: %s", exc, exc_info=True)

        fresh_valid, fresh_reason = self._clock_sync_quality(fresh_sync)

        selected_sync = None
        selected_source = "first_byte"
        selected_reason = "no_valid_clock_sync"

        if fresh_valid and isinstance(fresh_sync, dict):
            selected_sync = fresh_sync
            selected_source = "fresh"
            selected_reason = fresh_reason
            radar.last_clock_sync = fresh_sync
        elif (
            previous_valid
            and isinstance(previous_sync, dict)
            and previous_age_s is not None
            and previous_age_s <= self.CLOCK_SYNC_MAX_FALLBACK_AGE_S
        ):
            selected_sync = previous_sync
            selected_source = "previous"
            selected_reason = (
                f"fresh_rejected:{fresh_reason};previous_age:{previous_age_s:.1f}s"
            )
        elif previous_valid and previous_age_s is not None:
            selected_reason = (
                f"fresh_rejected:{fresh_reason};previous_too_old:{previous_age_s:.1f}s"
            )
        elif previous_reason != "missing":
            selected_reason = f"fresh_rejected:{fresh_reason};previous_rejected:{previous_reason}"
        elif fresh_error:
            selected_reason = f"fresh_error:{fresh_error}"
        else:
            selected_reason = f"fresh_rejected:{fresh_reason}"

        selected_offset_s = None
        selected_age_s = None
        if isinstance(selected_sync, dict):
            selected_offset_s = selected_sync.get("best_offset_s")
            selected_age_s = self._clock_sync_age_s(selected_sync)
            try:
                capture.apply_trigger_timestamp_from_clock_sync(float(selected_offset_s))
            except (TypeError, ValueError):
                logger.warning(
                    "[TRIGGER] Ignoring invalid selected OPS clock-sync offset: %r",
                    selected_offset_s,
                )
                selected_sync = None
                selected_source = "first_byte"
                selected_reason = "selected_offset_invalid"
                selected_offset_s = None

        previous_offset_s = (
            previous_sync.get("best_offset_s") if isinstance(previous_sync, dict) else None
        )
        fresh_offset_s = fresh_sync.get("best_offset_s") if isinstance(fresh_sync, dict) else None

        selection_log = {
            "selection": selected_source,
            "selection_reason": selected_reason,
            "selected_offset_s": selected_offset_s,
            "selected_age_s": round(selected_age_s, 3) if selected_age_s is not None else None,
            "fresh": self._clock_sync_summary_for_log(fresh_sync),
            "previous": self._clock_sync_summary_for_log(previous_sync),
            "fresh_error": fresh_error,
            "fresh_delta_from_previous_ms": (
                round((fresh_offset_s - previous_offset_s) * 1000.0, 3)
                if fresh_offset_s is not None and previous_offset_s is not None
                else None
            ),
        }

        if selected_sync is not None:
            logger.info(
                "[TRIGGER] OPS clock sync selected: %s offset=%.6fs age=%sms "
                "(fresh=%s, previous=%s, delta=%sms)",
                selected_source,
                selected_offset_s,
                "n/a" if selected_age_s is None else f"{selected_age_s:.1f}",
                fresh_reason,
                previous_reason,
                "n/a"
                if selection_log["fresh_delta_from_previous_ms"] is None
                else f"{selection_log['fresh_delta_from_previous_ms']:.1f}",
            )
        else:
            logger.info(
                "[TRIGGER] OPS clock sync fallback to first-byte timing: %s",
                selected_reason,
            )

        return selection_log

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
    ) -> Optional[IQCapture]:
        """
        Wait for hardware sound trigger and capture buffer.

        PREREQUISITE: Rolling buffer mode must already be configured via
        radar.configure_for_rolling_buffer() or radar.enter_rolling_buffer_mode().

        Unlike other triggers, no S! command is sent. The radar's
        HOST_INT pin receives the trigger from the SEN-14262 GATE
        output, causing the radar to dump its rolling buffer automatically.
        We just block on serial read waiting for the I/Q data to arrive.
        """
        logger.info("[TRIGGER] Waiting for sound trigger (timeout=%.0fs)...", timeout)

        response = self._wait_for_hardware_trigger(radar, timeout)

        if not response:
            logger.info("[TRIGGER] Sound trigger timeout — no hardware trigger received")
            return None

        response_len = len(response)
        logger.info("[TRIGGER] Sound trigger fired, %d bytes received", response_len)
        first_byte_timestamp = getattr(
            radar,
            "last_hardware_trigger_first_byte_timestamp",
            None,
        )

        # ORDERING MATTERS: after its dump the radar sits idle, where
        # HOST_INT pulses are harmless. A real shot produces a second loud
        # sound ~1s later (ball hitting the net); if we re-arm first, that
        # sound starts a new dump exactly when the clock-sync exchange
        # writes to the port — a two-way serial deadlock observed in the
        # field (capture thread wedged in serial.write, radar frozen
        # mid-dump). So: do all wire-talk (clock sync) while idle, and
        # re-arm LAST, when we are about to go back to reading.

        capture = self._parse_capture(
            processor,
            response,
            first_byte_timestamp=first_byte_timestamp,
        )

        if not capture:
            radar.rearm_rolling_buffer(self.pre_trigger_segments)
            logger.warning("[TRIGGER] Sound trigger parse failed (%d bytes received)", response_len)
            self._append_diagnostic(
                accepted=False,
                reason="parse_failed",
                response_bytes=response_len,
            )
            return None

        if first_byte_timestamp is not None and capture.first_byte_timestamp is None:
            capture.first_byte_timestamp = float(first_byte_timestamp)

        # Quick validation: does the capture contain any real swing data?
        # At a driving range, a nearby player's impact sound can trip the
        # trigger even though nothing was moving in front of our radar.
        # Discard these false triggers immediately so we re-arm fast.
        summary = self._summarize_capture_activity(processor, capture)

        if not summary["valid_outbound_count"]:
            # False trigger: re-arm immediately (no clock sync) so the
            # next real swing isn't missed.
            radar.rearm_rolling_buffer(self.pre_trigger_segments)
            logger.info(
                "[TRIGGER] Sound trigger rejected — no outbound speed >= %.0f mph "
                "(peak=%.1f mph, %d readings)",
                self.MIN_VALID_OUTBOUND_MPH,
                summary["peak_outbound_mph"],
                summary["total_readings"],
            )
            self._append_activity_diagnostic(
                summary,
                accepted=False,
                reason="no_outbound_speed",
                response_bytes=response_len,
            )
            return None

        # Accepted: talk on the wire while the radar is still idle, then
        # re-arm as the last serial action before returning to the reader.
        self._select_clock_sync_for_capture(radar, capture)
        radar.rearm_rolling_buffer(self.pre_trigger_segments)

        if capture.first_byte_timestamp is not None and capture.trigger_timestamp is None:
            capture.apply_trigger_timestamp_from_first_byte()

        if capture.trigger_timestamp is not None and capture.first_byte_timestamp is not None:
            logger.info(
                "[TRIGGER] Sound trigger wall time %.3f "
                "(source=%s, first byte %.3f, post-trigger %.1fms)",
                capture.trigger_timestamp,
                capture.trigger_timestamp_source or "unknown",
                capture.first_byte_timestamp,
                capture.post_trigger_duration_ms,
            )

        logger.info(
            "[TRIGGER] Sound trigger accepted — peak %.1f mph, %d outbound readings",
            summary["valid_peak_outbound_mph"],
            summary["valid_outbound_count"],
        )
        self._append_activity_diagnostic(
            summary,
            accepted=True,
            reason="accepted",
            response_bytes=response_len,
        )

        return capture

    def reset(self):
        """Reset trigger state."""
        pass  # No state to reset


class HardwareTriggeredCapture(SoundTrigger):
    """Use the OPS243's built-in speed trigger and rolling-buffer dump.

    The radar is configured once with ``STn`` and captures its own rolling
    buffer when that threshold is crossed.  Python only receives the completed
    dump, parses it, and re-arms the board with ``GC`` for the next shot.
    """

    def __init__(
        self,
        trigger_threshold_mph: float = 25.0,
        min_ball_speed_mph: float = 35.0,
        pre_trigger_segments: int = 16,
        trigger_magnitude: int = 40,
        sample_rate_ksps: int = 30,
    ):
        super().__init__(pre_trigger_segments=pre_trigger_segments)
        self.trigger_threshold_mph = trigger_threshold_mph
        if not 1 <= trigger_magnitude <= 2000:
            raise ValueError("Trigger magnitude must be between 1 and 2000")
        self.trigger_magnitude = trigger_magnitude
        self.min_ball_speed_mph = min_ball_speed_mph
        self.sample_rate_ksps = sample_rate_ksps

    def wait_for_raw_capture(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
        raw_capture_callback: Optional[RawCaptureCallback] = None,
    ) -> Optional[RawCaptureResult]:
        """Wait for one board-triggered I/Q dump without applying a ball gate."""
        logger.info(
            "[TRIGGER] Waiting for OPS hardware trigger > %.1f mph (timeout=%.0fs)...",
            self.trigger_threshold_mph,
            timeout,
        )
        started_at = time.time()
        response = self._wait_for_hardware_trigger(radar, timeout)
        if not response:
            logger.info("[TRIGGER] OPS hardware trigger timeout — no dump received")
            return None

        response_bytes = len(response)
        first_byte_timestamp = getattr(radar, "last_hardware_trigger_first_byte_timestamp", None)
        capture = self._parse_capture(
            processor,
            response,
            first_byte_timestamp=first_byte_timestamp,
        )

        # A received response proves a board trigger fired, even when it is
        # malformed.  Parse before this re-arm so the raw response is retained.
        radar.rearm_internal_speed_trigger(self.sample_rate_ksps)

        capture_latency_ms = (time.time() - started_at) * 1000.0
        if capture is None:
            self._append_diagnostic(
                accepted=False,
                reason="parse_failed",
                response_bytes=response_bytes,
                trigger_latency_ms=capture_latency_ms,
            )
            return RawCaptureResult(
                capture=None,
                trigger_speed_mph=None,
                trigger_timestamp=time.time(),
                response_bytes=response_bytes,
                capture_latency_ms=capture_latency_ms,
                trigger_threshold_mph=self.trigger_threshold_mph,
                failure_reason="parse_failed",
            )

        # Re-use the hardware-trigger timestamp refinement shared by the
        # existing sound path.  Its first-byte estimate remains the fallback.
        self._select_clock_sync_for_capture(radar, capture)
        trigger_timestamp = capture.trigger_timestamp or time.time()
        result = RawCaptureResult(
            capture=capture,
            trigger_speed_mph=None,
            trigger_timestamp=trigger_timestamp,
            response_bytes=response_bytes,
            capture_latency_ms=capture_latency_ms,
            trigger_threshold_mph=self.trigger_threshold_mph,
            board_timeline=capture.board_timeline,
        )
        if raw_capture_callback is not None:
            try:
                raw_capture_callback(result)
            except Exception:  # pylint: disable=broad-except
                logger.exception("[TRIGGER] Raw-capture callback failed")
        return result

    def wait_for_trigger(
        self,
        radar: "OPS243Radar",
        processor: RollingBufferProcessor,
        timeout: float = 30.0,
        raw_capture_callback: Optional[RawCaptureCallback] = None,
    ) -> Optional[IQCapture]:
        """Return a board-triggered capture that passes the legacy ball gate."""
        result = self.wait_for_raw_capture(
            radar,
            processor,
            timeout,
            raw_capture_callback=raw_capture_callback,
        )
        if result is None:
            return None
        if result.capture is None:
            return None

        capture = result.capture
        summary = self._summarize_capture_activity(processor, capture)
        valid_outbound = [
            speed
            for speed in summary["all_outbound_speeds"]
            if speed >= self.min_ball_speed_mph
        ]
        if not valid_outbound:
            self._append_activity_diagnostic(
                summary,
                accepted=False,
                reason="no_ball_speed",
                response_bytes=result.response_bytes,
                trigger_latency_ms=result.capture_latency_ms,
            )
            logger.info(
                "[TRIGGER] OPS hardware capture rejected — no speed >= %.1f mph",
                self.min_ball_speed_mph,
            )
            return None

        # Do not emit an accepted diagnostic here.  This only establishes that
        # the board delivered a plausible raw capture; the monitor still has
        # to process it and validate the resulting Shot.  Emitting one here
        # and another after that validation made the UI count every hardware
        # dump twice.  Rejections above remain diagnostics because no final
        # Shot diagnostic will follow them.
        return capture


def create_trigger(trigger_type: str = "hardware", **kwargs) -> TriggerStrategy:
    """
    Factory function to create trigger strategy.

    Args:
        trigger_type: "hardware" (recommended), "speed", "polling",
                      "threshold", "manual", "sound", or "sound-gpio"
        **kwargs: Arguments passed to trigger constructor

    Returns:
        Configured TriggerStrategy instance

    Trigger types:
        - "hardware": OPS243 built-in speed trigger drives the rolling-buffer
          dump directly.
        - "speed": Fast host-polled speed detection triggers rolling buffer capture.
                   Recommended by OmniPreSense for golf. ~5-6ms response time.
        - "polling": Continuously capture and check for activity. Simple but slow.
        - "threshold": Speed threshold triggers capture. Less efficient than "speed".
        - "manual": External trigger for testing.
        - "sound": Hardware sound trigger via SparkFun SEN-14262 GATE → HOST_INT.
                   Requires GATE voltage to reach 3.3V threshold.
        - "sound-gpio": GPIO-assisted sound trigger via Pi GPIO + S! command.
                        Use when GATE voltage doesn't reach HOST_INT threshold.
                        Requires gpiozero library.
    """
    triggers = {
        "hardware": HardwareTriggeredCapture,
        "speed": SpeedTriggeredCapture,
        "polling": PollingTrigger,
        "threshold": ThresholdTrigger,
        "manual": ManualTrigger,
        "sound": SoundTrigger,
        "sound-gpio": GPIOSoundTrigger,
    }

    if trigger_type not in triggers:
        raise ValueError(
            f"Unknown trigger type: {trigger_type}. Available: {list(triggers.keys())}"
        )

    return triggers[trigger_type](**kwargs)
