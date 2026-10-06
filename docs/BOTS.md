# GovCon bots

Ten bots run on the existing task queue. The orchestrator is a `bot_run` task. The morning and evening scheduler chains queue it after alerts, with `pull` off, so they do not call SAM twice. A person can also run it from `/bots`.

Nothing in this path sends email, submits a bid, contacts a contracting officer, or changes `AI_EXTERNAL_ALLOWED_*`. Approving a row records the decision only.

| Bot | Trigger | Reads | Writes | If it fails |
|---|---|---|---|---|
| Orchestrator | Scheduler slot or `/bots` | Child results | `bot_runs`, one `bot_run` task | The cycle state is `incomplete`. A failed document or compliance bot skips bid/no-bid for that opportunity. |
| Discovery | Orchestrator | SAM and DIBBS when `pull` is on; otherwise the latest local ingest rows | Opportunity ids touched since that pull | Error stored. No notices are invented. |
| Document | Per opportunity and source revision | Listing fields, description, attachment URLs | Cited requirements (`created_by=document_bot`, status `unreviewed`) | Unread attachments leave `state=incomplete`. |
| Matching | After document | Enabled watchlists | Which groups passed, the AND rule, eligibility, stored rank | Fit is unknown. Bid/no-bid is skipped. |
| Bid / no-bid | After a match, when document and compliance did not fail | Decision engine | Recommendation plus a `bid_recommendation` approval | No approval. The opportunity is incomplete. |
| Compliance | With matching | Listing fields and description | Unanswered questions and an approval when questions remain | A crash skips bid/no-bid. Open questions stay questions. |
| Amendment | Same revision | `opportunity_events` | What changed and a fixed impact sentence | An empty event list is "nothing stored", not "nothing changed at the source". |
| Awards intelligence | Same revision | Stored USAspending rows | Historical comps and a disclaimer | No rows means unknown, not zero. |
| Alert | End of the cycle | Unalerted matches | HTML in the outbox and a `daily_summary` approval | Matches stay unalerted if the write fails. SMTP is not opened. |
| Operations | End of the cycle | Heartbeats and local run history | Health snapshot | The error is stored. Health is not reported as ok. |

Idempotency keys:

- `orchestrator:{slot}:pull={0|1}`
- `discovery:{slot}:pull` or `discovery:{slot}:observe`
- `{bot}:{opportunity_id}:{source_revision}` for document, matching, compliance, amendment, awards, and bid/no-bid
- `alert:{slot}` and `operations:{slot}`

A finished key is not run again. A failed key can be retried; `attempt` increments. A run that is still `running` after 15 minutes is treated as stale and can be taken over.

`/bots` shows, for each bot, the last status, timestamps, attempt, trigger, source revision, why it concluded what it did, and the evidence lines. The approvals queue is on the same page.

Trace one opportunity: `opportunities.id` → `opportunity_events` → `bot_runs.opportunity_id` → `bot_approvals` → `decision_runs` / `bid_decisions`. Scheduler cycles are `scheduler_job_runs` plus the `bot_run` task.
