# GovCon control center

Open **Control center** in the sidebar, or visit /operate after signing in.

## Where to look

| View | Answers |
| --- | --- |
| Control center | What is running, what needs attention, how much recorded API work cost, and which schedules are next? |
| Live activity | Which HTTP routes is this web process handling? Which worker step is running, queued, waiting, or failed? |
| Schedules & history | What is the configured clock, the persisted next run, the number of previous runs, the trigger, and the failure step? Filter history by chain; all records are paginated. |
| Document reading | Which file/version was downloaded? Which pages were native text, OCR, or unreadable? What was the OCR confidence and document hash? |
| Workflow map | How do discovery, documents, matching, compliance, models, and human approvals connect? Select a node to inspect its inputs, outputs, and failure behavior. |
| APIs & models | Is a provider configured, blocked, or supported by a stored result? How many attempts succeeded or failed? Which model and purpose were most recently recorded? |
| API costs | Provider-reported tokens, cached tokens, search charges, latency, per-model and per-opportunity cost, and editable model rates. |
| Agents / Bots | Agent responsibilities, recent handoffs, evidence, and the human approval queue. |
| Ops | Recovery actions for failed and waiting tasks, source-ingestion history, and process health. |

The sidebar, cards, tables, status badges, form controls, and opportunity tabs use the same responsive design. Opportunity headers keep the exact deadline in PT visible; every document links to its reading inspector.

## Reading the telemetry

- Dashboard cost is the known amount for the last 30 days, accompanied by the number of attempts without reported usage or a price. Unknown amounts are not zero. Cost-period Today on the existing detailed cost view uses UTC and is labeled accordingly.
- A configured credential is not a successful API call. Provider status, recorded attempt outcomes, and output quality are separate evidence.
- Running worker steps do not assert that an outbound HTTP request is in flight. Provider attempts are recorded after completion.
- HTTP activity is real, bounded telemetry for this web process. It resets on restart; a deployment with multiple web processes has separate counters. Static assets are excluded. Duration is measured until response headers. Only route templates and methods are stored, never identifiers, query strings, bodies, or users.
- Next-run times are read from saved scheduler jobs. If the scheduler is down, a persisted timestamp can be stale. The health panel shows heartbeat freshness separately.
- Run counts include both manual and scheduled executions. History states the trigger for each row.
- Native means the original text layer. OCR means image-to-text extraction. None means no readable text. OCR confidence does not establish compliance.
- Document contents and private local file paths are not copied into the operations dashboard. Open the opportunity for source evidence and analysis.

## Refresh

Use Refresh now, or opt into a 15-second refresh. Refresh is paused when an operational form has unsaved changes, and when the browser tab is hidden. Open trace disclosures are preserved. Failed refreshes retain the previous snapshot and show a warning.

## Verification in this environment

The Python launcher and PostgreSQL service from the original laptop are not available in this workspace. An isolated portable runtime under the ignored .venv directory was used with existing dependencies. Isolated SQL projection tests and template tests run without live APIs. The browser automation surface is unavailable, so a visual browser pass and PostgreSQL-backed lifecycle tests remain deployment checks.

preview.html is a self-contained synthetic design preview, not live telemetry.
