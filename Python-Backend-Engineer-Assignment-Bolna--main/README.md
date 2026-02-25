# OpenAI Status Page Tracker

An **async, event-driven** Python service that automatically monitors the [OpenAI Status Page](https://status.openai.com/) for incidents, outages, and degradation updates. When a new or updated incident is detected, it prints the affected product/service and the latest status message to the console.

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                     asyncio Event Loop                       │
│                                                              │
│   ┌─────────────────┐  ┌─────────────────┐                  │
│   │ StatusPageMonitor│  │ StatusPageMonitor│  ... (N pages)  │
│   │  (OpenAI API)   │  │   (GitHub)      │                  │
│   └────────┬────────┘  └────────┬────────┘                  │
│            │                    │                             │
│   ┌────────▼────────────────────▼────────┐                  │
│   │  HTTP Conditional Request (ETag)      │                  │
│   │  → 304 Not Modified = skip            │                  │
│   │  → 200 OK = parse + diff              │                  │
│   └────────┬─────────────────────────────┘                  │
│            │                                                 │
│   ┌────────▼──────────────────────┐                         │
│   │  Change-Detection Reactor     │                         │
│   │  Compare incident IDs +       │                         │
│   │  timestamps vs. known state   │                         │
│   └────────┬──────────────────────┘                         │
│            │ (only on new/updated)                           │
│   ┌────────▼──────────────────────┐                         │
│   │  Event Callback               │                         │
│   │  → Console output / webhook   │                         │
│   └───────────────────────────────┘                         │
└──────────────────────────────────────────────────────────────┘
```

### Why This Is Event-Based (Not Naive Polling)

| Feature | Naive Polling | This Tracker |
|---------|--------------|--------------|
| Data transfer | Full response every cycle | **Zero bytes on 304** |
| Processing | Parse every cycle | **Parse only on change** |
| Callbacks | Emit everything | **Emit only new/updated incidents** |
| Concurrency | 1 thread per page | **N pages on 1 thread (asyncio)** |
| Scalability | Poor | **100+ pages, same event loop** |

The combination of **HTTP conditional requests** + **change-detection reactor** + **async I/O** creates an efficient event-driven system over standard HTTP infrastructure.

## Quick Start

### Prerequisites

- Python 3.10+

### Step 1: Create a Virtual Environment

```bash
python -m venv venv
```

### Step 2: Activate the Virtual Environment

**Linux / macOS:**

```bash
source venv/bin/activate
```

**Windows (PowerShell):**

```powershell
.\venv\Scripts\Activate
```

> **Note (Windows):** If you get an error about running scripts being disabled, run this first in the same terminal:
> ```powershell
> Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
> ```
> This only affects the current terminal session.

**Windows (Command Prompt):**

```cmd
venv\Scripts\activate.bat
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 4: Run the Tracker

```bash
python status_tracker.py
```

The tracker will start monitoring the OpenAI Status Page. Press **Ctrl+C** to stop.

### Example Output

```
╔══════════════════════════════════════════════════════════════╗
║          OpenAI Status Page Tracker (Event-Driven)         ║
║          Monitoring: https://status.openai.com/            ║
║          Press Ctrl+C to stop                              ║
╚══════════════════════════════════════════════════════════════╝

======================================================================
[2025-11-03 14:32:00] Product: OpenAI API - Chat Completions (Degraded Performance)
  Incident: Degraded performance due to upstream issue
  Status:   Degraded Performance
  Message:  We are investigating elevated error rates.
  Link:     https://status.openai.com/incidents/abc123
======================================================================
```

### Step 5: Run Tests

```bash
pip install pytest
python -m pytest test_status_tracker.py -v
```

### Deactivate the Virtual Environment

When you're done, deactivate the virtual environment:

```bash
deactivate
```

## Scaling to 100+ Status Pages

Add more monitors in `status_tracker.py` → `main()`:

```python
monitors = [
    StatusPageMonitor(
        name="OpenAI API",
        feed_url="https://status.openai.com/history.atom",
    ),
    StatusPageMonitor(
        name="GitHub",
        feed_url="https://www.githubstatus.com/history.atom",
    ),
    StatusPageMonitor(
        name="Stripe",
        feed_url="https://status.stripe.com/history.atom",
    ),
    # ... add as many as needed — all run concurrently on 1 thread
]
```

## Design Decisions

1. **Atom feed over scraping** — Structured XML is robust against UI changes; Atom is a web standard supported by most status page providers.

2. **ETag-based conditional requests** — The server returns `304 Not Modified` when nothing has changed, meaning zero data transfer and zero parsing work on quiet cycles.

3. **Change-detection reactor** — Incident IDs and timestamps are tracked in memory. Callbacks fire only when a genuinely new or updated incident appears — not on every poll.

4. **asyncio concurrency** — All monitors share a single event loop and thread. This means monitoring 100 pages uses the same resources as monitoring 1, since most time is spent `await`-ing I/O.

5. **Callback pattern** — The `on_incident_update` callback is replaceable. Swap it for a webhook sender, database writer, or Slack notifier without changing the monitor.

## Project Structure

```
├── status_tracker.py       # Main application
├── test_status_tracker.py  # Unit tests
├── requirements.txt        # Python dependencies
└── README.md               # This file
```
