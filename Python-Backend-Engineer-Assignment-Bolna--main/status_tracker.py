"""
OpenAI Status Page Tracker
==========================

An async, event-driven service that monitors the OpenAI Status Page
(https://status.openai.com/) for incidents, outages, and degradation updates.

Architecture:
  - Uses the Atom feed at /history.atom for structured incident data
  - Employs HTTP conditional requests (ETag / If-None-Match) so the server
    returns 304 Not Modified when nothing has changed — zero wasted bandwidth
  - asyncio event loop allows monitoring 100+ status pages concurrently
    on a single thread
  - Reactor pattern: callbacks fire only when new or updated incidents
    are detected

Usage:
  pip install -r requirements.txt
  python status_tracker.py
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable

import aiohttp
import feedparser

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Utility – strip HTML tags from feed content
# ---------------------------------------------------------------------------
class _HTMLStripper(HTMLParser):
    """Minimal HTML-to-text converter."""

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str):
        self._parts.append(data)

    def get_text(self) -> str:
        return "".join(self._parts).strip()


def strip_html(html: str) -> str:
    """Return plain text from an HTML string."""
    stripper = _HTMLStripper()
    stripper.feed(html)
    return stripper.get_text()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
class IncidentUpdate:
    """Represents a parsed incident from the status page feed."""

    __slots__ = (
        "id",
        "title",
        "link",
        "status",
        "affected_components",
        "latest_message",
        "updated_at",
    )

    def __init__(
        self,
        id: str,
        title: str,
        link: str,
        status: str,
        affected_components: list[str],
        latest_message: str,
        updated_at: datetime,
    ):
        self.id = id
        self.title = title
        self.link = link
        self.status = status
        self.affected_components = affected_components
        self.latest_message = latest_message
        self.updated_at = updated_at

    def __repr__(self) -> str:
        return f"IncidentUpdate(id={self.id!r}, title={self.title!r})"


# ---------------------------------------------------------------------------
# Feed parser helpers
# ---------------------------------------------------------------------------
def _parse_components(entry) -> list[str]:
    """
    Extract affected component names from an Atom entry.

    incident.io feeds store component info in <category> tags or
    in the entry content/summary.  We try both approaches.
    """
    components: list[str] = []

    # Method 1: <category> tags (standard Atom)
    if hasattr(entry, "tags") and entry.tags:
        for tag in entry.tags:
            term = tag.get("term", "")
            if term:
                components.append(term)

    # Method 2: Parse from content/summary text
    if not components:
        raw = ""
        if hasattr(entry, "content") and entry.content:
            raw = entry.content[0].get("value", "")
        elif hasattr(entry, "summary"):
            raw = entry.summary or ""

        text = strip_html(raw)
        # Look for lines matching "ComponentName (Status)" pattern
        for line in text.split("\n"):
            line = line.strip("- ").strip()
            if "(" in line and ")" in line:
                components.append(line)

    return components


def _parse_status(entry) -> str:
    """Determine the current status string for an incident entry."""
    # Check for explicit status in tags
    if hasattr(entry, "tags") and entry.tags:
        for tag in entry.tags:
            label = tag.get("label", "").lower()
            if label in (
                "investigating",
                "identified",
                "monitoring",
                "resolved",
                "degraded performance",
                "partial outage",
                "major outage",
            ):
                return tag.get("label", label)

    # Fallback: extract from content
    raw = ""
    if hasattr(entry, "content") and entry.content:
        raw = entry.content[0].get("value", "")
    elif hasattr(entry, "summary"):
        raw = entry.summary or ""

    text = strip_html(raw)

    # Keyword-based detection
    lower = text.lower()
    if "resolved" in lower or "fully recovered" in lower:
        return "Resolved"
    if "monitoring" in lower:
        return "Monitoring"
    if "identified" in lower:
        return "Identified"
    if "investigating" in lower:
        return "Investigating"
    if "degraded" in lower:
        return "Degraded Performance"

    return "Unknown"


def _parse_latest_message(entry) -> str:
    """Extract the most recent human-readable update message."""
    raw = ""
    if hasattr(entry, "content") and entry.content:
        raw = entry.content[0].get("value", "")
    elif hasattr(entry, "summary"):
        raw = entry.summary or ""

    text = strip_html(raw)

    # The first meaningful sentence is usually the latest update
    for line in text.split("\n"):
        line = line.strip()
        if line and "(" not in line:
            return line

    return text[:200] if text else "(no message)"


def _parse_timestamp(entry) -> datetime:
    """Parse the updated/published timestamp from the entry."""
    time_struct = entry.get("updated_parsed") or entry.get("published_parsed")
    if time_struct:
        from calendar import timegm

        return datetime.fromtimestamp(timegm(time_struct), tz=timezone.utc)
    return datetime.now(tz=timezone.utc)


def parse_incident(entry) -> IncidentUpdate:
    """Convert a feedparser entry into an IncidentUpdate."""
    return IncidentUpdate(
        id=entry.get("id", entry.get("link", "")),
        title=entry.get("title", "Unknown Incident"),
        link=entry.get("link", ""),
        status=_parse_status(entry),
        affected_components=_parse_components(entry),
        latest_message=_parse_latest_message(entry),
        updated_at=_parse_timestamp(entry),
    )


# ---------------------------------------------------------------------------
# Console callback (default event handler)
# ---------------------------------------------------------------------------
def on_incident_update(page_name: str, incident: IncidentUpdate) -> None:
    """Default callback — pretty-prints an incident update to stdout."""
    timestamp = incident.updated_at.strftime("%Y-%m-%d %H:%M:%S")

    print()
    print("=" * 70)

    if incident.affected_components:
        for component in incident.affected_components:
            print(f"[{timestamp}] Product: {page_name} - {component}")
    else:
        print(f"[{timestamp}] Product: {page_name}")

    print(f"  Incident: {incident.title}")
    print(f"  Status:   {incident.status}")
    print(f"  Message:  {incident.latest_message}")
    print(f"  Link:     {incident.link}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Core monitor class
# ---------------------------------------------------------------------------
class StatusPageMonitor:
    """
    Async monitor for an incident.io-powered status page.

    Uses HTTP conditional requests (ETag) so the server returns
    304 Not Modified when the feed hasn't changed — no wasted bandwidth.

    Multiple instances can run concurrently in a single asyncio loop,
    making it trivial to scale to 100+ status pages.
    """

    def __init__(
        self,
        name: str,
        feed_url: str,
        callback: Callable[[str, IncidentUpdate], None] | None = None,
        poll_interval: int = 60,
    ):
        """
        Parameters
        ----------
        name : str
            Human-readable name for this status page (e.g. "OpenAI API").
        feed_url : str
            URL to the Atom/RSS feed (e.g. https://status.openai.com/history.atom).
        callback : callable, optional
            Function called with (page_name, IncidentUpdate) on each new/updated
            incident.  Defaults to on_incident_update (console printer).
        poll_interval : int
            Seconds between conditional-fetch cycles.  Default 60.
        """
        self.name = name
        self.feed_url = feed_url
        self.callback = callback or on_incident_update
        self.poll_interval = poll_interval

        # Conditional-request state
        self._etag: str | None = None
        self._last_modified: str | None = None

        # Change-detection state: maps incident ID -> last-seen updated timestamp
        self._seen_incidents: dict[str, str] = {}

        # Graceful shutdown
        self._running = False

    async def start(self) -> None:
        """Begin the async monitoring loop."""
        self._running = True
        logger.info(
            ">> Starting monitor for '%s' (feed: %s, interval: %ds)",
            self.name,
            self.feed_url,
            self.poll_interval,
        )

        async with aiohttp.ClientSession() as session:
            # Initial fetch — show all current incidents
            await self._check_feed(session, initial=True)

            while self._running:
                await asyncio.sleep(self.poll_interval)
                if not self._running:
                    break
                await self._check_feed(session, initial=False)

        logger.info("-- Monitor for '%s' stopped.", self.name)

    def stop(self) -> None:
        """Signal the monitor to stop after the current cycle."""
        self._running = False

    async def _check_feed(
        self, session: aiohttp.ClientSession, initial: bool = False
    ) -> None:
        """Fetch the feed (conditionally) and process any new incidents."""
        headers: dict[str, str] = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        try:
            async with session.get(
                self.feed_url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 304:
                    logger.debug(
                        "No changes for '%s' (304 Not Modified).", self.name
                    )
                    return

                if resp.status != 200:
                    logger.warning(
                        "Unexpected status %d from '%s'.", resp.status, self.name
                    )
                    return

                # Update conditional-request tokens
                self._etag = resp.headers.get("ETag")
                self._last_modified = resp.headers.get("Last-Modified")

                body = await resp.text()

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error("Network error for '%s': %s", self.name, exc)
            return

        # Parse
        feed = feedparser.parse(body)
        if feed.bozo and not feed.entries:
            logger.warning("Malformed feed from '%s': %s", self.name, feed.bozo_exception)
            return

        self._process_entries(feed.entries, initial)

    def _process_entries(self, entries: list, initial: bool) -> None:
        """Detect new/updated incidents and fire callbacks."""
        new_seen: dict[str, str] = {}

        for entry in entries:
            incident = parse_incident(entry)
            updated_key = incident.updated_at.isoformat()
            new_seen[incident.id] = updated_key

            # On initial run, show all incidents to give immediate context
            if initial:
                self.callback(self.name, incident)
                continue

            # On subsequent runs, only fire if this is new or updated
            prev = self._seen_incidents.get(incident.id)
            if prev is None:
                logger.info("[NEW] Incident detected: %s", incident.title)
                self.callback(self.name, incident)
            elif prev != updated_key:
                logger.info("[UPDATED] Incident updated: %s", incident.title)
                self.callback(self.name, incident)

        self._seen_incidents = new_seen


# ---------------------------------------------------------------------------
# Multi-page runner
# ---------------------------------------------------------------------------
async def run_monitors(monitors: list[StatusPageMonitor]) -> None:
    """Run multiple status page monitors concurrently."""
    # Handle graceful shutdown on SIGINT / SIGTERM
    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("Shutdown signal received — stopping all monitors…")
        for m in monitors:
            m.stop()

    # Register signal handlers (Unix-only; on Windows we rely on KeyboardInterrupt)
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _shutdown)

    tasks = [asyncio.create_task(m.start()) for m in monitors]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """Entry point — configure monitors and run."""
    print("+--------------------------------------------------------------+")
    print("|          OpenAI Status Page Tracker (Event-Driven)           |")
    print("|          Monitoring: https://status.openai.com/              |")
    print("|          Press Ctrl+C to stop                                |")
    print("+--------------------------------------------------------------+")
    print()

    # ── Configure monitors ─────────────────────────────────────────────
    # You can add more monitors here to track additional status pages.
    # Each runs concurrently in the same asyncio event loop.

    monitors = [
        StatusPageMonitor(
            name="OpenAI API",
            feed_url="https://status.openai.com/history.atom",
            poll_interval=60,  # seconds between conditional checks
        ),
        # ── Examples: add more providers ────────────────────────────────
        # StatusPageMonitor(
        #     name="GitHub",
        #     feed_url="https://www.githubstatus.com/history.atom",
        #     poll_interval=60,
        # ),
        # StatusPageMonitor(
        #     name="Stripe",
        #     feed_url="https://status.stripe.com/history.atom",
        #     poll_interval=60,
        # ),
    ]

    logger.info("Configured %d monitor(s). Starting…", len(monitors))

    try:
        asyncio.run(run_monitors(monitors))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shutting down.")


if __name__ == "__main__":
    main()
