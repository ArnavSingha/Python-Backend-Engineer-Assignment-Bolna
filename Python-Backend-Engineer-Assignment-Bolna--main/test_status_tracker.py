"""
Unit tests for the OpenAI Status Page Tracker.

Tests cover:
  - Atom feed entry parsing (incidents, components, status, messages)
  - Change-detection logic (new vs. already-seen incidents)
  - Callback invocation behaviour
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from calendar import timegm
from time import struct_time

import pytest

from status_tracker import (
    IncidentUpdate,
    StatusPageMonitor,
    parse_incident,
    strip_html,
    on_incident_update,
)


# ---------------------------------------------------------------------------
# Fixtures – synthetic feed entries
# ---------------------------------------------------------------------------
def _make_entry(
    id: str = "https://status.openai.com/incidents/test123",
    title: str = "Test Incident",
    link: str = "https://status.openai.com/incidents/test123",
    summary: str = "We are investigating the issue.",
    updated: tuple = (2025, 11, 3, 14, 32, 0, 0, 307, 0),
    tags: list | None = None,
    content: list | None = None,
):
    """Create a mock feedparser entry."""
    entry = MagicMock()
    entry.get = lambda k, d=None: {
        "id": id,
        "title": title,
        "link": link,
        "updated_parsed": struct_time(updated),
        "published_parsed": struct_time(updated),
    }.get(k, d)
    entry.tags = tags or []
    entry.summary = summary
    entry.content = content or []
    return entry


# ---------------------------------------------------------------------------
# Tests – HTML stripping
# ---------------------------------------------------------------------------
class TestStripHtml:
    def test_plain_text_unchanged(self):
        assert strip_html("Hello world") == "Hello world"

    def test_removes_tags(self):
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_empty_string(self):
        assert strip_html("") == ""

    def test_nested_tags(self):
        result = strip_html("<div><p>Line 1</p><p>Line 2</p></div>")
        assert "Line 1" in result
        assert "Line 2" in result


# ---------------------------------------------------------------------------
# Tests – parse_incident
# ---------------------------------------------------------------------------
class TestParseIncident:
    def test_basic_parsing(self):
        entry = _make_entry(
            title="Elevated Error Rate for ChatGPT",
            summary="We are investigating the issue.",
        )
        incident = parse_incident(entry)

        assert isinstance(incident, IncidentUpdate)
        assert incident.title == "Elevated Error Rate for ChatGPT"
        assert incident.id == "https://status.openai.com/incidents/test123"
        assert incident.link == "https://status.openai.com/incidents/test123"

    def test_status_investigating(self):
        entry = _make_entry(summary="We are investigating the issue.")
        incident = parse_incident(entry)
        assert incident.status == "Investigating"

    def test_status_resolved(self):
        entry = _make_entry(
            summary="All impacted services have now fully recovered."
        )
        incident = parse_incident(entry)
        assert incident.status == "Resolved"

    def test_status_monitoring(self):
        entry = _make_entry(
            summary="We have applied the fix and are monitoring recovery."
        )
        incident = parse_incident(entry)
        assert incident.status == "Monitoring"

    def test_status_degraded(self):
        entry = _make_entry(
            summary="Degraded performance due to upstream issue."
        )
        incident = parse_incident(entry)
        assert incident.status == "Degraded Performance"

    def test_timestamp_parsing(self):
        entry = _make_entry(updated=(2025, 11, 3, 14, 32, 0, 0, 307, 0))
        incident = parse_incident(entry)
        assert incident.updated_at.year == 2025
        assert incident.updated_at.month == 11
        assert incident.updated_at.day == 3

    def test_components_from_tags(self):
        tags = [
            {"term": "Chat Completions", "label": ""},
            {"term": "Responses", "label": ""},
        ]
        entry = _make_entry(tags=tags)
        incident = parse_incident(entry)
        assert "Chat Completions" in incident.affected_components
        assert "Responses" in incident.affected_components

    def test_components_from_content(self):
        entry = _make_entry(
            summary="- Conversations (Degraded Performance)\n- Login (Operational)",
            tags=None,
        )
        incident = parse_incident(entry)
        assert len(incident.affected_components) >= 1

    def test_latest_message_extraction(self):
        entry = _make_entry(
            summary="All impacted services have now fully recovered."
        )
        incident = parse_incident(entry)
        assert "recovered" in incident.latest_message.lower()


# ---------------------------------------------------------------------------
# Tests – StatusPageMonitor change detection
# ---------------------------------------------------------------------------
class TestChangeDetection:
    def test_initial_run_fires_all(self):
        """On the first run, all entries should trigger callbacks."""
        fired = []

        def cb(name, incident):
            fired.append(incident)

        monitor = StatusPageMonitor(
            name="Test",
            feed_url="http://example.com/feed.atom",
            callback=cb,
        )

        entries = [
            _make_entry(id="inc1", title="Incident 1"),
            _make_entry(id="inc2", title="Incident 2"),
        ]

        monitor._process_entries(entries, initial=True)
        assert len(fired) == 2

    def test_no_change_no_callback(self):
        """If same entries appear with same timestamps, no callbacks fire."""
        fired = []

        def cb(name, incident):
            fired.append(incident)

        monitor = StatusPageMonitor(
            name="Test",
            feed_url="http://example.com/feed.atom",
            callback=cb,
        )

        entries = [_make_entry(id="inc1", title="Incident 1")]

        # Initial run
        monitor._process_entries(entries, initial=True)
        fired.clear()

        # Second run — same data
        monitor._process_entries(entries, initial=False)
        assert len(fired) == 0

    def test_new_incident_fires_callback(self):
        """A brand-new incident should fire a callback."""
        fired = []

        def cb(name, incident):
            fired.append(incident)

        monitor = StatusPageMonitor(
            name="Test",
            feed_url="http://example.com/feed.atom",
            callback=cb,
        )

        entries_v1 = [_make_entry(id="inc1", title="Incident 1")]
        monitor._process_entries(entries_v1, initial=True)
        fired.clear()

        entries_v2 = [
            _make_entry(id="inc1", title="Incident 1"),
            _make_entry(id="inc2", title="New Incident"),
        ]
        monitor._process_entries(entries_v2, initial=False)
        assert len(fired) == 1
        assert fired[0].title == "New Incident"

    def test_updated_incident_fires_callback(self):
        """An incident with a changed timestamp should fire a callback."""
        fired = []

        def cb(name, incident):
            fired.append(incident)

        monitor = StatusPageMonitor(
            name="Test",
            feed_url="http://example.com/feed.atom",
            callback=cb,
        )

        entries_v1 = [
            _make_entry(
                id="inc1",
                title="Incident 1",
                updated=(2025, 11, 3, 14, 0, 0, 0, 307, 0),
            )
        ]
        monitor._process_entries(entries_v1, initial=True)
        fired.clear()

        entries_v2 = [
            _make_entry(
                id="inc1",
                title="Incident 1",
                updated=(2025, 11, 3, 15, 0, 0, 0, 307, 0),  # 1 hour later
            )
        ]
        monitor._process_entries(entries_v2, initial=False)
        assert len(fired) == 1


# ---------------------------------------------------------------------------
# Tests – Console output callback
# ---------------------------------------------------------------------------
class TestConsoleCallback:
    def test_on_incident_update_prints(self, capsys):
        incident = IncidentUpdate(
            id="test1",
            title="Test Incident",
            link="https://example.com/test1",
            status="Investigating",
            affected_components=["Chat Completions", "Responses"],
            latest_message="We are investigating.",
            updated_at=datetime(2025, 11, 3, 14, 32, 0, tzinfo=timezone.utc),
        )

        on_incident_update("OpenAI API", incident)
        captured = capsys.readouterr().out

        assert "OpenAI API" in captured
        assert "Chat Completions" in captured
        assert "Investigating" in captured
        assert "2025-11-03" in captured


# ---------------------------------------------------------------------------
# Tests – Monitor construction
# ---------------------------------------------------------------------------
class TestMonitorInit:
    def test_defaults(self):
        m = StatusPageMonitor(name="Test", feed_url="http://example.com/feed")
        assert m.name == "Test"
        assert m.feed_url == "http://example.com/feed"
        assert m.poll_interval == 60
        assert m.callback is not None

    def test_custom_interval(self):
        m = StatusPageMonitor(
            name="Test", feed_url="http://example.com/feed", poll_interval=120
        )
        assert m.poll_interval == 120

    def test_custom_callback(self):
        cb = lambda n, i: None
        m = StatusPageMonitor(
            name="Test", feed_url="http://example.com/feed", callback=cb
        )
        assert m.callback is cb
