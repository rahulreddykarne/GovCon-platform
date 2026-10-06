# Operating the GovCon laptop

This is the local Windows setup. It does not use Docker. It does not start the Postgres instance on port 5432. It does not deploy anywhere.

## What you run

Three processes, plus Postgres 15 (or 16) already listening on `127.0.0.1:5433`:

| Process | Command | What it does |
|---|---|---|
| Web | `govcon web serve --host 127.0.0.1 --port 8001` | The UI. Default bind is localhost. |
| Worker | `govcon worker start` | Claims tasks: chains, bots, preparation. |
| Scheduler | `govcon scheduler start --no-worker` | Queues the six chains. The worker runs them. |

`scripts/windows/Start-GovCon.ps1` starts those three. `Stop-GovCon.ps1` stops them and leaves Postgres running. `Get-GovConStatus.ps1` prints ports and `govcon status`.

```powershell
# from the repo root, after the venv exists and DATABASE_URL points at port 5433
.\scripts\windows\Start-GovCon.ps1 -PostgresPort 5433 -WebPort 8001
.\scripts\windows\Get-GovConStatus.ps1
.\scripts\windows\Stop-GovCon.ps1
```

The scripts do not print `DATABASE_URL` or API keys. If Postgres is not accepting connections on the port you pass, they stop.

## Clocks

The scheduler uses `America/Los_Angeles`, so the same local hour holds in Pacific Daylight Time and Pacific Standard Time.

| Local time | Job |
|---|---|
| 11:30 PM | SAM, DIBBS, match, alerts, then queue the orchestrator |
| 12:30 AM | USAspending |
| 1:00 AM | Embeddings and semantic match |
| 5:00 AM | Deadline and archive check |
| 11:00 AM | Second SAM and DIBBS cycle, then the orchestrator |
| Sunday 2:00 AM | Archive, cache, analytics, VACUUM |

A missed run still fires once if you open the laptop within 18 hours (36 hours for Sunday). Two schedulers cannot hold the advisory lock at the same time. Digest alerts are written to the outbox and are not emailed.

## Where to look

- `/` — new matches, pending bot decisions, and whether health checks need you.
- `/bots` — each bot's last run, evidence, and the approvals queue.
- `/ops` — heartbeats, last success, last failure, next run, and the next command to run.
- `/health` — JSON. Database down returns 503 and no error text.
- `outbox/` — HTML digests. Approving one on `/bots` does not send it.

## Database

```powershell
govcon db upgrade
govcon db seed-demo-watchlist
govcon db seed-demo-opportunities
```

The demo notices use source `demo` and titles that say they are not real solicitations. The demo watchlist is empty until you add PSC or NAICS codes. Groups you fill in are combined with AND. Values inside one group match with OR.

## AI sharing

`AI_EXTERNAL_ALLOWED_FOR_PROPRIETARY`, `AI_EXTERNAL_ALLOWED_FOR_FCI`, and `AI_EXTERNAL_ALLOWED_FOR_CUI` default off. Settings shows the current values and does not change them. Bid/no-bid uses JEV only when the gateway allows the package; otherwise the local rules engine writes the recommendation. The bot still asks you to approve it.

## When something is wrong

1. Open `/ops`. Read the "What to do" column.
2. If the worker or scheduler heartbeat is down, start that process. A heartbeat older than 90 seconds is down.
3. If a chain failed, the error is on that row. After you fix the cause, `govcon jobs run <chain>`.
4. A failed bot leaves the opportunity state `incomplete`. It does not mean the review is finished.
5. Source rows on `/ops` come from the last local ingest. The page does not call SAM to check.

## Logs

`logs-live/web.out.log`, `worker.out.log`, and `scheduler.out.log` when you use the PowerShell scripts. Do not paste those files into chat if they might contain a URL with an API key. SAM description downloads append the key as a query parameter.

## Remaining risks

- If `NOTIFY_EMAIL_ENABLED` is turned on, the notification-email task can still send SMTP. Bot approvals and the scheduled digest do not.
- A crash after an alert email is handed to SMTP and before the send is recorded can deliver that digest twice. The outbox path used by the app does not send.
- DIBBS quote PDFs and the full SAM description body are not fetched on the scheduled ingest. The workflow still uses attachments that were already stored.
- Tesseract may be missing on the laptop. Pages that need OCR stay unread and the document bot does not treat that opportunity as finished.
- Live SAM quota and whether the Hugging Face embedding model is already cached are unknown until the first real run. `/ops` does not download the model or call SAM.
- `/` and `/ops` mark the worker and the scheduler down when those processes have not written a heartbeat in the last 90 seconds. That is the web process looking at local rows, not a live probe of another machine.
