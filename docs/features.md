# Features

A detailed look at ChatPSA's core features — how they work, how to use them, and what's configurable.

## Natural-Language Chat

ChatPSA translates plain English questions into SQL, runs them against a local read-only mirror of your ConnectWise data, and returns formatted results. Tables, lists, and summaries are rendered directly in the chat.

The AI agent has full visibility into your database schema and knows which integrations are active. It can query tickets, time entries, agreements, companies, contacts, configurations, M365 licences, Duo users, Huntress agents, and ThreatLocker endpoints — anything that's been synced.

Example questions:

- *"Which clients logged the most tickets this month?"*
- *"Who logged the most hours this week?"*
- *"Show me all overdue tickets on the Help Desk board"*
- *"Which M365 licences are unassigned?"*
- *"List all Duo users in bypass mode at Acme Corp"*

All queries run against a local SQLite copy of your data — never against your production ConnectWise database. Only `SELECT` queries are permitted.

### Suggested Chips

The chat welcome screen shows two rows of clickable suggestion buttons to help you get started:

**Live insight chips** appear when anomalies are detected. Each anomaly generates two natural-language questions specific to the issue (e.g., *"Why is Acme Corp submitting double the usual number of tickets?"*). These are fetched from the anomaly detection engine on page load and cycle through in groups of four every 25–60 seconds.

**Standard suggestion chips** are drawn from a pool of general-purpose questions. Four are shown at a time and cycle every 20–60 seconds. The pool adapts based on which integrations are active — if Duo is connected, Duo-related suggestions appear.

Clicking any chip sends it as a chat message, exactly as if you'd typed it.

---

## Agent Memories

The AI agent can remember context across conversations — your preferences, naming conventions, common filters, or any factual note you want it to keep in mind.

### How to use

Tell the agent to remember something in plain language:

- *"Remember that we call the helpdesk board 'Service Desk'"*
- *"Note that Dan prefers ticket counts grouped by week"*
- *"Keep in mind that Acme Corp is our largest client"*

The agent proposes a memory and shows an approval card in the chat. The memory is only saved after you click **Approve**. You can also dismiss the proposal if you don't want it stored.

### The context bar

Stored memories appear in a horizontal bar labeled **Context** below the navigation header. Each memory is shown as a chip you can hover to see the full text. Five small slot indicators show how many of your 5 available slots are used.

To delete a memory, click the **×** on its chip. An inline confirmation appears — click **Yes** to remove it, or **No** to cancel. You can also ask the agent to forget something: *"Forget the memory about billing contacts."*

### Limits

- **5 memories per user.** If all slots are full, the agent will show your existing memories and ask which one to remove before adding a new one.
- **Key:** A short slug up to 80 characters (e.g., `billing_contact`, `ticket_format`).
- **Value:** A plain factual note up to 1,000 characters.
- Values are scanned for prompt injection patterns (markdown headers, XML tags, instruction-like language) and rejected if any are detected.

### How it works under the hood

Memories are injected into the AI system prompt on every conversation as background context. They're scoped per user — each person has their own private set. The agent is instructed to treat them as data, not instructions, to prevent prompt injection via stored values.

The agent is also blocked server-side from saving memories unless your message contains an explicit trigger word like "remember", "save", "memory", or "context". This prevents the agent from spontaneously storing junk memories as a side effect of answering questions.

---

## Trends and Anomaly Detection

The **Trends** page automatically surfaces operational issues across all connected services. Anomalies are displayed in a kanban-style board, grouped by service.

![Trends view](images/trends.png)

### Anomaly types

**Ticket anomalies** (always active):

| Anomaly | Trigger | Severity |
|---------|---------|----------|
| Ticket Spike | 2x+ the client's baseline ticket volume over the last 14 days vs. prior 90 days | High if ≥ 2.5x, otherwise medium |
| Overdue Tickets | ≥ 2 open helpdesk tickets past their required date | High |
| Stale High-Priority | ≥ 2 high-priority tickets open longer than 24 hours | High |
| Quiet Client | Active agreement but zero helpdesk tickets in 60 days | Low |

**Microsoft 365** (requires CIPP):

| Anomaly | Trigger | Severity |
|---------|---------|----------|
| Unused Licences | > 3 paid M365 licences (E3, E5, Business, F3) with no assigned user | High if ≥ 10, otherwise medium |

**Duo Security** (requires Duo):

| Anomaly | Trigger | Severity |
|---------|---------|----------|
| MFA Bypass | ≥ 1 user with Duo status "bypass" | High |
| Unenrolled/Stale Users | ≥ 2 active users either not enrolled or not logged in for 90+ days | High if ≥ 3 unenrolled, otherwise medium |

**Huntress EDR** (requires Huntress):

| Anomaly | Trigger | Severity |
|---------|---------|----------|
| Open Incidents | ≥ 1 open (not closed/resolved) incident | High if any critical/high severity, otherwise medium |

**ThreatLocker** (requires ThreatLocker):

| Anomaly | Trigger | Severity |
|---------|---------|----------|
| Unsecured Endpoints | ≥ 2 endpoints not in Secure mode | High if ≥ 50% unsecured, otherwise medium |
| Deny Spike | ≥ 20 application denials today, compared to 7-day average | High if ≥ 3x ratio or ≥ 100 denials, otherwise medium |

### Filtering

Use the **service pills** at the top to filter to a single integration. A text search box filters by client name. The count indicator shows how many anomalies are visible after filtering.

### Actionable chips

Each anomaly card includes clickable question chips that link directly to the chat. Clicking one opens the chat with the question pre-filled, so you can drill into the issue immediately.

### Caching

Results are cached for 30 minutes. Click the **Refresh** button to force a fresh analysis. The page shows when the data was last calculated.

### Configuration

| Setting | Where | Effect |
|---------|-------|--------|
| `HELPDESK_BOARD` | `.env` or Admin → Settings | Which board's tickets to analyse |
| `TRENDS_EXCLUDE_COMPANIES` | `.env` or Admin → Settings | Companies to exclude from all anomaly queries (e.g., your own MSP) |
| `TRENDS_HIGH_PRIORITIES` | `.env` or Admin → Settings | Priority names that trigger the stale-priority check (auto-detects if unset) |

---

## Pinned Queries

The agent can save notable findings or live queries to the Trends page, where they persist as team-visible cards. This turns a one-off chat insight into something everyone can see.

### Two pin types

**Finding** — a static snapshot of a specific result. For example: *"34% of Acme Corp's recent tickets are Priority 1."* Point-in-time, does not update.

**Query** — a live SQL search that re-runs every time someone visits the Trends page. Acts like a custom anomaly detector. For example: *"Clients with a high ratio of Priority 1 tickets."* Results display in a table, capped at 20 rows.

### How to use

Ask the agent to pin something during a conversation:

- *"Pin this to the trends page"*
- *"Save this as a pinned query"*

The agent presents both options (finding vs. query) and waits for you to choose before saving. Query pins include a **Show SQL** button so you can see exactly what's running.

### Dismissing

Each pinned card has a **Dismiss** button. Dismissed pins are soft-deleted — they disappear from the UI but remain in the database for audit purposes.

### Limits

- **Maximum 10 active query pins.** Finding pins have no limit. If you hit the cap, dismiss an existing query pin first.
- Pin SQL is validated for safety — only `SELECT` statements are allowed. Dangerous keywords (`INSERT`, `UPDATE`, `DELETE`, `DROP`, etc.) are blocked.

---

## Timeline

The **Timeline** extracts future dates mentioned in ticket notes and displays them as a forward-looking calendar of upcoming events — scheduled maintenance, follow-ups, renewals, and anything else your team has noted with a date.

![Timeline view](images/timeline.png)

### How events are extracted

The timeline parser scans ticket notes for date references using 16 patterns in two confidence tiers:

**Confirmed** (explicit dates): `2026-06-15`, `15th June 2026`, `15/06/2026`, `Fri 15/06`, and similar formats.

**Likely** (relative dates): `next Monday`, `tomorrow`, `in 2 weeks`, `in a fortnight`, `this Friday`, and similar phrases. Relative dates are resolved against the note's creation date, not today.

Only future dates within 12 months are kept. Past events are automatically pruned after each parse run. Dates found in email signatures (after `--`, `Regards`, `From:`, etc.) are skipped.

### AI summaries

Each event can have an AI-generated summary — a single concise sentence describing what's scheduled. These are generated automatically during the parse cycle (using Claude Haiku to keep costs low) and can also be triggered on demand per event. Toggle the star icon on any event to switch between the AI summary and the raw context snippet.

### Filtering

The timeline page offers several filters:

- **Text search** — searches across context, ticket summary, company name, and AI descriptions
- **Company filter** — filter to a specific client
- **Date range** — From/To date pickers (defaults to today → 3 months out)
- **Board filter** — dropdown of boards with timeline events
- **Confidence** — show only confirmed dates, likely dates, or both

### Dismissing events

Click **Dismiss** on any event card to remove it. Dismissals are applied optimistically (the card disappears immediately) and synced to the server in the background.

### Configuration

| Setting | Where | Effect |
|---------|-------|--------|
| `timeline_ai_summaries` | Admin → Settings → Timeline | Toggle automatic AI summary generation on/off |
| `timeline_boards` | Admin → Settings → Timeline | Comma-separated list of boards to include (blank = all boards) |

### Sync schedule

The timeline parser runs every **2 hours**. It processes incrementally — only new notes since the last run. Use the `--reparse` flag to reprocess all notes from scratch.

---

## Analytics

The **Analytics** page provides a usage dashboard showing how the team interacts with ChatPSA — query volume, response times, peak hours, and who's using it.

### Summary cards

Six stat cards across the top give an at-a-glance overview:

- **Total Queries** — lifetime query count
- **Last 7 Days** — queries in the past week, with a percentage change indicator compared to the prior week
- **Unique Users** — distinct users who have queried the system
- **Avg Response** — mean AI response time in seconds
- **Error Rate** — percentage of queries that returned an error
- **Suggestions Used** — percentage of queries that came from clicking a suggested chip rather than typing

### Charts and visualisations

**Daily Activity** — a 30-day bar chart showing queries per day. Hover any bar to see the exact count.

**Response Time Trend** — a 30-day line chart of average response time, helping you spot slowdowns or improvements after model changes.

**Peak Usage Hours** — a 24-hour heatmap grid showing which hours of the day see the most activity. Useful for understanding when your team relies on the tool.

**Top Users** — a horizontal bar chart of the top 10 users by query count.

**Query Sources** — a donut chart breaking down typed queries vs. suggested-chip queries. Helps gauge whether the suggestion system is adding value.

**Query Themes** — AI-generated topic clusters summarising what people are asking about (e.g., "Ticket volume by client", "Time entry reporting"). Themes are generated by Claude and cached for 6 hours.

**Error Rate Trend** — a 30-day chart of error percentages, colour-coded to highlight spikes.

### Page traffic

A separate section tracks page views across the application — total views, a breakdown by page (Chat, Trends, Timeline, Analytics, Admin), the most active users by page visits, and a daily page-views chart.

### User breakdown

A table listing each user with their query count, average response time, and error count. Sortable by any column.

### Recent activity

A table of the most recent queries showing timestamp, user, query text, source (typed or suggested), and response time.

### Access

Analytics is gated behind the **admin** feature permission. Only users with admin access can view this page.
