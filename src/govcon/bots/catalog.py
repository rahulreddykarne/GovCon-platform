"""What each bot is allowed to do. The /bots page and the docs share this list."""

from __future__ import annotations

from typing import TypedDict


class BotSpec(TypedDict):
    title: str
    trigger: str
    inputs: str
    outputs: str
    permissions: str
    failure: str


CATALOG: dict[str, BotSpec] = {
    "orchestrator": {
        "title": "Orchestrator",
        "trigger": "Queued at the end of the morning and evening ingest chains, or from /bots.",
        "inputs": "A slot id and whether this run should pull sources again.",
        "outputs": "Child run ids and a per-opportunity state. A failed child stays incomplete.",
        "permissions": "Queue the other bots. It cannot send email, submit a bid, or change AI sharing.",
        "failure": "The cycle is marked incomplete. Bid/no-bid is not run for an opportunity whose document or compliance bot failed.",
    },
    "discovery": {
        "title": "Discovery",
        "trigger": "The orchestrator, or Run discovery on /bots.",
        "inputs": "SAM and DIBBS through the existing ingest functions. A scheduled cycle records the pull the chain just made instead of pulling twice.",
        "outputs": "Counts and the opportunity ids inserted or updated.",
        "permissions": "Read and write opportunities from the public feeds. No quotes are submitted.",
        "failure": "The error is stored on the run. Matching still uses whatever was already saved.",
    },
    "document": {
        "title": "Document",
        "trigger": "Orchestrator, once per opportunity and source revision.",
        "inputs": "Listing fields, description text, and attachment URLs.",
        "outputs": "Requirements with a citation. Missing text stays unknown.",
        "permissions": "Read attachments through the existing downloader. External AI only if the classification gateway allows it. This bot does not call a model.",
        "failure": "Listed attachments that were not read leave the opportunity incomplete. Nothing is marked reviewed.",
    },
    "matching": {
        "title": "Matching",
        "trigger": "After the document bot for that revision.",
        "inputs": "Enabled watchlists and the listing.",
        "outputs": "Which groups passed, the AND rule, eligibility, and the stored rank when one exists.",
        "permissions": "Read watchlists and matches. It does not pursue or dismiss.",
        "failure": "The run is failed and the bid bot does not treat the fit as known.",
    },
    "bid_decision": {
        "title": "Bid / no-bid",
        "trigger": "After matching, and only when document and compliance did not fail.",
        "inputs": "The decision engine: JEV when the gateway allows the package, otherwise the local rules.",
        "outputs": "An explainable recommendation and a pending approval.",
        "permissions": "Write a decision package. It cannot approve the bid or change AI sharing flags.",
        "failure": "No recommendation is treated as final. The approval is not created.",
    },
    "compliance": {
        "title": "Compliance",
        "trigger": "With matching, per opportunity revision.",
        "inputs": "Listing fields and requirements the document bot cited.",
        "outputs": "Eligibility, deadline, set-aside, and questions that are still unanswered.",
        "permissions": "Read the opportunity. It does not assert a certification the source did not state.",
        "failure": "Unanswered items stay unknown and the bid bot is skipped.",
    },
    "amendment": {
        "title": "Amendment",
        "trigger": "When the opportunity has source events.",
        "inputs": "opportunity_events for that id.",
        "outputs": "What changed and the operational impact. No guessed price or compliance effect.",
        "permissions": "Read events. It does not edit the solicitation.",
        "failure": "A read error is stored. Silence is not treated as 'no change' unless the event list was read and was empty.",
    },
    "awards": {
        "title": "Awards intelligence",
        "trigger": "After matching, per opportunity revision.",
        "inputs": "USAspending rows already stored for the NSN or PSC.",
        "outputs": "Historical comps labeled as history, not as this opportunity's value.",
        "permissions": "Read stored awards. The scheduled cycle does not start a new USAspending download.",
        "failure": "Missing history is unknown, not zero.",
    },
    "alert": {
        "title": "Alert",
        "trigger": "End of an orchestrator cycle.",
        "inputs": "Unalerted matches and the cycle's child runs.",
        "outputs": "An HTML file in the outbox and a pending approval to review it.",
        "permissions": "Write the outbox. It does not open SMTP.",
        "failure": "Matches stay unalerted if the file was not written.",
    },
    "operations": {
        "title": "Operations",
        "trigger": "End of a cycle, and the same checks /ops shows.",
        "inputs": "Heartbeats, ingestion runs, and local configuration flags.",
        "outputs": "A health snapshot. No live call to a source or model.",
        "permissions": "Read operational tables.",
        "failure": "The snapshot records the error. It does not mark ingest as healthy.",
    },
}
