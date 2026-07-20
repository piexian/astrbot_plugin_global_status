"""SVG-backed Pillow status alert and overview card renderer."""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .sources import Issue, SourceResult
from .translation import normalize_language

WIDTH = 1200
BACKGROUND_TOP = "#07101F"
BACKGROUND_BOTTOM = "#111A33"
SURFACE = "#121C30"
SURFACE_ALT = "#0E182A"
TEXT = "#F8FAFC"
TEXT_SOFT = "#D9E2F2"
MUTED = "#8FA1BC"
BORDER = "#263753"
ICON_DIR = Path(__file__).parent / "assets" / "icons"

SEVERITY_COLORS = {
    "critical": "#FB7185",
    "warning": "#FBBF24",
    "maintenance": "#60A5FA",
    "info": "#60A5FA",
    "operational": "#34D399",
    "unavailable": "#94A3B8",
}

VENDOR_COLORS = {
    "openai": "#10A37F",
    "claude": "#D97757",
    "google_vertex_gemini": "#7C8CF8",
    "groq": "#F55036",
    "cohere": "#2D8C78",
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
        "new": "NEW INCIDENT",
        "current": "ACTIVE",
        "update": "UPDATE",
        "recovered": "RECOVERED",
    },
    "bilingual": {
        "new": "新异常 · NEW",
        "current": "当前异常 · ACTIVE",
        "update": "状态更新 · UPDATE",
        "recovered": "已恢复 · RECOVERED",
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
        "critical": "CRITICAL",
        "warning": "DEGRADED",
        "maintenance": "MAINTENANCE",
        "info": "NOTICE",
        "operational": "OPERATIONAL",
        "unavailable": "UNAVAILABLE",
    },
    "bilingual": {
        "critical": "严重异常 · CRITICAL",
        "warning": "服务降级 · DEGRADED",
        "maintenance": "计划维护 · MAINTENANCE",
        "info": "状态提醒 · NOTICE",
        "operational": "运行正常 · OPERATIONAL",
        "unavailable": "数据不可用 · UNAVAILABLE",
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


@lru_cache(maxsize=160)
def _svg_icon(name: str, size: int, current_color: str = "#FFFFFF") -> Image.Image:
    """Rasterize a controlled local SVG asset without native dependencies.

    The bundled icon set intentionally uses only SVG geometric primitives. This
    keeps plugin installation portable while retaining SVG as the source of truth.

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
    scale_x = size / width
    scale_y = size / height
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def point(x: str | float, y: str | float) -> tuple[int, int]:
        return (
            round((float(x) - left) * scale_x),
            round((float(y) - top) * scale_y),
        )

    def render(element: ElementTree.Element, inherited: dict[str, str]) -> None:
        attrs = dict(inherited)
        attrs.update(element.attrib)
        tag = element.tag.rsplit("}", 1)[-1]
        fill = _parse_color(attrs.get("fill", "#000000"), current_color)
        stroke = _parse_color(attrs.get("stroke"), current_color)
        stroke_width = max(
            1,
            round(float(attrs.get("stroke-width", "1")) * min(scale_x, scale_y)),
        )
        if tag == "rect":
            x1, y1 = point(attrs.get("x", 0), attrs.get("y", 0))
            x2, y2 = point(
                float(attrs.get("x", 0)) + float(attrs.get("width", 0)),
                float(attrs.get("y", 0)) + float(attrs.get("height", 0)),
            )
            radius = round(float(attrs.get("rx", 0)) * min(scale_x, scale_y))
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
    top = tuple(bytes.fromhex(BACKGROUND_TOP.removeprefix("#")))
    bottom = tuple(bytes.fromhex(BACKGROUND_BOTTOM.removeprefix("#")))
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(
            round(top[index] * (1 - ratio) + bottom[index] * ratio)
            for index in range(3)
        )
        draw.line((0, y, WIDTH, y), fill=(*color, 255))
    glow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse((-220, -300, 520, 440), fill=(55, 94, 246, 50))
    glow_draw.ellipse((760, -240, 1370, 390), fill=(124, 58, 237, 42))
    glow = glow.filter(ImageFilter.GaussianBlur(90))
    image.alpha_composite(glow)
    return image


def _draw_panel(
    image: Image.Image,
    bounds: tuple[int, int, int, int],
    fill: str = SURFACE,
    radius: int = 28,
) -> None:
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    x1, y1, x2, y2 = bounds
    shadow_draw.rounded_rectangle(
        (x1 + 2, y1 + 9, x2 + 2, y2 + 12),
        radius=radius,
        fill=(0, 0, 0, 90),
    )
    image.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(bounds, radius=radius, fill=fill, outline=BORDER, width=2)
    draw.line((x1 + radius, y1 + 1, x2 - radius, y1 + 1), fill="#324665", width=1)


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


def _badge(
    image: Image.Image,
    position: tuple[int, int],
    text: str,
    color: str,
    icon_name: str,
) -> int:
    draw = ImageDraw.Draw(image)
    font = _font(21, True)
    x, y = position
    width = int(_text_width(text, font)) + 65
    draw.rounded_rectangle((x, y, x + width, y + 40), radius=20, fill=color)
    _paste_icon(image, icon_name, (x + 12, y + 10), 20, "#101728")
    draw.text((x + 41, y + 6), text, font=font, fill="#101728")
    return width


def _event_layout(
    stage: str,
    issue: Issue,
    translations: dict[str, str],
    language: str,
) -> dict[str, object]:
    title, title_original = _localized_pair(issue.title, translations, language)
    title_lines = _wrap_text(title, _font(34, True), 980, 3)
    title_original_lines = _wrap_text(title_original, _font(21), 980, 2)

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
    service_lines = _wrap_text(service_text, _font(24), 936, 2)
    service_original_lines = _wrap_text(service_original, _font(19), 936, 2)

    detail, detail_original = _localized_pair(issue.detail, translations, language)
    detail_lines = _wrap_text(detail, _font(24), 980, 6)
    detail_original_lines = _wrap_text(detail_original, _font(19), 980, 5)

    height = 91 + len(title_lines) * 45 + len(title_original_lines) * 29
    if service_lines:
        height += 56 + len(service_lines) * 34 + len(service_original_lines) * 27
    if detail_lines:
        height += 28 + len(detail_lines) * 34 + len(detail_original_lines) * 27
    height += 70
    return {
        "stage": stage,
        "issue": issue,
        "title_lines": title_lines,
        "title_original_lines": title_original_lines,
        "service_lines": service_lines,
        "service_original_lines": service_original_lines,
        "detail_lines": detail_lines,
        "detail_original_lines": detail_original_lines,
        "height": max(height, 235),
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
    height = 210 + sum(int(layout["height"]) + 26 for layout in layouts) + 68
    image = _new_canvas(height)
    draw = ImageDraw.Draw(image)

    source_id = events[0][1].source_id
    vendor_color = VENDOR_COLORS.get(source_id, "#3B82F6")
    draw.rounded_rectangle((48, 36, 142, 130), radius=28, fill=vendor_color)
    draw.rounded_rectangle((49, 37, 141, 129), radius=27, outline="#FFFFFF55", width=2)
    _paste_icon(image, _vendor_icon(source_id), (67, 55), 56)
    draw.text((172, 39), source_name, font=_font(43, True), fill=TEXT)
    header_subtitle = {
        "zh-CN": "官方服务状态变更",
        "en-US": "OFFICIAL SERVICE STATUS UPDATE",
        "bilingual": "官方服务状态变更  ·  OFFICIAL STATUS UPDATE",
    }[language]
    draw.text((174, 98), header_subtitle, font=_font(23), fill=MUTED)
    draw.rounded_rectangle((48, 161, WIDTH - 48, 164), radius=2, fill="#30425F")

    y = 192
    for layout in layouts:
        stage = str(layout["stage"])
        issue = layout["issue"]
        assert isinstance(issue, Issue)
        block_height = int(layout["height"])
        severity = "operational" if stage == "recovered" else issue.severity
        accent = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["warning"])
        _draw_panel(image, (48, y, WIDTH - 48, y + block_height))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (48, y + 24, 56, y + block_height - 24), radius=4, fill=accent
        )
        _badge(
            image,
            (82, y + 25),
            STAGE_LABELS[language].get(stage, stage),
            accent,
            _stage_icon(stage),
        )
        severity_label = STATUS_LABELS[language].get(severity, severity)
        severity_font = _font(21, True)
        severity_width = int(_text_width(severity_label, severity_font)) + 48
        severity_x = WIDTH - 82 - severity_width
        draw.rounded_rectangle(
            (severity_x, y + 26, WIDTH - 82, y + 65),
            radius=19,
            fill="#0A1324",
            outline=accent,
            width=1,
        )
        _paste_icon(
            image, _status_icon(severity), (severity_x + 11, y + 36), 18, accent
        )
        draw.text(
            (severity_x + 35, y + 32),
            severity_label,
            font=severity_font,
            fill=accent,
        )

        cursor = y + 89
        cursor = _draw_lines(
            draw, layout["title_lines"], (82, cursor), _font(34, True), TEXT, 45
        )
        original_lines = layout["title_original_lines"]
        if original_lines:
            cursor += 2
            cursor = _draw_lines(
                draw, original_lines, (82, cursor), _font(21), MUTED, 29
            )

        service_lines = layout["service_lines"]
        if service_lines:
            cursor += 16
            _paste_icon(image, "service", (82, cursor + 2), 25, accent)
            service_label = {
                "zh-CN": "受影响服务",
                "en-US": "AFFECTED SERVICES",
                "bilingual": "受影响服务 · AFFECTED SERVICES",
            }[language]
            draw.text((118, cursor), service_label, font=_font(20, True), fill=accent)
            cursor += 31
            cursor = _draw_lines(
                draw, service_lines, (118, cursor), _font(24), TEXT_SOFT, 34
            )
            service_original_lines = layout["service_original_lines"]
            if service_original_lines:
                cursor = _draw_lines(
                    draw, service_original_lines, (118, cursor), _font(19), MUTED, 27
                )

        detail_lines = layout["detail_lines"]
        if detail_lines:
            cursor += 17
            draw.rounded_rectangle(
                (82, cursor, WIDTH - 82, cursor + 2), radius=1, fill="#283A57"
            )
            cursor += 17
            cursor = _draw_lines(
                draw, detail_lines, (82, cursor), _font(24), TEXT_SOFT, 34
            )
            detail_original_lines = layout["detail_original_lines"]
            if detail_original_lines:
                cursor += 3
                cursor = _draw_lines(
                    draw, detail_original_lines, (82, cursor), _font(19), MUTED, 27
                )

        meta_y = y + block_height - 49
        if issue.updated_at:
            _paste_icon(image, "clock", (82, meta_y), 22, MUTED)
            draw.text((113, meta_y - 1), issue.updated_at, font=_font(19), fill=MUTED)
        if issue.status_url:
            parsed = urlparse(issue.status_url)
            url_text = issue.status_url
            if len(url_text) > 58:
                url_text = f"{parsed.scheme}://{parsed.netloc}/…"
            url_width = _text_width(url_text, _font(19))
            link_x = WIDTH - 82 - int(url_width)
            _paste_icon(image, "link", (link_x - 31, meta_y), 22, "#7CB5FF")
            draw.text((link_x, meta_y - 1), url_text, font=_font(19), fill="#7CB5FF")
        y += block_height + 26

    footer = {
        "zh-CN": "AstrBot 全球状态监控  ·  数据来自厂商官方状态页",
        "en-US": "ASTRBOT GLOBAL STATUS  ·  OFFICIAL VENDOR DATA",
        "bilingual": "AstrBot 全球状态监控  ·  OFFICIAL VENDOR DATA",
    }[language]
    footer_width = _text_width(footer, _font(19))
    draw.text(
        ((WIDTH - footer_width) / 2, height - 43), footer, font=_font(19), fill=MUTED
    )
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
            primary_lines = _wrap_text(primary, _font(22, True), 680, 2)
            original_lines = _wrap_text(original, _font(17), 680, 1)
        row_height = 112 + len(primary_lines) * 30 + len(original_lines) * 23
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
            {"result": None, "primary_lines": [], "original_lines": [], "height": 112}
        ]
    height = 206 + sum(int(row["height"]) + 18 for row in rows) + 67
    image = _new_canvas(height)
    draw = ImageDraw.Draw(image)
    draw.text(
        (52, 38),
        "全球厂商服务状态" if language != "en-US" else "GLOBAL VENDOR STATUS",
        font=_font(45, True),
        fill=TEXT,
    )
    subtitle = {
        "zh-CN": "AI · 云服务 · 开发者基础设施",
        "en-US": "AI · CLOUD · DEVELOPER INFRASTRUCTURE",
        "bilingual": "AI · 云服务 · CLOUD · DEVELOPER INFRASTRUCTURE",
    }[language]
    draw.text((54, 99), subtitle, font=_font(23), fill=MUTED)
    live_label = (
        "实时查询 · LIVE"
        if language == "bilingual"
        else ("实时查询" if language == "zh-CN" else "LIVE QUERY")
    )
    live_width = int(_text_width(live_label, _font(20, True))) + 61
    live_x = WIDTH - 52 - live_width
    draw.rounded_rectangle(
        (live_x, 56, WIDTH - 52, 98),
        radius=21,
        fill="#122943",
        outline="#315B7B",
        width=1,
    )
    _paste_icon(image, "update", (live_x + 14, 67), 20, "#67C7FF")
    draw.text((live_x + 42, 63), live_label, font=_font(20, True), fill="#8DD5FF")
    draw.rounded_rectangle((52, 158, WIDTH - 52, 161), radius=2, fill="#30425F")

    y = 188
    for index, row in enumerate(rows):
        result = row["result"]
        row_height = int(row["height"])
        _draw_panel(
            image,
            (52, y, WIDTH - 52, y + row_height),
            SURFACE if index % 2 == 0 else SURFACE_ALT,
            24,
        )
        draw = ImageDraw.Draw(image)
        if result is None:
            _paste_icon(image, "unavailable", (82, y + 34), 42, MUTED)
            empty_text = (
                "没有启用任何状态来源"
                if language != "en-US"
                else "NO STATUS SOURCES ENABLED"
            )
            draw.text((144, y + 35), empty_text, font=_font(26), fill=MUTED)
            y += row_height + 18
            continue
        assert isinstance(result, SourceResult)
        severity = result.severity
        accent = SEVERITY_COLORS.get(severity, SEVERITY_COLORS["unavailable"])
        vendor_color = VENDOR_COLORS.get(result.spec.source_id, "#3B82F6")
        draw.rounded_rectangle((76, y + 22, 140, y + 86), radius=20, fill=vendor_color)
        _paste_icon(image, _vendor_icon(result.spec.source_id), (90, y + 36), 36)
        draw.text((164, y + 18), result.spec.name, font=_font(28, True), fill=TEXT)
        if result.success:
            if result.issues:
                subtitle_text = (
                    f"{len(result.issues)} 个活动事件"
                    if language != "en-US"
                    else f"{len(result.issues)} ACTIVE INCIDENT(S)"
                )
                if language == "bilingual":
                    subtitle_text += " · ACTIVE"
            else:
                subtitle_text = (
                    "未发现活动异常 · ALL SYSTEMS NORMAL"
                    if language == "bilingual"
                    else (
                        "未发现活动异常"
                        if language == "zh-CN"
                        else "NO ACTIVE INCIDENTS"
                    )
                )
        else:
            subtitle_text = (
                "本次查询失败，不视为服务故障"
                if language == "zh-CN"
                else "QUERY FAILED · NOT TREATED AS AN OUTAGE"
            )
            if language == "bilingual":
                subtitle_text = "查询失败 · QUERY FAILED · 不视为故障"
        draw.text((164, y + 55), subtitle_text, font=_font(19), fill=MUTED)

        label = STATUS_LABELS[language].get(severity, severity)
        label_font = _font(19, True)
        label_width = int(_text_width(label, label_font)) + 53
        label_x = WIDTH - 76 - label_width
        draw.rounded_rectangle(
            (label_x, y + 32, WIDTH - 76, y + 74),
            radius=21,
            fill="#091322",
            outline=accent,
            width=1,
        )
        _paste_icon(image, _status_icon(severity), (label_x + 12, y + 43), 20, accent)
        draw.text((label_x + 39, y + 38), label, font=label_font, fill=accent)

        cursor = y + 94
        primary_lines = row["primary_lines"]
        if primary_lines:
            cursor = _draw_lines(
                draw, primary_lines, (164, cursor), _font(22, True), TEXT_SOFT, 30
            )
        original_lines = row["original_lines"]
        if original_lines:
            _draw_lines(draw, original_lines, (164, cursor), _font(17), MUTED, 23)
        y += row_height + 18

    footer = {
        "zh-CN": "实时查询  ·  数据来自厂商官方状态接口",
        "en-US": "LIVE QUERY  ·  OFFICIAL VENDOR STATUS APIS",
        "bilingual": "实时查询 · LIVE  ·  OFFICIAL VENDOR STATUS APIS",
    }[language]
    footer_width = _text_width(footer, _font(19))
    draw.text(
        ((WIDTH - footer_width) / 2, height - 43), footer, font=_font(19), fill=MUTED
    )
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
