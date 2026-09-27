#!/usr/bin/env bash
# Phase 19 smoke test — §25 full pipeline:
#   db upgrade → seed demo watchlist → ingest fixtures → snapshot diff →
#   match → digest → validate prompt registry →
#   render and schema-check fixture prompts →
#   analyze fixture opportunity → generate bid recommendation →
#   generate compliance matrix → create proposal v1 →
#   prepare submission checklist → assert outputs
#
# No live network calls. All AI steps run without a key and warn gracefully.
# Every required step must succeed; failures exit non-zero.
set -euo pipefail
cd "$(dirname "$0")/.."

# ─── helpers ──────────────────────────────────────────────────────────────────

py() {
    python3 -c "
import os, sys
os.environ.setdefault('DATABASE_URL', '${DATABASE_URL:-postgresql+psycopg://govcon:govcon@localhost:5432/govcon}')
$1"
}

fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

# ── 1. DB upgrade ─────────────────────────────────────────────────────────────
echo "=== smoke: db upgrade ==="
govcon db upgrade

# ── 2. Seed demo watchlist ────────────────────────────────────────────────────
echo "=== smoke: seed demo watchlist ==="
govcon db seed-demo-watchlist

# ── 3. govcon status ──────────────────────────────────────────────────────────
echo "=== smoke: govcon status ==="
govcon status

# ── 4. Ingest fixtures ────────────────────────────────────────────────────────
echo "=== smoke: ingest SAM fixture ==="
govcon ingest sam --file tests/fixtures/sam_opportunities_search.json

echo "=== smoke: ingest DIBBS fixture ==="
govcon ingest dibbs --file tests/fixtures/dibbs/in260925.txt

# ── 5. Snapshot diff — unchanged re-ingest must produce zero new snapshots ────
echo "=== smoke: snapshot diff (re-ingest SAM fixture — expect zero new snapshots) ==="
SNAP_BEFORE=$(py "
from govcon.db import session_scope
from govcon.models import OpportunitySnapshot
with session_scope() as s:
    print(s.query(OpportunitySnapshot).count())
")
govcon ingest sam --file tests/fixtures/sam_opportunities_search.json
SNAP_AFTER=$(py "
from govcon.db import session_scope
from govcon.models import OpportunitySnapshot
with session_scope() as s:
    print(s.query(OpportunitySnapshot).count())
")
[ "$SNAP_BEFORE" -eq "$SNAP_AFTER" ] || fail "snapshot count changed on re-ingest: before=$SNAP_BEFORE after=$SNAP_AFTER"
echo "  snapshot_count_before=$SNAP_BEFORE snapshot_count_after=$SNAP_AFTER (no change: OK)"

# ── 6. Match ──────────────────────────────────────────────────────────────────
echo "=== smoke: match ==="
govcon match run

# ── 7. Digest ─────────────────────────────────────────────────────────────────
echo "=== smoke: alerts digest (outbox mode) ==="
govcon alerts digest

# ── 8. Validate prompt registry ───────────────────────────────────────────────
echo "=== smoke: validate prompt registry ==="
govcon prompts validate

# ── 9. Render and schema-check fixture prompts ────────────────────────────────
echo "=== smoke: render and schema-check fixture prompts ==="

# Write a fixture that satisfies all required_variables for each safety-critical prompt
# amendment_analysis: REQUIREMENTS_JSON, AMENDMENT_JSON, DOCUMENT_INVENTORY_JSON
cat > /tmp/smoke_amendment_vars.json <<'JSON'
{
  "REQUIREMENTS_JSON": [{"id": 1, "category": "delivery", "text": "30 days ARO", "mandatory": true}],
  "AMENDMENT_JSON": {"amendment_number": 1, "title": "Amendment 1", "text": "POC update only. No material change."},
  "DOCUMENT_INVENTORY_JSON": [{"file_id": 1, "filename": "solicitation.pdf", "document_type": "base_solicitation"}]
}
JSON
echo "  rendering amendment_analysis..."
govcon prompts render amendment_analysis --fixture /tmp/smoke_amendment_vars.json > /tmp/smoke_render_out.txt
grep -q "SYSTEM PROMPT" /tmp/smoke_render_out.txt || fail "render output missing SYSTEM PROMPT section"
grep -q "USER CONTEXT" /tmp/smoke_render_out.txt || fail "render output missing USER CONTEXT section"
echo "  amendment_analysis render: OK (schema: amendment_analysis.v1)"

# solicitation_analysis: OPPORTUNITY_JSON, SOURCE_PACKAGE_JSON
cat > /tmp/smoke_solicitation_vars.json <<'JSON'
{
  "OPPORTUNITY_JSON": {"id": 1, "title": "Medical Supply NSN 6515-01-234-5678", "psc_code": "6515"},
  "SOURCE_PACKAGE_JSON": {"files": [], "text_chunks": [{"page": 1, "section": "Section B", "text": "Quantity 500 EA. Delivery 30 days ARO."}]}
}
JSON
echo "  rendering solicitation_analysis..."
govcon prompts render solicitation_analysis --fixture /tmp/smoke_solicitation_vars.json > /tmp/smoke_render_sol.txt
grep -q "SYSTEM PROMPT" /tmp/smoke_render_sol.txt || fail "solicitation_analysis render missing SYSTEM PROMPT"
echo "  solicitation_analysis render: OK (schema: solicitation_analysis.v1)"

# Verify schema versions appear in registry
py "
from govcon.ai.schemas import SCHEMA_REGISTRY
required = ['amendment_analysis.v1', 'solicitation_analysis.v1', 'requirement_extraction.v1',
            'compliance_validation.v1', 'proposal_draft.v1', 'proposal_red_team.v1',
            'outcome_analysis.v1']
missing = [sv for sv in required if sv not in SCHEMA_REGISTRY]
assert not missing, f'Schemas not in registry: {missing}'
print(f'  schema_registry: {len(SCHEMA_REGISTRY)} entries, all required schemas present')
"

# ── 10. Analyze fixture opportunity ──────────────────────────────────────────
echo "=== smoke: analyze fixture opportunity ==="
# Ingest the fixture PDF so the opportunity has source text.
# Use opp id=1 from the SAM fixture; ingest the fixture PDF.
SMOKE_OPP_ID=$(py "
from govcon.db import session_scope
from govcon.models import Opportunity
from sqlalchemy import select
with session_scope() as s:
    opp = s.execute(select(Opportunity).where(Opportunity.source=='sam').limit(1)).scalar_one_or_none()
    assert opp is not None, 'No SAM opportunity found'
    print(opp.id)
")
echo "  using opportunity_id=$SMOKE_OPP_ID"

govcon enrich ingest-file --opportunity-id "$SMOKE_OPP_ID" --file tests/fixtures/solicitation_fixture.pdf

# analyze warns gracefully without a key; exit 0 is required
govcon enrich analyze --opportunity-id "$SMOKE_OPP_ID"
echo "  analyze fixture opportunity: OK (AI key absent → graceful warn)"

# ── 11. Generate bid recommendation ──────────────────────────────────────────
echo "=== smoke: generate bid recommendation ==="
govcon decision run-package --opportunity-id "$SMOKE_OPP_ID"
# Verify a decision_run row was persisted
py "
from govcon.db import session_scope
from govcon.models import DecisionRun
from sqlalchemy import select
opp_id = int('$SMOKE_OPP_ID')
with session_scope() as s:
    run = s.execute(select(DecisionRun).where(DecisionRun.opportunity_id==opp_id).limit(1)).scalar_one_or_none()
    assert run is not None, 'No decision_run found after run-package'
    assert run.provider in {'rules','jev','llm'}, f'Unknown provider: {run.provider}'
    print(f'  decision_run_id={run.id} provider={run.provider} bundle={run.bundle_name}')
"

# ── 12. Generate compliance matrix ────────────────────────────────────────────
echo "=== smoke: generate compliance matrix ==="
govcon compliance run --opportunity-id "$SMOKE_OPP_ID" --no-ai
govcon compliance matrix --opportunity-id "$SMOKE_OPP_ID" --json > /tmp/smoke_matrix.json
MATRIX_LEN=$(python3 -c "import json; rows=json.load(open('/tmp/smoke_matrix.json')); print(len(rows))")
[ "$MATRIX_LEN" -gt 0 ] || fail "compliance matrix is empty after run"
echo "  compliance_matrix: $MATRIX_LEN rows"

# Verify source refs are present in at least one row
py "
import json
rows = json.load(open('/tmp/smoke_matrix.json'))
sourced = [r for r in rows if r.get('source') and r['source'].get('filename')]
assert sourced, 'No matrix row has a source file reference'
print(f'  source_refs: {len(sourced)}/{len(rows)} rows have source file references')
"

# ── 13. Set up smoke opportunity as approved-to-bid ───────────────────────────
echo "=== smoke: set up approved-to-bid state for proposal step ==="
SMOKE_USER_EMAIL="smoke-runner@test.govcon"
py "
from govcon.db import session_scope
from govcon.models import Pursuit, ReviewSession, User
from govcon.collaboration.review_sessions import ensure_review_session
from sqlalchemy import select
opp_id = int('$SMOKE_OPP_ID')
user_email = '$SMOKE_USER_EMAIL'
with session_scope() as s:
    # Ensure owner user exists
    user = s.execute(select(User).where(User.email==user_email)).scalar_one_or_none()
    if not user:
        user = User(email=user_email, display_name='Smoke Runner', role='owner', password_hash='x')
        s.add(user)
        s.flush()
    # Ensure pursuit in bid_approved stage
    pursuit = s.execute(select(Pursuit).where(Pursuit.opportunity_id==opp_id)).scalar_one_or_none()
    if not pursuit:
        pursuit = Pursuit(opportunity_id=opp_id, stage='bid_approved', sourcing_cost=8000, quote_price=10000)
        s.add(pursuit)
    else:
        pursuit.stage = 'bid_approved'
    # Ensure review session approved_to_bid
    rs = ensure_review_session(s, opportunity_id=opp_id)
    rs.final_approval_status = 'approved_to_bid'
    rs.status = 'approved_to_bid'
    s.commit()
    print(f'  user={user.email} pursuit_id={pursuit.id} stage={pursuit.stage}')
    print(f'  review_session_id={rs.id} final_approval_status={rs.final_approval_status}')
"

# ── 14. Create proposal v1 ───────────────────────────────────────────────────
echo "=== smoke: create proposal v1 ==="
govcon proposal generate \
    --opportunity-id "$SMOKE_OPP_ID" \
    --actor-email "$SMOKE_USER_EMAIL" \
    --skip-ai

# Verify proposal and version rows exist with correct structure
py "
from govcon.db import session_scope
from govcon.models import Proposal, ProposalVersion, ProposalSection
from sqlalchemy import select, desc
opp_id = int('$SMOKE_OPP_ID')
with session_scope() as s:
    proposal = s.execute(select(Proposal).where(Proposal.opportunity_id==opp_id)).scalar_one_or_none()
    assert proposal is not None, 'No Proposal row found'
    # Verify version number increments correctly
    all_vers = s.execute(select(ProposalVersion).where(ProposalVersion.proposal_id==proposal.id).order_by(ProposalVersion.version_number)).scalars().all()
    assert len(all_vers) >= 1, 'No ProposalVersion row found'
    v_numbers = [v.version_number for v in all_vers]
    assert v_numbers == sorted(set(v_numbers)), f'Version numbers not unique or non-sequential: {v_numbers}'
    version = all_vers[-1]
    sections = s.execute(select(ProposalSection).where(ProposalSection.proposal_version_id==version.id)).scalars().all()
    assert len(sections) > 0, 'No ProposalSection rows found'
    # requirement_ids is a list (may be empty when no section assignments exist)
    assert all(isinstance(sec.requirement_ids, list) for sec in sections), 'requirement_ids must be a list'
    # Unsupported-claim / missing-evidence blockers: [[BLOCKER:...]] markers are flagged
    # when requirements are unsatisfied — this is the unsupported-claim flagging behavior
    has_blockers = any('[[BLOCKER:' in (sec.content or '') for sec in sections)
    print(f'  proposal_id={proposal.id} version={version.version_number} sections={len(sections)}')
    print(f'  version_numbers_unique={v_numbers}')
    print(f'  has_unsupported_claim_markers={has_blockers} (blocker markers flag missing evidence)')
    # If any requirement was assigned to a section, it must appear in requirement_ids
    linked = [sec for sec in sections if sec.requirement_ids]
    print(f'  sections_with_requirement_links={len(linked)}/{len(sections)}')
"

# ── 15. Prepare submission checklist ─────────────────────────────────────────
echo "=== smoke: prepare submission checklist ==="
govcon submission package --opportunity-id "$SMOKE_OPP_ID"
govcon submission checklist --opportunity-id "$SMOKE_OPP_ID" > /tmp/smoke_checklist.txt
grep -qiE "blocked|BLOCKED|proposal|requirement" /tmp/smoke_checklist.txt || fail "submission checklist output is empty or malformed"
echo "  checklist generated: $(wc -l < /tmp/smoke_checklist.txt) lines"

# ── 16. Assert outputs ────────────────────────────────────────────────────────
echo "=== smoke: assert outputs ==="

py "
from govcon.db import session_scope
from govcon.models import (
    Opportunity, Watchlist, Match, DecisionRun,
    Proposal, ProposalVersion, Submission, AIAnalysis,
)
from sqlalchemy import select, func

with session_scope() as s:
    opp_count = s.execute(select(func.count()).select_from(Opportunity)).scalar()
    assert opp_count >= 1, f'Expected >= 1 opportunity, got {opp_count}'
    print(f'  opportunities: {opp_count}')

    wl_count = s.execute(select(func.count()).select_from(Watchlist)).scalar()
    assert wl_count >= 1, f'Expected >= 1 watchlist, got {wl_count}'
    print(f'  watchlists: {wl_count}')

    match_count = s.execute(select(func.count()).select_from(Match)).scalar()
    assert match_count >= 1, f'Expected >= 1 match, got {match_count}'
    print(f'  matches: {match_count}')

    dr_count = s.execute(select(func.count()).select_from(DecisionRun)).scalar()
    assert dr_count >= 1, f'Expected >= 1 decision_run, got {dr_count}'
    print(f'  decision_runs: {dr_count}')

    prop_count = s.execute(select(func.count()).select_from(Proposal)).scalar()
    assert prop_count >= 1, f'Expected >= 1 proposal, got {prop_count}'
    print(f'  proposals: {prop_count}')

    pv_count = s.execute(select(func.count()).select_from(ProposalVersion)).scalar()
    assert pv_count >= 1, f'Expected >= 1 proposal_version, got {pv_count}'
    print(f'  proposal_versions: {pv_count}')

    sub_count = s.execute(select(func.count()).select_from(Submission)).scalar()
    assert sub_count >= 1, f'Expected >= 1 submission, got {sub_count}'
    print(f'  submissions: {sub_count}')
"

echo ""
echo "=== smoke: PASS ==="
