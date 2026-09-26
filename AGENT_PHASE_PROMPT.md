# Agent Phase Implementation Prompt

You are implementing the GovCon platform from `MASTER_SPEC_v2.5.md`.

## Current task

Implement **only**:

`<CURRENT_PHASE_FILE.md>`

## Required behavior

1. Read `MASTER_SPEC_v2.5.md` first for architecture and product context.
2. Read the current phase spec completely.
3. Inspect the repository and `IMPLEMENTATION_STATUS.md`, `SPEC_DEVIATIONS.md`, and `DECISIONS.md`.
4. Verify all phase dependencies are complete.
5. Before coding, provide a concise phase implementation plan tied to the phase deliverables and acceptance criteria.
6. Verify all `⚠️ VERIFY` items against current official/live interfaces before implementation.
7. Reuse existing code; do not create duplicate database/config/HTTP/provider/service layers.
8. Implement only the current phase plus minimal dependency fixes.
9. Add/run the tests required by the phase.
10. Verify every acceptance criterion explicitly.
11. Do not start the next phase.
12. Update `IMPLEMENTATION_STATUS.md`.
13. Record confirmed spec deviations in `SPEC_DEVIATIONS.md`.
14. Record durable architecture choices in `DECISIONS.md`.
15. Finish with:
   - files changed
   - tests run/results
   - acceptance-criteria checklist
   - unresolved blockers
   - exact recommended next phase

Do not mark a phase complete while a required acceptance criterion is failing.
