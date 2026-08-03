"""Low-cost static visualizations for OPS243 board-processing output.

The renderer intentionally uses only the Python standard library. A shot
preview is a portable SVG, so it can be opened directly on the Raspberry Pi
without starting a web server or running a plotting runtime. The session
index is static HTML and contains no JavaScript.
"""

from __future__ import annotations

import html
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .rolling_buffer.trigger import RawCaptureResult
from .rolling_buffer.types import BoardProcessedTimeline

SHOT_PREVIEW_FILENAME = "shot-preview.svg"
SESSION_INDEX_FILENAME = "index.html"

# Fixed categorical colors make the board's five array positions easy to
# distinguish. The board orders each multi-target array strongest first.
ARRAY_POSITION_COLORS = (
    "#0072b2",  # Mag1: blue
    "#e69f00",  # Mag2: orange
    "#009e73",  # Mag3: green
    "#cc79a7",  # Mag4: pink
    "#d55e00",  # Mag5: vermillion
)


def _timeline_for_result(result: RawCaptureResult) -> BoardProcessedTimeline:
    if result.board_timeline is not None:
        return result.board_timeline
    if result.capture is not None and result.capture.board_timeline is not None:
        return result.capture.board_timeline
    return BoardProcessedTimeline(
        sample_time=result.capture.sample_time if result.capture is not None else None,
        trigger_time=result.capture.trigger_time if result.capture is not None else None,
    )


def _paired_points(timeline: BoardProcessedTimeline) -> list[tuple[float, float, float, int]]:
    """Flatten board records into (relative-ms, absolute-speed, magnitude, index).

    A malformed record with unequal speed/magnitude arrays is truncated to the
    valid paired prefix. Invalid individual values are skipped, allowing a
    useful plot even when one board record is incomplete.
    """
    points: list[tuple[float, float, float, int]] = []
    for record in timeline.records:
        if timeline.sample_time is None:
            continue
        try:
            offset_ms = (float(record.timestamp) - float(timeline.sample_time)) * 1000.0
        except (TypeError, ValueError):
            continue
        for array_index, (speed, magnitude) in enumerate(zip(record.speeds, record.magnitudes), start=1):
            try:
                x_value = float(offset_ms)
                y_value = abs(float(speed))
                color_value = float(magnitude)
            except (TypeError, ValueError):
                continue
            if not all(math.isfinite(value) for value in (x_value, y_value, color_value)):
                continue
            points.append((x_value, y_value, color_value, array_index))
    return points


def board_point_count(result: RawCaptureResult) -> int:
    """Return the number of valid paired board speed/magnitude values."""
    return len(_paired_points(_timeline_for_result(result)))


def _format_number(value: float, digits: int = 1) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.{digits}f}"


def _nice_upper_bound(value: float) -> float:
    """Choose a readable positive upper axis bound."""
    if not math.isfinite(value) or value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    scale = 10**exponent
    for step in (1.0, 2.0, 5.0, 10.0):
        bound = step * scale
        if bound >= value:
            return bound
    return 10.0 * scale


def _svg_text(
    x: float,
    y: float,
    text: str,
    *,
    size: int = 13,
    anchor: str = "start",
    weight: str = "400",
    fill: str = "#1f2937",
) -> str:
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" font-family="system-ui,-apple-system,Segoe UI,sans-serif" '
        f'font-size="{size}px" font-weight="{weight}" text-anchor="{anchor}" fill="{fill}">'
        f"{html.escape(text)}</text>"
    )


def _svg_line(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    stroke: str = "#d1d5db",
    width: float = 1.0,
    dash: str | None = None,
) -> str:
    dash_attribute = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'stroke="{stroke}" stroke-width="{width:.2f}"{dash_attribute}/>'
    )


def _ticks(upper: float, count: int = 5) -> list[float]:
    return [upper * index / count for index in range(count + 1)]


def render_shot_preview_svg(
    result: RawCaptureResult,
    *,
    title: str = "OPS243 board speed / magnitude",
) -> bytes:
    """Render one capture as an absolute-speed/magnitude scatter plot."""
    if result.capture is None:
        raise ValueError("A complete I/Q capture is required for a shot preview")

    timeline = _timeline_for_result(result)
    points = _paired_points(timeline)
    unit = timeline.units or "native units"
    width, height = 1200, 720
    left, right, top, bottom = 92, 184, 92, 82
    plot_x0, plot_x1 = left, width - right
    plot_y0, plot_y1 = top, height - bottom
    plot_width, plot_height = plot_x1 - plot_x0, plot_y1 - plot_y0

    capture_duration_ms = result.capture.duration_ms
    max_point_x = max((point[0] for point in points), default=0.0)
    x_min = min(0.0, min((point[0] for point in points), default=0.0))
    x_max = _nice_upper_bound(max(capture_duration_ms, max_point_x, 1.0))
    if x_min < 0:
        x_min = math.floor(x_min / 10.0) * 10.0
    max_speed = max((point[1] for point in points), default=0.0)
    y_max = _nice_upper_bound(max(max_speed, 1.0))

    def x_pixel(value: float) -> float:
        return plot_x0 + (value - x_min) / (x_max - x_min) * plot_width

    def y_pixel(value: float) -> float:
        return plot_y1 - value / y_max * plot_height

    record_count = timeline.record_count
    point_count = len(points)
    subtitle = (
        f"Absolute speed; colors Mag1 highest → Mag5 lowest; {record_count} records, "
        f"{point_count} paired values; unit: {unit}"
    )
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        _svg_text(left, 38, title, size=23, weight="700"),
        _svg_text(left, 63, subtitle, size=13, fill="#4b5563"),
        f'<rect x="{plot_x0:.2f}" y="{plot_y0:.2f}" width="{plot_width:.2f}" '
        f'height="{plot_height:.2f}" fill="#f9fafb" stroke="#9ca3af"/>',
    ]

    for tick in _ticks(x_max, 5):
        px = x_pixel(tick)
        parts.append(_svg_line(px, plot_y0, px, plot_y1, dash="3 4"))
        parts.append(_svg_text(px, plot_y1 + 25, _format_number(tick), anchor="middle", size=12, fill="#374151"))
    for tick in _ticks(y_max, 5):
        py = y_pixel(tick)
        parts.append(_svg_line(plot_x0, py, plot_x1, py, dash="3 4"))
        parts.append(_svg_text(plot_x0 - 12, py + 4, _format_number(tick), anchor="end", size=12, fill="#374151"))

    parts.extend(
        [
            _svg_text(
                (plot_x0 + plot_x1) / 2,
                height - 27,
                "Capture-relative time (ms)",
                anchor="middle",
                size=14,
                weight="600",
            ),
            f'<g transform="translate(24 {(plot_y0 + plot_y1) / 2:.2f}) rotate(-90)">',
            _svg_text(0, 0, f"Absolute speed ({unit})", anchor="middle", size=14, weight="600"),
            "</g>",
        ]
    )

    if points:
        for x_value, y_value, magnitude, array_index in points:
            px, py = x_pixel(x_value), y_pixel(y_value)
            color = ARRAY_POSITION_COLORS[array_index - 1] if 1 <= array_index <= len(ARRAY_POSITION_COLORS) else "#4b5563"
            label = f"Mag{array_index}, time {x_value:.3f} ms, speed {y_value:.3f} {unit}, magnitude {magnitude:.3f}"
            parts.append(
                f'<circle cx="{px:.2f}" cy="{py:.2f}" r="3.7" fill="{color}" '
                f'stroke="#111827" stroke-width="0.65" data-array-index="{array_index}" '
                f'aria-label="{html.escape(label)}"/>'
            )
    else:
        parts.append(
            _svg_text(
                (plot_x0 + plot_x1) / 2,
                (plot_y0 + plot_y1) / 2,
                "No valid board speed/magnitude pairs",
                anchor="middle",
                size=16,
                fill="#6b7280",
            )
        )

    legend_x, legend_y = plot_x1 + 34, plot_y0 + 40
    parts.append(_svg_text(legend_x, legend_y - 18, "Array position", size=13, weight="600"))
    parts.append(_svg_text(legend_x, legend_y - 2, "highest magnitude → lowest", size=11, fill="#4b5563"))
    for array_index, color in enumerate(ARRAY_POSITION_COLORS, start=1):
        row_y = legend_y + 24 + (array_index - 1) * 42
        parts.append(f'<rect x="{legend_x:.2f}" y="{row_y - 14:.2f}" width="20" height="20" rx="3" fill="{color}" stroke="#111827" stroke-width="0.7"/>')
        parts.append(_svg_text(legend_x + 30, row_y + 1, f"Mag{array_index}", size=12, weight="600"))
        parts.append(_svg_text(legend_x + 30, row_y + 17, f"array position {array_index}", size=10, fill="#4b5563"))
    parts.append(
        _svg_text(
            left,
            height - 5,
            "Source: board-speed-magnitude.jsonl; board-native values are preserved in the companion file.",
            size=11,
            fill="#6b7280",
        )
    )
    parts.append("</svg>")
    return ("\n".join(parts) + "\n").encode("utf-8")


def _session_rows(session_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for hit_dir in sorted(session_dir.glob("hit-*")):
        meta_path = hit_dir / "capture.sigmf-meta"
        preview_path = hit_dir / SHOT_PREVIEW_FILENAME
        if not meta_path.is_file() or not preview_path.is_file():
            continue
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            capture = (metadata.get("captures") or [{}])[0]
        except (OSError, ValueError, TypeError):
            continue
        rows.append(
            {
                "name": hit_dir.name,
                "shot_number": capture.get("openflight:shot_number"),
                "trigger_type": capture.get("openflight:trigger_type"),
                "trigger_speed": capture.get("openflight:trigger_speed_mph"),
                "trigger_threshold": capture.get("openflight:trigger_threshold_mph"),
                "ball_speed": capture.get("openflight:ball_speed_mph"),
                "club_speed": capture.get("openflight:club_speed_mph"),
                "spin_rpm": capture.get("openflight:spin_rpm"),
                "spin_quality": capture.get("openflight:spin_quality"),
                "carry": capture.get(
                    "openflight:estimated_carry_ui_yards",
                    capture.get("openflight:carry_yards"),
                ),
                "unit": capture.get("openflight:board_unit") or "native units",
                "records": capture.get("openflight:board_record_count", 0),
                "points": capture.get("openflight:board_point_count", "—"),
                "peak_speed": capture.get("openflight:board_peak_speed"),
                "peak_magnitude": capture.get("openflight:board_peak_magnitude"),
            }
        )
    return rows


def _display(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return _format_number(value, 2)
    return str(value)


def _metric_display(value: Any, unit: str, digits: int = 2) -> str:
    """Format an optional numeric summary with its display unit."""
    if value is None:
        return "—"
    if isinstance(value, float):
        value_display = f"{value:.1f}" if digits == 1 else _format_number(value, digits)
    else:
        value_display = str(value)
    return f"{value_display} {unit}"


def _trigger_display(row: dict[str, Any]) -> str:
    """Show an actual host trigger speed or the hardware threshold."""
    trigger_speed = row.get("trigger_speed")
    trigger_threshold = row.get("trigger_threshold")
    if trigger_speed is not None:
        value = _metric_display(trigger_speed, "mph", digits=1)
    elif trigger_threshold is not None:
        value = f">{_metric_display(trigger_threshold, 'mph', digits=1)}"
    else:
        return "—"

    trigger_type = row.get("trigger_type")
    return f"{value} ({trigger_type})" if trigger_type else value


def _spin_display(row: dict[str, Any]) -> str:
    """Format spin RPM and retain the processor quality label when present."""
    value = _metric_display(row.get("spin_rpm"), "RPM", digits=0)
    quality = row.get("spin_quality")
    if quality and value != "—":
        return f"{value} ({quality})"
    return value


def _session_index_html(session_dir: Path) -> bytes:
    rows = _session_rows(session_dir)
    if rows:
        body_rows = (
            "<tr>"
            f'<td><a href="{html.escape(row["name"] + "/" + SHOT_PREVIEW_FILENAME)}">{html.escape(row["name"])}</a>'
            f'<br><img loading="lazy" src="{html.escape(row["name"] + "/" + SHOT_PREVIEW_FILENAME)}" alt="{html.escape(row["name"])} preview"></td>'
            f'<td>{html.escape(_display(row["shot_number"]))}</td>'
            f'<td>{html.escape(_trigger_display(row))}</td>'
            f'<td>{html.escape(_metric_display(row["ball_speed"], "mph", digits=1))}</td>'
            f'<td>{html.escape(_metric_display(row["club_speed"], "mph", digits=1))}</td>'
            f'<td>{html.escape(_spin_display(row))}</td>'
            f'<td>{html.escape(_metric_display(row["carry"], "yd", digits=1))}</td>'
            f'<td>{html.escape(_display(row["records"]))}</td>'
            f'<td>{html.escape(_display(row["points"]))}</td>'
            f'<td>{html.escape(_metric_display(row["peak_speed"], row["unit"]))}</td>'
            f'<td>{html.escape(_display(row["peak_magnitude"]))}</td>'
            "</tr>"
            for row in rows
        )
    else:
        body_rows = ('<tr><td colspan="11" class="empty">No completed shot previews yet.</td></tr>',)
    session_title = html.escape(session_dir.name)
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OpenFlight {session_title}</title>
<style>
body {{ font-family: system-ui, -apple-system, Segoe UI, sans-serif; color: #1f2937; margin: 2rem; background: #f3f4f6; }}
main {{ max-width: 1500px; margin: auto; background: #fff; padding: 1.5rem 2rem 2rem; border: 1px solid #d1d5db; border-radius: 10px; }}
h1 {{ margin-top: 0; font-size: 1.5rem; }}
p {{ color: #4b5563; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; vertical-align: top; padding: .7rem; border-top: 1px solid #e5e7eb; }}
th {{ background: #f9fafb; font-size: .9rem; }}
a {{ color: #1d4ed8; }}
img {{ display: block; width: 320px; max-width: 45vw; margin-top: .55rem; border: 1px solid #d1d5db; }}
.empty {{ text-align: center; color: #6b7280; padding: 2rem; }}
.note {{ font-size: .9rem; }}
</style>
</head>
<body><main>
<h1>OpenFlight board speed / magnitude previews</h1>
<p>Session: <code>{session_title}</code></p>
<p class="note">Trigger shows the actual host trigger speed when available, otherwise the hardware threshold. Spin includes the processor quality label when present. Absolute board speed is plotted on the Y-axis; point color identifies the 1-based board array position: Mag1 is highest magnitude and Mag5 is lowest. Open a hit name for the full-size SVG.</p>
<table>
<thead><tr><th>Hit / preview (OpenFlight)</th><th>Shot (OpenFlight)</th><th>Trigger / threshold (OPS243)</th><th>Ball speed (OpenFlight processor)</th><th>Club speed (OpenFlight processor)</th><th>Spin (OpenFlight processor)</th><th>Carry (OpenFlight model)</th><th>Board records (OPS243)</th><th>Paired points (OpenFlight)</th><th>Peak speed (OPS243)</th><th>Peak magnitude (OPS243)</th></tr></thead>
<tbody>{''.join(body_rows)}</tbody>
</table>
</main></body></html>
"""
    return document.encode("utf-8")


def update_session_index(session_dir: Path) -> Path:
    """Atomically refresh the static HTML index for a kiosk session."""
    index_path = session_dir / SESSION_INDEX_FILENAME
    content = _session_index_html(session_dir)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=session_dir,
            prefix=f".{SESSION_INDEX_FILENAME}.",
            delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, index_path)
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise
    return index_path
