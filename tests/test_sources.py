from data.plugins.astrbot_plugin_global_status.sources import (
    Issue,
    SourceSpec,
    build_source_specs,
    parse_google_cloud,
    parse_rss,
    parse_statuspage,
)


def _statuspage_spec() -> SourceSpec:
    return SourceSpec(
        source_id="test",
        name="Test Vendor",
        kind="statuspage",
        endpoint="https://status.example.com",
        status_url="https://status.example.com/",
    )


def test_issue_fingerprint_is_stable_and_content_sensitive():
    issue = Issue("s", "Source", "1", "warning", "Title", detail="v1")
    same = Issue("s", "Source", "1", "warning", "Title", detail="v1")
    changed = Issue("s", "Source", "1", "critical", "Title", detail="v2")

    assert issue.fingerprint == same.fingerprint
    assert issue.fingerprint != changed.fingerprint
    assert Issue.from_dict(issue.to_dict()) == issue


def test_parse_statuspage_deduplicates_incident_components_and_filters_maintenance():
    summary = {
        "page": {"updated_at": "2026-07-20T01:00:00Z"},
        "status": {"indicator": "major", "description": "Major outage"},
        "components": [
            {"id": "api", "name": "API", "status": "major_outage"},
            {"id": "web", "name": "Web", "status": "degraded_performance"},
            {"id": "maint", "name": "Region A", "status": "under_maintenance"},
            {"id": "group", "name": "Products", "status": "partial_outage", "group": True},
        ],
    }
    incidents = {
        "incidents": [
            {
                "id": "active",
                "name": "API errors",
                "status": "investigating",
                "impact": "critical",
                "components": [{"id": "api", "name": "API"}],
                "incident_updates": [
                    {
                        "body": "<strong>Investigating</strong> elevated errors.",
                        "updated_at": "2026-07-20T01:05:00Z",
                    }
                ],
            },
            {
                "id": "resolved",
                "name": "Old issue",
                "status": "resolved",
                "impact": "minor",
            },
        ]
    }

    result = parse_statuspage(_statuspage_spec(), summary, incidents, False)

    assert result.success
    assert set(result.issues) == {"incident_active", "components"}
    assert result.issues["incident_active"].affected_services == ("API",)
    assert result.issues["incident_active"].detail == "Investigating elevated errors."
    assert result.issues["components"].affected_services == ("Web",)
    assert "incident_resolved" in result.resolved_issue_ids


def test_parse_statuspage_can_include_maintenance():
    summary = {
        "page": {"updated_at": "now"},
        "status": {"indicator": "maintenance", "description": "Maintenance"},
        "components": [
            {"id": "region", "name": "Region A", "status": "under_maintenance"}
        ],
    }

    result = parse_statuspage(_statuspage_spec(), summary, {"incidents": []}, True)

    assert result.issues["components"].severity == "maintenance"


def test_parse_google_cloud_keeps_only_active_vertex_or_gemini_incidents():
    spec = SourceSpec(
        "google",
        "Google Vertex AI / Gemini",
        "google",
        "https://status.cloud.google.com/incidents.json",
        "https://status.cloud.google.com/",
    )
    payload = [
        {
            "id": "active",
            "external_desc": "Gemini API elevated errors",
            "affected_products": [{"title": "Vertex AI"}],
            "status_impact": "SERVICE_DISRUPTION",
            "most_recent_update": {
                "status": "SERVICE_DISRUPTION",
                "text": "Requests may fail.",
                "modified": "2026-07-20T01:00:00Z",
            },
        },
        {
            "id": "resolved",
            "external_desc": "Vertex AI incident",
            "affected_products": [{"title": "Vertex AI"}],
            "end": "2026-07-20T02:00:00Z",
            "most_recent_update": {"status": "AVAILABLE"},
        },
        {
            "id": "irrelevant",
            "external_desc": "Cloud SQL incident",
            "affected_products": [{"title": "Cloud SQL"}],
            "most_recent_update": {"status": "SERVICE_DISRUPTION"},
        },
    ]

    result = parse_google_cloud(spec, payload)

    assert set(result.issues) == {"incident_active"}
    assert result.issues["incident_active"].severity == "critical"
    assert "incident_resolved" in result.resolved_issue_ids


def test_parse_rss_merges_updates_and_recognizes_resolution():
    spec = SourceSpec("aws", "AWS", "rss", "feed", "https://status.aws.amazon.com/")
    active_xml = """
    <rss><channel>
      <item><title>Service disruption: API errors</title>
        <guid>https://status.aws.amazon.com/#ec2-us-east-1_1000000000</guid>
        <pubDate>Mon, 20 Jul 2026 01:00:00 GMT</pubDate>
        <description>Initial update.</description></item>
      <item><title>Service disruption: API errors</title>
        <guid>https://status.aws.amazon.com/#ec2-us-east-1_1000000600</guid>
        <pubDate>Mon, 20 Jul 2026 01:10:00 GMT</pubDate>
        <description>Latest update.</description></item>
    </channel></rss>
    """
    active = parse_rss(spec, active_xml, False)

    assert len(active.issues) == 1
    issue_id, issue = next(iter(active.issues.items()))
    assert issue.detail == "Latest update."
    assert issue.severity == "critical"

    resolved_xml = """
    <rss><channel><item><title>Resolved: API errors</title>
      <guid>https://status.aws.amazon.com/#ec2-us-east-1_1000001200</guid>
      <pubDate>Mon, 20 Jul 2026 01:20:00 GMT</pubDate>
      <description>The issue has been resolved.</description>
    </item></channel></rss>
    """
    resolved = parse_rss(spec, resolved_xml, False)

    assert not resolved.issues
    assert issue_id in resolved.resolved_issue_ids


def test_build_source_specs_validates_and_deduplicates_custom_sources():
    specs = build_source_specs(
        {"openai": True, "claude": False},
        [
            {"name": "Custom", "base_url": "https://status.custom.test", "enabled": True},
            {"name": "Duplicate", "base_url": "https://status.openai.com", "enabled": True},
            {"name": "Invalid", "base_url": "not-a-url", "enabled": True},
        ],
    )

    assert any(spec.source_id == "openai" for spec in specs)
    assert not any(spec.source_id == "claude" for spec in specs)
    assert len([spec for spec in specs if spec.source_id.startswith("custom_")]) == 1
    by_id = {spec.source_id: spec for spec in specs}
    assert by_id["xai"].endpoint == "https://status.x.ai/feed.xml"
    assert by_id["xai"].kind == "rss"
    assert by_id["deepseek"].endpoint == "https://deepseek.statuspage.io"
    assert by_id["deepseek"].status_url == "https://status.deepseek.com/"
    assert by_id["moonshot"].endpoint == "https://status.moonshot.cn"
    assert by_id["moonshot"].kind == "statuspage"
    assert by_id["minimax"].endpoint == "https://status.minimaxi.com"
    assert by_id["minimax"].kind == "statuspage"


def test_xai_rss_categories_control_resolution_and_severity():
    spec = SourceSpec(
        "xai",
        "xAI",
        "rss",
        "https://status.x.ai/feed.xml",
        "https://status.x.ai/",
    )
    active_xml = """
    <rss><channel><item>
      <title>xAI API availability incident</title>
      <guid>incident-42</guid>
      <pubDate>Mon, 20 Jul 2026 01:00:00 GMT</pubDate>
      <description>Requests are failing.</description>
      <category>outage</category>
    </item></channel></rss>
    """
    active = parse_rss(spec, active_xml, False)
    issue_id, issue = next(iter(active.issues.items()))

    assert issue.severity == "critical"

    resolved_xml = """
    <rss><channel><item>
      <title>xAI API availability incident</title>
      <guid>incident-42</guid>
      <pubDate>Mon, 20 Jul 2026 01:10:00 GMT</pubDate>
      <description>Services are healthy.</description>
      <category>resolved</category>
    </item></channel></rss>
    """
    resolved = parse_rss(spec, resolved_xml, False)

    assert not resolved.issues
    assert issue_id in resolved.resolved_issue_ids
