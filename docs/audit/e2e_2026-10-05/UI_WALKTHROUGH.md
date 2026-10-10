# UI walkthrough — 2026-10-06

Shots are from this branch after the merge of `fix/audit-and-workflow-2026-10-05` (`118a94a`) and `audit10032026` / PR 26. The web process was `govcon web serve --host 127.0.0.1 --port 8001` against a local Postgres database that had been migrated to Alembic head `d6e7f8a9b0c1`. Demo rows came from `govcon db seed-demo-opportunities` (source `demo`, PSC `ZZ99`, titles say they are not real solicitations). No page load called SAM, DIBBS, USAspending, or a model provider. The worker and the scheduler were not running, so `/` and `/ops` mark those heartbeats down.

The workflow line before this branch had the same inbox, search, pipeline, and settings shell. It did not have a Bots nav item, the inbox approvals and health cards, the External AI sharing card, Pacific clock text on `/ops`, or a stage subtitle on pipeline cards. Those are the surfaces below.

## Inbox

`/` shows the two demo matches that are not already in a pursuit, the pending bid recommendation, the orchestrator state `needs_decision`, and the missing worker and scheduler heartbeats.

![Inbox with demo matches, a pending approval, and health attention](screenshots/after_inbox.png)

## Search

`/search?q=valve` returns the sanitized valve-kit notice. One row, source `demo`, with the existing recipient-search link left in place. Pages are 50 rows; this query has a single row so no next page is offered.

![Search result for the sanitized valve-kit notice](screenshots/after_search.png)

## Opportunity

`/opp/2` is the workspace for `DEMO-0002`. The title says it is a sanitized demo.

![Opportunity workspace for the sanitized bearing-set notice](screenshots/after_opportunity.png)

## Pipeline

`/pipeline` keeps the workflow columns: matched, preparing, in review, drafting, ready, closed. `DEMO-0001` is in preparing with the stage subtitle `evaluating`. The other two demo notices sit in matched.

![Pipeline with preparing and matched demo notices](screenshots/after_pipeline.png)

## Settings

`/settings` shows the external AI sharing flags read from the environment. On this process they are off. The form does not change them.

![Settings with proprietary, FCI, and CUI sharing off](screenshots/after_settings.png)

## Ops

`/ops` lists component checks and the six chains. Next-run text uses `America/Los_Angeles`. Source rows are local history (no ingest has run here, so they are stale). The worker and scheduler rows say to start those processes.

![Ops health checks and Pacific chain board](screenshots/after_ops.png)

## Bots

`/bots` lists the ten bots, the last orchestrator run (`waiting_approval` / `needs_decision`), and the pending bid recommendation. The run button was not used. Recording an approval was not clicked; that action only stores the human decision.

![Bots catalog and the pending demo approval](screenshots/after_bots.png)

## Operate, learning, and the notice trace

These shots are from the same local web process after `govcon db upgrade` to Alembic head `e7f8a9b0c1d2`. The database is the walkthrough database with the three sanitized demo notices. No page load called SAM, DIBBS, USAspending, DeepSeek, JEV, or Anthropic. The worker and the scheduler were not running, so those heartbeats stay Failed. Run, Record approval, Dismiss, Pursue, Save, and Rollback were not clicked.

### Overview

`/operate` shows stored counts: 1 decision waiting, 0 agents running, 3 new matches, and 2 of 12 checks healthy. Company strategy fields are listed as missing. The recent orchestrator run is `waiting_approval`.

![Operate overview with stored counts and missing strategy fields](screenshots/operate_overview.png)

### Agents

`/operate/agents` lists the ten bots. Only the orchestrator has a stored run. The others say no run is stored.

![Agent cards with the orchestrator selected](screenshots/operate_agents.png)

### Architecture

`/operate/architecture` draws the real stage order. Selecting Document opens the inspector (trigger, inputs, outputs, permissions, failure).

![Architecture with the Document node selected](screenshots/operate_architecture.png)

### Integrations and the model route

`/operate/integrations` shows the route in use: analysis provider `deepseek`, model `deepseek-flash`, decision fallback `rules`, version label `settings` because no route row is saved. Health cards are local. On this process SAM and DeepSeek are not configured, the database and local embeddings are healthy, and the worker and scheduler have no heartbeat.

![Model route form before any version is saved](screenshots/operate_integrations.png)

![Integration health cards from local checks](screenshots/operate_integrations_health.png)

### How GovCon works

`/operate/how` follows `Sanitized demo — valve kit` (`DEMO-0001`). The notice is stored. Source files are not stored. Later bot stages are not run. The explanations are system text; the status comes from that notice.

![Guided steps for the stored valve-kit notice](screenshots/operate_how.png)

### Learning

`/learning` shows outcome capture at 0 suggested, 0 confirmed, and 0 dismissed, because no suggestions are stored. The sanitized eval set has sample size 4 and 4 passed: sparse output, missing citations, contradictory delivery windows, and a missed blocker. Win rate is not computed because there is no recorded won or lost outcome.

![Sanitized eval set with sample size 4](screenshots/learning_evals.png)

![Win-rate formula with a sample size of zero](screenshots/learning_calculations.png)

### Notice trace

`/opp/1` includes "What ran on this notice". The notice is stored. Source files, document, matching, compliance, awards, amendment, bid/no-bid, and solicitation analysis are not run or not stored. The human decision is waiting.

![Stage trace on the valve-kit workspace](screenshots/opportunity_trace.png)

### Company strategy

`/settings` shows the strategy fields still missing, the Pacific clocks, and external AI sharing off. Nothing was saved.

![Settings company strategy fields left blank](screenshots/settings_strategy.png)
