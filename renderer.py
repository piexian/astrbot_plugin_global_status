"""SVG-backed Pillow status alert and overview card renderer."""

from __future__ import annotations

import math
import re
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from PIL import Image, ImageChops, ImageDraw, ImageFont

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .sources import Issue, SourceResult
from .translation import normalize_language

WIDTH = 1200
BACKGROUND_TOP = "#ECEAE5"
BACKGROUND_BOTTOM = "#ECEAE5"
SURFACE = "#FAF9F6"
SURFACE_ALT = "#F5F3EE"
TEXT = "#19222C"
TEXT_SOFT = "#36414C"
MUTED = "#727B84"
BORDER = "#D4D1CA"
ICON_DIR = Path(__file__).parent / "assets" / "icons"
PROJECT_SIGNATURE = "github.com/Futureppo/astrbot_plugin_global_status"

SEVERITY_COLORS = {
    "critical": "#BD3342",
    "warning": "#A86B0A",
    "maintenance": "#31699D",
    "info": "#31699D",
    "operational": "#287256",
    "unavailable": "#707982",
}

VENDOR_COLORS = {
    "openai": "#10A37F",
    "claude": "#D97757",
    "google_vertex_gemini": "#7C8CF8",
    "groq": "#F55036",
    "cohere": "#2D8C78",
    "xai": "#111111",
    "deepseek": "#4D6BFE",
    "aws": "#E99024",
    "azure": "#1689D4",
    "github": "#64748B",
    "cloudflare": "#F48120",
}

STAGE_LABELS = {
    "zh-CN": {
        "new": "新异常",
        "current": "当前异常",
        "update": "状态更新",
        "recovered": "已恢复",
    },
    "en-US": {
        "new": "New incident",
        "current": "Active incident",
        "update": "Status update",
        "recovered": "Recovered",
    },
    "bilingual": {
        "new": "新异常  /  New incident",
        "current": "当前异常  /  Active incident",
        "update": "状态更新  /  Status update",
        "recovered": "已恢复  /  Recovered",
    },
}

STATUS_LABELS = {
    "zh-CN": {
        "critical": "严重异常",
        "warning": "服务降级",
        "maintenance": "计划维护",
        "info": "状态提醒",
        "operational": "运行正常",
        "unavailable": "数据不可用",
    },
    "en-US": {
        "critical": "Critical",
        "warning": "Degraded",
        "maintenance": "Maintenance",
        "info": "Notice",
        "operational": "Operational",
        "unavailable": "Unavailable",
    },
    "bilingual": {
        "critical": "严重异常  /  Critical",
        "warning": "服务降级  /  Degraded",
        "maintenance": "计划维护  /  Maintenance",
        "info": "状态提醒  /  Notice",
        "operational": "运行正常  /  Operational",
        "unavailable": "数据不可用  /  Unavailable",
    },
}


def _font_candidates(bold: bool) -> list[str]:
    data_font = Path(get_astrbot_data_path()) / "font.ttf"
    if bold:
        return [
            str(data_font),
            r"C:\Windows\Fonts\msyhbd.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "msyhbd.ttc",
            "NotoSansCJK-Bold.ttc",
            "DejaVuSans-Bold.ttf",
        ]
    return [
        str(data_font),
        r"C:\Windows\Fonts\msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "msyh.ttc",
        "NotoSansCJK-Regular.ttc",
        "DejaVuSans.ttf",
    ]


@lru_cache(maxsize=48)
def _font(
    size: int, bold: bool = False
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _font_candidates(bold):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _text_width(text: str, font: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> float:
    try:
        return font.getlength(text)
    except AttributeError:
        left, _, right, _ = font.getbbox(text)
        return float(right - left)


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    """Wrap mixed Chinese and English text to a bounded number of lines.

    Args:
        text: Source text.
        font: Pillow font used for width measurement.
        max_width: Maximum rendered line width.
        max_lines: Maximum returned line count.

    Returns:
        Wrapped lines with an ellipsis when content is truncated.
    """
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return []
    lines: list[str] = []
    current = ""
    cursor = 0
    while cursor < len(normalized) and len(lines) < max_lines:
        char = normalized[cursor]
        candidate = current + char
        if not current or _text_width(candidate, font) <= max_width:
            current = candidate
            cursor += 1
            continue
        break_at = current.rfind(" ")
        if break_at > 0:
            lines.append(current[:break_at].rstrip())
            current = current[break_at + 1 :]
        else:
            lines.append(current.rstrip())
            current = ""
    if current and len(lines) < max_lines:
        lines.append(current.rstrip())
    if cursor < len(normalized) and lines:
        suffix = "…"
        last = lines[-1]
        while last and _text_width(last + suffix, font) > max_width:
            last = last[:-1]
        lines[-1] = last.rstrip() + suffix
    return lines


def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    position: tuple[int, int],
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: str,
    line_height: int,
) -> int:
    x, y = position
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def _parse_color(value: str | None, current_color: str) -> str | None:
    if not value or value == "none":
        return None
    return current_color if value == "currentColor" else value


_PATH_TOKEN_PATTERN = re.compile(
    r"[AaCcHhLlMmVvZz]|[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?"
)


def _arc_points(
    start: tuple[float, float],
    radius_x: float,
    radius_y: float,
    rotation: float,
    large_arc: bool,
    sweep: bool,
    end: tuple[float, float],
) -> list[tuple[float, float]]:
    """Approximate one SVG elliptical arc with short line segments.

    Args:
        start: Arc start point.
        radius_x: Horizontal ellipse radius.
        radius_y: Vertical ellipse radius.
        rotation: Ellipse-axis rotation in degrees.
        large_arc: Whether to select the larger arc.
        sweep: Whether to use the positive-angle direction.
        end: Arc end point.

    Returns:
        Sampled points excluding ``start`` and including ``end``.
    """
    x1, y1 = start
    x2, y2 = end
    radius_x = abs(radius_x)
    radius_y = abs(radius_y)
    if radius_x == 0 or radius_y == 0 or start == end:
        return [end]

    angle = math.radians(rotation % 360)
    cosine = math.cos(angle)
    sine = math.sin(angle)
    half_x = (x1 - x2) / 2
    half_y = (y1 - y2) / 2
    transformed_x = cosine * half_x + sine * half_y
    transformed_y = -sine * half_x + cosine * half_y

    scale = transformed_x**2 / radius_x**2 + transformed_y**2 / radius_y**2
    if scale > 1:
        multiplier = math.sqrt(scale)
        radius_x *= multiplier
        radius_y *= multiplier

    numerator = max(
        0.0,
        radius_x**2 * radius_y**2
        - radius_x**2 * transformed_y**2
        - radius_y**2 * transformed_x**2,
    )
    denominator = radius_x**2 * transformed_y**2 + radius_y**2 * transformed_x**2
    coefficient = 0.0
    if denominator:
        coefficient = math.sqrt(numerator / denominator)
        if large_arc == sweep:
            coefficient = -coefficient
    center_x_transformed = coefficient * radius_x * transformed_y / radius_y
    center_y_transformed = -coefficient * radius_y * transformed_x / radius_x
    center_x = (
        cosine * center_x_transformed - sine * center_y_transformed + (x1 + x2) / 2
    )
    center_y = (
        sine * center_x_transformed + cosine * center_y_transformed + (y1 + y2) / 2
    )

    def vector_angle(first: tuple[float, float], second: tuple[float, float]) -> float:
        dot = first[0] * second[0] + first[1] * second[1]
        length = math.hypot(*first) * math.hypot(*second)
        value = max(-1.0, min(1.0, dot / length)) if length else 1.0
        result = math.acos(value)
        if first[0] * second[1] - first[1] * second[0] < 0:
            result = -result
        return result

    start_vector = (
        (transformed_x - center_x_transformed) / radius_x,
        (transformed_y - center_y_transformed) / radius_y,
    )
    end_vector = (
        (-transformed_x - center_x_transformed) / radius_x,
        (-transformed_y - center_y_transformed) / radius_y,
    )
    start_angle = vector_angle((1, 0), start_vector)
    angle_delta = vector_angle(start_vector, end_vector)
    if not sweep and angle_delta > 0:
        angle_delta -= math.tau
    elif sweep and angle_delta < 0:
        angle_delta += math.tau

    steps = max(4, math.ceil(abs(angle_delta) / (math.pi / 18)))
    points: list[tuple[float, float]] = []
    for step in range(1, steps + 1):
        position = start_angle + angle_delta * step / steps
        ellipse_x = radius_x * math.cos(position)
        ellipse_y = radius_y * math.sin(position)
        points.append(
            (
                center_x + cosine * ellipse_x - sine * ellipse_y,
                center_y + sine * ellipse_x + cosine * ellipse_y,
            )
        )
    points[-1] = end
    return points


def _svg_path_subpaths(data: str) -> list[list[tuple[float, float]]]:
    """Convert supported SVG path commands into sampled polygon subpaths.

    Args:
        data: SVG path ``d`` attribute.

    Returns:
        Source-coordinate point lists for each subpath.

    Raises:
        ValueError: If malformed or unsupported path data is encountered.
    """
    tokens = _PATH_TOKEN_PATTERN.findall(data.replace(",", " "))
    index = 0
    command = ""
    current = (0.0, 0.0)
    start = (0.0, 0.0)
    subpath: list[tuple[float, float]] = []
    subpaths: list[list[tuple[float, float]]] = []

    def number() -> float:
        nonlocal index
        if index >= len(tokens) or tokens[index].isalpha():
            raise ValueError("Malformed SVG path data")
        value = float(tokens[index])
        index += 1
        return value

    def arc_flag() -> bool:
        nonlocal index
        if index >= len(tokens) or tokens[index].isalpha():
            raise ValueError("Malformed SVG arc flag")
        token = tokens[index]
        if not token or token[0] not in {"0", "1"}:
            raise ValueError("SVG arc flags must be zero or one")
        value = token[0] == "1"
        if len(token) == 1:
            index += 1
        else:
            # Minified SVG permits adjacent flags and coordinates, such as
            # ``00-.856``. Keep the remainder available to the next reader.
            tokens[index] = token[1:]
        return value

    while index < len(tokens):
        if tokens[index].isalpha():
            command = tokens[index]
            index += 1
            if command in {"Z", "z"}:
                if subpath and subpath[-1] != start:
                    subpath.append(start)
                current = start
                command = ""
                continue
        if not command:
            if index < len(tokens) and tokens[index].isalpha():
                continue
            raise ValueError("SVG path data is missing a command")

        relative = command.islower()
        operation = command.upper()
        if operation == "M":
            x, y = number(), number()
            if relative:
                x += current[0]
                y += current[1]
            if subpath:
                subpaths.append(subpath)
            current = (x, y)
            start = current
            subpath = [current]
            command = "l" if relative else "L"
        elif operation == "L":
            x, y = number(), number()
            if relative:
                x += current[0]
                y += current[1]
            current = (x, y)
            subpath.append(current)
        elif operation == "H":
            x = number() + (current[0] if relative else 0)
            current = (x, current[1])
            subpath.append(current)
        elif operation == "V":
            y = number() + (current[1] if relative else 0)
            current = (current[0], y)
            subpath.append(current)
        elif operation == "C":
            control_one = (number(), number())
            control_two = (number(), number())
            end = (number(), number())
            if relative:
                control_one = (
                    control_one[0] + current[0],
                    control_one[1] + current[1],
                )
                control_two = (
                    control_two[0] + current[0],
                    control_two[1] + current[1],
                )
                end = (end[0] + current[0], end[1] + current[1])
            origin = current
            for step in range(1, 13):
                position = step / 12
                inverse = 1 - position
                subpath.append(
                    (
                        inverse**3 * origin[0]
                        + 3 * inverse**2 * position * control_one[0]
                        + 3 * inverse * position**2 * control_two[0]
                        + position**3 * end[0],
                        inverse**3 * origin[1]
                        + 3 * inverse**2 * position * control_one[1]
                        + 3 * inverse * position**2 * control_two[1]
                        + position**3 * end[1],
                    )
                )
            current = end
        elif operation == "A":
            radius_x, radius_y, rotation = number(), number(), number()
            large_arc, sweep = arc_flag(), arc_flag()
            end = (number(), number())
            if relative:
                end = (end[0] + current[0], end[1] + current[1])
            subpath.extend(
                _arc_points(
                    current,
                    radius_x,
                    radius_y,
                    rotation,
                    large_arc,
                    sweep,
                    end,
                )
            )
            current = end
        else:
            raise ValueError(f"Unsupported SVG path command: {command}")
    if subpath:
        subpaths.append(subpath)
    return subpaths


@lru_cache(maxsize=160)
def _svg_icon(name: str, size: int, current_color: str = "#FFFFFF") -> Image.Image:
    """Rasterize a controlled local SVG asset without native dependencies.

    The local icon set uses geometric primitives and sampled SVG paths. This keeps
    plugin installation portable while retaining SVG as the source of truth.

    Args:
        name: SVG asset stem.
        size: Output width and height in pixels.
        current_color: Replacement for SVG's ``currentColor`` value.

    Returns:
        Transparent RGBA icon image.

    Raises:
        ValueError: If the SVG viewBox or primitive data is invalid.
        OSError: If the asset cannot be read.
    """
    path = ICON_DIR / f"{name}.svg"
    root = ElementTree.fromstring(path.read_text(encoding="utf-8"))
    view_box = [
        float(value) for value in root.attrib.get("viewBox", "0 0 64 64").split()
    ]
    if len(view_box) != 4 or view_box[2] <= 0 or view_box[3] <= 0:
        raise ValueError(f"Invalid SVG viewBox in {path.name}")
    left, top, width, height = view_box
    scale = min(size / width, size / height)
    offset_x = (size - width * scale) / 2
    offset_y = (size - height * scale) / 2
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    gradients: dict[str, str] = {}
    for candidate in root.iter():
        if candidate.tag.rsplit("}", 1)[-1] != "linearGradient":
            continue
        gradient_id = candidate.attrib.get("id", "")
        for stop in candidate:
            if stop.tag.rsplit("}", 1)[-1] != "stop":
                continue
            color = stop.attrib.get("stop-color", "#000000")
            opacity = float(stop.attrib.get("stop-opacity", "1"))
            if color.startswith("#") and opacity < 1:
                raw = color.removeprefix("#")
                if len(raw) == 3:
                    raw = "".join(character * 2 for character in raw)
                color = f"#{raw[:6]}{round(opacity * 255):02X}"
            gradients[gradient_id] = color
            break

    def point(x: str | float, y: str | float) -> tuple[int, int]:
        return (
            round((float(x) - left) * scale + offset_x),
            round((float(y) - top) * scale + offset_y),
        )

    def render(element: ElementTree.Element, inherited: dict[str, str]) -> None:
        attrs = dict(inherited)
        attrs.update(element.attrib)
        tag = element.tag.rsplit("}", 1)[-1]
        fill_value = attrs.get("fill", "#000000")
        gradient_match = re.fullmatch(r"url\(#(.+)\)", fill_value)
        fill = (
            gradients.get(gradient_match.group(1), current_color)
            if gradient_match
            else _parse_color(fill_value, current_color)
        )
        stroke = _parse_color(attrs.get("stroke"), current_color)
        stroke_width = max(
            1,
            round(float(attrs.get("stroke-width", "1")) * scale),
        )
        if tag == "rect":
            x1, y1 = point(attrs.get("x", 0), attrs.get("y", 0))
            x2, y2 = point(
                float(attrs.get("x", 0)) + float(attrs.get("width", 0)),
                float(attrs.get("y", 0)) + float(attrs.get("height", 0)),
            )
            radius = round(float(attrs.get("rx", 0)) * scale)
            draw.rounded_rectangle(
                (x1, y1, x2, y2),
                radius=radius,
                fill=fill,
                outline=stroke,
                width=stroke_width,
            )
        elif tag in {"circle", "ellipse"}:
            cx = float(attrs.get("cx", 0))
            cy = float(attrs.get("cy", 0))
            rx = float(attrs.get("r", attrs.get("rx", 0)))
            ry = float(attrs.get("r", attrs.get("ry", 0)))
            x1, y1 = point(cx - rx, cy - ry)
            x2, y2 = point(cx + rx, cy + ry)
            draw.ellipse(
                (x1, y1, x2, y2),
                fill=fill,
                outline=stroke,
                width=stroke_width,
            )
        elif tag == "line":
            draw.line(
                (
                    *point(attrs.get("x1", 0), attrs.get("y1", 0)),
                    *point(attrs.get("x2", 0), attrs.get("y2", 0)),
                ),
                fill=stroke or fill,
                width=stroke_width,
            )
        elif tag in {"polyline", "polygon"}:
            values = [
                float(value)
                for value in attrs.get("points", "").replace(",", " ").split()
            ]
            points = [
                point(values[index], values[index + 1])
                for index in range(0, len(values), 2)
            ]
            if tag == "polygon" and fill:
                draw.polygon(points, fill=fill)
            if stroke and len(points) > 1:
                line_points = points + ([points[0]] if tag == "polygon" else [])
                draw.line(line_points, fill=stroke, width=stroke_width, joint="curve")
        elif tag == "path":
            subpaths = _svg_path_subpaths(attrs.get("d", ""))
            sampled = [
                [point(source_x, source_y) for source_x, source_y in subpath]
                for subpath in subpaths
                if len(subpath) >= 2
            ]
            if fill and sampled:
                path_mask = Image.new("1", (size, size), 0)
                even_odd = attrs.get("fill-rule", "").lower() == "evenodd"
                for polygon in sampled:
                    if len(polygon) < 3:
                        continue
                    if even_odd:
                        subpath_mask = Image.new("1", (size, size), 0)
                        ImageDraw.Draw(subpath_mask).polygon(polygon, fill=1)
                        path_mask = ImageChops.logical_xor(path_mask, subpath_mask)
                    else:
                        ImageDraw.Draw(path_mask).polygon(polygon, fill=1)
                overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                ImageDraw.Draw(overlay).bitmap((0, 0), path_mask, fill=fill)
                image.alpha_composite(overlay)
            if stroke:
                for sampled_path in sampled:
                    draw.line(
                        sampled_path,
                        fill=stroke,
                        width=stroke_width,
                        joint="curve",
                    )
        for child in element:
            render(child, attrs)

    render(root, {})
    return image


def _paste_icon(
    image: Image.Image,
    name: str,
    position: tuple[int, int],
    size: int,
    color: str = "#FFFFFF",
) -> None:
    icon = _svg_icon(name, size, color)
    image.alpha_composite(icon, position)


def _new_canvas(height: int) -> Image.Image:
    image = Image.new("RGBA", (WIDTH, height), BACKGROUND_TOP)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, WIDTH, 8), fill="#222B34")
    draw.line((52, 24, WIDTH - 52, 24), fill="#D8D5CE", width=1)
    return image


def _draw_panel(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    fill: str = SURFACE,
    radius: int = 16,
) -> None:
    x1, y1, x2, y2 = bounds
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=BORDER, width=1)
    draw.line((x1 + radius, y1 + 1, x2 - radius, y1 + 1), fill="#FFFFFF", width=1)


def _draw_project_footer(image: Image.Image, y: int) -> None:
    """Draw the centered GitHub repository link in the image footer."""
    draw = ImageDraw.Draw(image)
    font = _font(17)
    icon_size = 18
    gap = 10
    link_width = _text_width(PROJECT_SIGNATURE, font)
    footer_width = icon_size + gap + link_width
    footer_x = (WIDTH - footer_width) / 2
    _paste_icon(
        image,
        "github",
        (round(footer_x), y + 1),
        icon_size,
        "#858B91",
    )
    draw.text(
        (footer_x + icon_size + gap, y - 1),
        PROJECT_SIGNATURE,
        font=font,
        fill="#858B91",
    )


def _localized_pair(
    text: str,
    translations: dict[str, str],
    language: str,
) -> tuple[str, str]:
    original = " ".join(str(text or "").split())
    translated = translations.get(original, "")
    if language == "en-US":
        return original, ""
    if language == "zh-CN":
        return translated or original, ""
    if translated and translated != original:
        return translated, original
    return original, ""


def _stage_icon(stage: str) -> str:
    if stage == "recovered":
        return "check"
    if stage == "update":
        return "update"
    return "alert"


def _status_icon(severity: str) -> str:
    if severity == "operational":
        return "check"
    if severity == "unavailable":
        return "unavailable"
    if severity in {"info", "maintenance"}:
        return "update"
    return "alert"


def _vendor_icon(source_id: str) -> str:
    return source_id if (ICON_DIR / f"{source_id}.svg").is_file() else "vendor"


def _event_layout(
    stage: str,
    issue: Issue,
    translations: dict[str, str],
    language: str,
) -> dict[str, object]:
    title, title_original = _localized_pair(issue.title, translations, language)
    title_lines = _wrap_text(title, _font(36, True), 950, 3)
    title_original_lines = _wrap_text(title_original, _font(20), 950, 2)

    services = list(issue.affected_services[:12])
    localized_services = [
        _localized_pair(service, translations, language)[0] for service in services
    ]
    service_text = "、".join(localized_services)
    service_original = " · ".join(services) if language == "bilingual" else ""
    if len(issue.affected_services) > 12:
        service_text += f" 等 {len(issue.affected_services)} 项"
        service_original += f" · {len(issue.affected_services)} services total"
    if service_original == service_text:
        service_original = ""
    service_lines = _wrap_text(service_text, _font(23), 780, 3)
    service_original_lines = _wrap_text(service_original, _font(18), 780, 2)

    detail, detail_original = _localized_pair(issue.detail, translations, language)
    detail_lines = _wrap_text(detail, _font(23), 780, max(1, len(detail)))
    detail_original_lines = _wrap_text(
        detail_original,
        _font(18),
        780,
        max(1, len(detail_original)),
    )

    height = 82 + len(title_lines) * 47 + len(title_original_lines) * 27
    if service_lines:
        service_height = max(
            43 if language == "bilingual" else 28,
            len(service_lines) * 32 + len(service_original_lines) * 25,
        )
        height += 40 + service_height
    if detail_lines:
        detail_height = max(
            43 if language == "bilingual" else 28,
            len(detail_lines) * 32 + len(detail_original_lines) * 25,
        )
        height += 40 + detail_height
    height += 68
    return {
        "stage": stage,
        "issue": issue,
        "title_lines": title_lines,
        "title_original_lines": title_original_lines,
        "service_lines": service_lines,
        "service_original_lines": service_original_lines,
        "detail_lines": detail_lines,
        "detail_original_lines": detail_original_lines,
        "height": max(height, 228),
    }


def render_alert_card(
    source_name: str,
    events: list[tuple[str, Issue]],
    translations: dict[str, str] | None = None,
    language: str = "bilingual",
) -> bytes:
    """Render one vendor's changed issues into a polished bilingual PNG card.

    Args:
        source_name: Human-readable vendor name.
        events: Stage and issue pairs for this notification.
        translations: Original official strings mapped to Chinese translations.
        language: ``zh-CN``, ``en-US``, or ``bilingual``.

    Returns:
        Encoded PNG bytes.

    Raises:
        ValueError: If no events were provided.
    """
    if not events:
        raise ValueError("At least one alert event is required")
    language = normalize_language(language)
    translations = translations or {}
    layouts = [
        _event_layout(stage, issue, translations, language) for stage, issue in events
    ]
    height = 164 + sum(int(layout["height"]) + 20 for layout in layouts) + 68
    image = _new_canvas(height)
    draw = ImageDraw.Draw(image)

    source_id = events[0][1].source_id
    vendor_color = VENDOR_COLORS.get(source_id, "#386A8A")
    draw.rounded_rectangle(
        (54, 44, 126, 116),
        radius=16,
        fill=SURFACE,
        outline=BORDER,
        width=1,
    )
    _paste_icon(image, _vendor_icon(source_id), (70, 60), 40, vendor_color)
    source_name_lines = _wrap_text(source_name, _font(40, True), 610, 1)
    _draw_lines(
        draw,
        source_name_lines,
        (150, 42),
        _font(40, True),
        TEXT,
        48,
    )
    header_subtitle = {
        "zh-CN": "厂商官方状态更新",
        "en-US": "Official vendor status update",
        "bilingual": "厂商官方状态更新  /  Official vendor update",
    }[language]
    draw.text((152, 91), header_subtitle, font=_font(20), fill=MUTED)

    source_url = events[0][1].status_url
    source_host = urlparse(source_url).netloc or "official status page"
    source_label = {
        "zh-CN": "官方来源",
        "en-US": "Official source",
        "bilingual": "官方来源  /  Official source",
    }[language]
    draw.text((812, 46), source_label, font=_font(15, True), fill=MUTED)
    draw.text((812, 72), source_host, font=_font(21, True), fill=TEXT_SOFT)
    count_label = (
        f"本次 {len(events)} 项变更"
        if language == "zh-CN"
        else f"{len(events)} change(s) in this bulletin"
    )
    if language == "bilingual":
        count_label = f"本次 {len(events)} 项变更  /  {len(events)} change(s)"
    draw.text((812, 100), count_label, font=_font(16), fill=MUTED)
    draw.rectangle((54, 136, 272, 140), fill=vendor_color)
    draw.rectangle((272, 137, WIDTH - 54, 139), fill=BORDER)

    y = 164
    for event_index, layout in enumerate(layouts, start=1):
        stage = str(layout["stage"])
        issue = layout["issue"]
        assert isinstance(issue, Issue)
        block_height = int(layout["height"])
        severity = "operational" if stage == "recovered" else issue.severity
        accent = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["warning"])
        _draw_panel(image, (54, y, WIDTH - 54, y + block_height))
        draw = ImageDraw.Draw(image)
        draw.rectangle((54, y + 16, 59, y + block_height - 16), fill=accent)
        draw.text(
            (82, y + 25),
            f"{event_index:02d}",
            font=_font(17, True),
            fill=MUTED,
        )
        draw.line((116, y + 24, 116, y + 50), fill=BORDER, width=1)
        _paste_icon(image, _stage_icon(stage), (136, y + 27), 18, accent)
        draw.text(
            (166, y + 23),
            STAGE_LABELS[language].get(stage, stage),
            font=_font(19, True),
            fill=accent,
        )
        severity_label = STATUS_LABELS[language].get(severity, severity)
        severity_font = _font(18, True)
        severity_width = int(_text_width(severity_label, severity_font))
        severity_x = WIDTH - 82 - severity_width
        draw.ellipse((severity_x - 24, y + 31, severity_x - 12, y + 43), fill=accent)
        draw.text(
            (severity_x, y + 24),
            severity_label,
            font=severity_font,
            fill=accent,
        )

        cursor = y + 82
        cursor = _draw_lines(
            draw, layout["title_lines"], (82, cursor), _font(36, True), TEXT, 47
        )
        original_lines = layout["title_original_lines"]
        if original_lines:
            cursor = _draw_lines(
                draw, original_lines, (82, cursor), _font(20), MUTED, 27
            )

        service_lines = layout["service_lines"]
        if service_lines:
            cursor += 20
            draw.line((82, cursor, WIDTH - 82, cursor), fill=BORDER, width=1)
            cursor += 20
            _paste_icon(image, "service", (82, cursor + 1), 20, accent)
            service_heading = (
                "Affected services" if language == "en-US" else "受影响服务"
            )
            draw.text(
                (112, cursor - 2),
                service_heading,
                font=_font(17, True),
                fill=TEXT_SOFT,
            )
            if language == "bilingual":
                draw.text(
                    (112, cursor + 20),
                    "Affected services",
                    font=_font(14),
                    fill=MUTED,
                )
            content_cursor = _draw_lines(
                draw, service_lines, (280, cursor - 3), _font(23), TEXT_SOFT, 32
            )
            service_original_lines = layout["service_original_lines"]
            if service_original_lines:
                content_cursor = _draw_lines(
                    draw,
                    service_original_lines,
                    (280, content_cursor),
                    _font(18),
                    MUTED,
                    25,
                )
            label_height = 43 if language == "bilingual" else 28
            cursor = max(cursor + label_height, content_cursor)

        detail_lines = layout["detail_lines"]
        if detail_lines:
            cursor += 20
            draw.line((82, cursor, WIDTH - 82, cursor), fill=BORDER, width=1)
            cursor += 20
            detail_heading = "Official note" if language == "en-US" else "官方说明"
            draw.text(
                (82, cursor - 2),
                detail_heading,
                font=_font(17, True),
                fill=TEXT_SOFT,
            )
            if language == "bilingual":
                draw.text(
                    (82, cursor + 20),
                    "Official note",
                    font=_font(14),
                    fill=MUTED,
                )
            content_cursor = _draw_lines(
                draw, detail_lines, (280, cursor - 3), _font(23), TEXT_SOFT, 32
            )
            detail_original_lines = layout["detail_original_lines"]
            if detail_original_lines:
                content_cursor = _draw_lines(
                    draw,
                    detail_original_lines,
                    (280, content_cursor),
                    _font(18),
                    MUTED,
                    25,
                )
            label_height = 43 if language == "bilingual" else 28
            cursor = max(cursor + label_height, content_cursor)

        meta_y = y + block_height - 45
        draw.line((82, meta_y - 15, WIDTH - 82, meta_y - 15), fill="#E6E3DC", width=1)
        if issue.updated_at:
            _paste_icon(image, "clock", (82, meta_y), 18, MUTED)
            draw.text((110, meta_y - 2), issue.updated_at, font=_font(17), fill=MUTED)
        if issue.status_url:
            parsed = urlparse(issue.status_url)
            url_text = issue.status_url
            if len(url_text) > 58:
                url_text = f"{parsed.scheme}://{parsed.netloc}/…"
            url_width = _text_width(url_text, _font(17))
            link_x = WIDTH - 82 - int(url_width)
            _paste_icon(image, "link", (link_x - 27, meta_y), 18, "#315F7C")
            draw.text((link_x, meta_y - 2), url_text, font=_font(17), fill="#315F7C")
        y += block_height + 20

    _draw_project_footer(image, height - 40)
    output = BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def render_overview(
    results: list[SourceResult],
    translations: dict[str, str] | None = None,
    language: str = "bilingual",
) -> bytes:
    """Render current status of all enabled sources into one bilingual PNG image.

    Args:
        results: Latest source query results.
        translations: Original official strings mapped to Chinese translations.
        language: ``zh-CN``, ``en-US``, or ``bilingual``.

    Returns:
        Encoded PNG bytes.
    """
    language = normalize_language(language)
    translations = translations or {}
    rows: list[dict[str, object]] = []
    for result in results:
        primary_lines: list[str] = []
        original_lines: list[str] = []
        if result.success and result.issues:
            first_issue = max(
                result.issues.values(),
                key=lambda issue: (
                    issue.severity == "critical",
                    issue.updated_at,
                ),
            )
            primary, original = _localized_pair(
                first_issue.title, translations, language
            )
            primary_lines = _wrap_text(primary, _font(21, True), 370, 2)
            original_lines = _wrap_text(original, _font(16), 370, 2)
        row_height = max(
            96,
            68 + len(primary_lines) * 28 + len(original_lines) * 22,
        )
        rows.append(
            {
                "result": result,
                "primary_lines": primary_lines,
                "original_lines": original_lines,
                "height": max(112, row_height),
            }
        )
    if not rows:
        rows = [
            {"result": None, "primary_lines": [], "original_lines": [], "height": 96}
        ]
    table_header_height = 50
    table_height = table_header_height + sum(int(row["height"]) for row in rows)
    height = 144 + table_height + 68
    image = _new_canvas(height)
    draw = ImageDraw.Draw(image)
    draw.text(
        (52, 48),
        "全球厂商服务状态" if language != "en-US" else "Global vendor status",
        font=_font(43, True),
        fill=TEXT,
    )
    count_text = (
        f"监控来源  {len(results):02d}"
        if language == "zh-CN"
        else f"Sources monitored  {len(results):02d}"
    )
    if language == "bilingual":
        count_text = f"监控来源 / Sources  {len(results):02d}"
    draw.text((878, 52), count_text, font=_font(18, True), fill=TEXT_SOFT)
    live_text = {
        "zh-CN": "实时查询 · 官方状态接口",
        "en-US": "Live query · Official status APIs",
        "bilingual": "实时查询 / Live query · Official APIs",
    }[language]
    draw.text((878, 86), live_text, font=_font(16), fill=MUTED)
    draw.rectangle((52, 118, 300, 122), fill="#222B34")
    draw.rectangle((300, 119, WIDTH - 52, 121), fill=BORDER)

    table_y = 144
    _draw_panel(image, (52, table_y, WIDTH - 52, table_y + table_height), radius=14)
    draw = ImageDraw.Draw(image)
    column_labels = {
        "zh-CN": ("厂商", "事件摘要", "当前状态"),
        "en-US": ("Vendor", "Incident summary", "Current status"),
        "bilingual": (
            "厂商 / Vendor",
            "事件摘要 / Incident summary",
            "当前状态 / Status",
        ),
    }[language]
    draw.text((80, table_y + 15), column_labels[0], font=_font(15, True), fill=MUTED)
    draw.text((488, table_y + 15), column_labels[1], font=_font(15, True), fill=MUTED)
    draw.text((916, table_y + 15), column_labels[2], font=_font(15, True), fill=MUTED)
    draw.line(
        (52, table_y + table_header_height, WIDTH - 52, table_y + table_header_height),
        fill=BORDER,
        width=1,
    )
    draw.line((460, table_y, 460, table_y + table_height), fill="#E2DFD8", width=1)
    draw.line((888, table_y, 888, table_y + table_height), fill="#E2DFD8", width=1)

    y = table_y + table_header_height
    for index, row in enumerate(rows):
        result = row["result"]
        row_height = int(row["height"])
        draw = ImageDraw.Draw(image)
        if result is None:
            draw.rectangle((53, y, WIDTH - 53, y + row_height), fill=SURFACE_ALT)
            _paste_icon(image, "unavailable", (82, y + 29), 38, MUTED)
            empty_text = (
                "没有启用任何状态来源"
                if language != "en-US"
                else "No status sources enabled"
            )
            draw.text((144, y + 31), empty_text, font=_font(24), fill=MUTED)
            y += row_height
            continue
        assert isinstance(result, SourceResult)
        severity = result.severity
        accent = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["unavailable"])
        row_fill = SURFACE if index % 2 == 0 else SURFACE_ALT
        if severity == "critical":
            row_fill = "#FBF0F1"
        elif severity == "warning":
            row_fill = "#FCF6E9"
        elif not result.success:
            row_fill = "#F1F2F2"
        draw.rectangle((53, y, WIDTH - 53, y + row_height), fill=row_fill)
        row_center = y + row_height // 2
        vendor_color = VENDOR_COLORS.get(result.spec.source_id, "#386A8A")
        draw.rounded_rectangle(
            (80, row_center - 28, 136, row_center + 28),
            radius=13,
            fill=SURFACE,
            outline=BORDER,
            width=1,
        )
        _paste_icon(
            image,
            _vendor_icon(result.spec.source_id),
            (93, row_center - 15),
            30,
            vendor_color,
        )
        vendor_name_lines = _wrap_text(
            result.spec.name,
            _font(22, True),
            275,
            2,
        )
        vendor_block_height = len(vendor_name_lines) * 27 + 21
        vendor_cursor = row_center - vendor_block_height // 2
        vendor_cursor = _draw_lines(
            draw,
            vendor_name_lines,
            (156, vendor_cursor),
            _font(22, True),
            TEXT,
            27,
        )
        source_kind = {
            "statuspage": "Statuspage JSON",
            "google": "Google incidents",
            "rss": "RSS feed",
        }.get(result.spec.kind, result.spec.kind)
        draw.text(
            (156, vendor_cursor + 2),
            source_kind,
            font=_font(15),
            fill=MUTED,
        )

        if result.success:
            if result.issues:
                subtitle_text = (
                    f"{len(result.issues)} 个活动事件"
                    if language != "en-US"
                    else f"{len(result.issues)} active incident(s)"
                )
                if language == "bilingual":
                    subtitle_text += f"  /  {len(result.issues)} active"
            else:
                subtitle_text = (
                    "未发现活动异常  /  No active incidents"
                    if language == "bilingual"
                    else (
                        "未发现活动异常"
                        if language == "zh-CN"
                        else "No active incidents"
                    )
                )
        else:
            subtitle_text = (
                "本次查询失败，不视为服务故障"
                if language == "zh-CN"
                else "Query failed; not treated as an outage"
            )
            if language == "bilingual":
                subtitle_text = "查询失败  /  Query failed; not treated as an outage"

        primary_lines = row["primary_lines"]
        original_lines = row["original_lines"]
        if primary_lines:
            draw.text((488, y + 17), subtitle_text, font=_font(15, True), fill=accent)
            cursor = _draw_lines(
                draw,
                primary_lines,
                (488, y + 43),
                _font(21, True),
                TEXT_SOFT,
                28,
            )
            if original_lines:
                _draw_lines(
                    draw,
                    original_lines,
                    (488, cursor),
                    _font(16),
                    MUTED,
                    22,
                )
        else:
            summary_lines = _wrap_text(
                subtitle_text,
                _font(17),
                370,
                2,
            )
            _draw_lines(
                draw,
                summary_lines,
                (488, row_center - len(summary_lines) * 12),
                _font(17),
                MUTED,
                24,
            )

        label = STATUS_LABELS[language].get(severity, severity)
        status_lines = _wrap_text(label, _font(16, True), 190, 2)
        status_y = row_center - len(status_lines) * 11
        _paste_icon(
            image,
            _status_icon(severity),
            (916, status_y + 1),
            22,
            accent,
        )
        _draw_lines(
            draw,
            status_lines,
            (948, status_y),
            _font(16, True),
            accent,
            22,
        )
        y += row_height
        if index < len(rows) - 1:
            draw.line((52, y, WIDTH - 52, y), fill=BORDER, width=1)

    draw.line((460, table_y, 460, table_y + table_height), fill="#E2DFD8", width=1)
    draw.line((888, table_y, 888, table_y + table_height), fill="#E2DFD8", width=1)

    _draw_project_footer(image, height - 40)
    output = BytesIO()
    image.convert("RGB").save(output, format="PNG", optimize=True)
    return output.getvalue()


def build_alert_fallback(
    source_name: str,
    events: list[tuple[str, Issue]],
    translations: dict[str, str] | None = None,
    language: str = "bilingual",
) -> str:
    """Build localized text fallback for exceptional image rendering failures.

    Args:
        source_name: Human-readable vendor name.
        events: Stage and issue pairs.
        translations: Original official strings mapped to Chinese translations.
        language: ``zh-CN``, ``en-US``, or ``bilingual``.

    Returns:
        Multiline status alert.
    """
    language = normalize_language(language)
    translations = translations or {}
    lines = [f"【{source_name} 服务状态 / SERVICE STATUS】"]
    for stage, issue in events:
        title, original = _localized_pair(issue.title, translations, language)
        lines.append(f"{STAGE_LABELS[language].get(stage, stage)} · {title}")
        if original:
            lines.append(original)
        if issue.affected_services:
            services = [
                _localized_pair(item, translations, language)[0]
                for item in issue.affected_services[:12]
            ]
            prefix = "受影响服务：" if language != "en-US" else "Affected services: "
            lines.append(prefix + "、".join(services))
        if issue.detail:
            detail, original_detail = _localized_pair(
                issue.detail, translations, language
            )
            lines.append(detail[:500])
            if original_detail:
                lines.append(original_detail[:500])
        if issue.status_url:
            lines.append(issue.status_url)
    return "\n".join(lines)
