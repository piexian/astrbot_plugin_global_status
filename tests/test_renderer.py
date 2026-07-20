from io import BytesIO

from PIL import Image

from data.plugins.astrbot_plugin_global_status.renderer import (
    ICON_DIR,
    PROJECT_SIGNATURE,
    _font,
    _svg_icon,
    _text_width,
    _wrap_text,
    build_alert_fallback,
    render_alert_card,
    render_overview,
)
from data.plugins.astrbot_plugin_global_status.sources import (
    Issue,
    SourceResult,
    SourceSpec,
)


def test_render_alert_card_returns_valid_dynamic_png():
    issue = Issue(
        source_id="openai",
        source_name="OpenAI",
        issue_id="incident_1",
        severity="critical",
        title="Elevated errors affecting API requests",
        affected_services=("API", "ChatGPT", "Responses"),
        detail="We are investigating elevated error rates affecting a subset of requests. " * 8,
        updated_at="2026-07-20T01:00:00Z",
        status_url="https://status.openai.com/",
    )

    data = render_alert_card("OpenAI", [("new", issue), ("recovered", issue)])
    image = Image.open(BytesIO(data))

    assert image.format == "PNG"
    assert image.width == 1200
    assert image.height > 500
    assert PROJECT_SIGNATURE == "Futureppo/astrbot_plugin_global_status"


def test_svg_icon_assets_rasterize_without_native_dependencies():
    expected = {
        "openai",
        "claude",
        "google_vertex_gemini",
        "groq",
        "cohere",
        "aws",
        "azure",
        "github",
        "cloudflare",
        "vendor",
        "alert",
        "update",
        "check",
        "service",
        "clock",
        "link",
        "unavailable",
    }

    assert expected <= {path.stem for path in ICON_DIR.glob("*.svg")}
    for name in expected:
        icon = _svg_icon(name, 48, "#34D399")
        assert icon.mode == "RGBA"
        assert icon.size == (48, 48)
        assert icon.getbbox() is not None


def test_bilingual_alert_preserves_english_and_localizes_fallback():
    issue = Issue(
        source_id="openai",
        source_name="OpenAI",
        issue_id="incident_2",
        severity="warning",
        title="Elevated API errors",
        detail="We are investigating.",
        status_url="https://status.openai.com/",
    )
    translations = {
        issue.title: "API 错误率升高",
        issue.detail: "我们正在调查。",
    }

    for language in ("zh-CN", "en-US", "bilingual"):
        data = render_alert_card(
            "OpenAI", [("update", issue)], translations, language
        )
        image = Image.open(BytesIO(data))
        assert image.format == "PNG"
        assert image.width == 1200

    fallback = build_alert_fallback(
        "OpenAI", [("update", issue)], translations, "bilingual"
    )
    assert "API 错误率升高" in fallback
    assert "Elevated API errors" in fallback


def test_long_custom_vendor_name_is_bounded_in_alert_header():
    source_name = (
        "A Very Long Custom Statuspage Vendor Name / "
        "超长自定义厂商名称用于边界压力测试"
    )
    source_name_lines = _wrap_text(source_name, _font(40, True), 610, 1)
    issue = Issue(
        source_id="custom_extreme_vendor",
        source_name=source_name,
        issue_id="custom-1",
        severity="critical",
        title="Major service disruption",
        status_url="https://status.example.com/",
    )

    data = render_alert_card(source_name, [("new", issue)])
    image = Image.open(BytesIO(data))

    assert image.format == "PNG"
    assert len(source_name_lines) == 1
    assert source_name_lines[0].endswith("…")
    assert _text_width(source_name_lines[0], _font(40, True)) <= 610


def test_render_overview_includes_operational_and_unavailable_rows():
    ok_spec = SourceSpec("ok", "Operational", "statuspage", "url", "url")
    bad_spec = SourceSpec("bad", "Unavailable", "rss", "url", "url")
    results = [
        SourceResult(spec=ok_spec, success=True),
        SourceResult(spec=bad_spec, success=False, error="timeout"),
    ]

    data = render_overview(results)
    image = Image.open(BytesIO(data))

    assert image.format == "PNG"
    assert image.size[0] == 1200
    assert image.size[1] >= 400
