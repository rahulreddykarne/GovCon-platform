Submission packages now retain an immutable manifest for each assembled version.
The manifest records each filename, byte count, SHA-256 hash, and completed
actions. Required files and amendment acknowledgments are checked against that
snapshot. Changed files or changed submission instructions require a new
pre-flight and approval before recording confirmation.

Generate instructions, export the proposal, and gather supporting files. Then
prepare package JSON, for example:

```json
{
  "proposal_version_id": 42,
  "submission_method": "email",
  "recipient_email": "buyer@agency.gov",
  "files": [
    {"name": "proposal.pdf", "local_path": "C:/packages/proposal.pdf", "role": "proposal", "page_count": 12},
    {"name": "SF30.pdf", "local_path": "C:/packages/SF30.pdf", "role": "acknowledgment", "form_id": "SF30", "signed": true}
  ],
  "amendments_acknowledged": ["0001"],
  "representations_complete": true,
  "certifications_complete": true
}
```

Use the real proposal version and solicitation instructions. Local paths point
to the actual files that will be submitted. Assembly computes hashes and sizes:

```powershell
govcon submission assemble --opportunity-id 123 --package-json package.json --actor-email approver@example.com
govcon compliance preflight --opportunity-id 123
```

Pre-flight uses the stored assembled manifest by default. An explicit
`--package` still accepts a manifest JSON file with hashes and sizes, but every
file must also have a `local_path` whose bytes match them: a manifest that only
describes files fails `package_integrity` in pre-flight, the readiness gate and
confirmation. Proposal
export supports `--format pdf`, along with DOCX, XLSX, and ZIP. ZIP export includes
assembled supporting files and a hash manifest; missing required content or a
generation error fails the export.

Apply Alembic migration `01a2b3c4d5e6` before using these changes. It preserves
legacy outcome rows in correction history and duplicate submission rows in an
append-only archive before adding uniqueness constraints. Legacy invalid
numeric values remain in history and become null in the current outcome.
Outcome changes require an approver and a recorded submission for won/lost.
Terminal outcome types cannot change, but their details can be corrected.

The scheduler now stores schedules in PostgreSQL, coordinates daemons and job
chains with advisory locks, and marks abandoned runs failed before retrying
their chain. Opportunity embedding jobs rebuild vectors when source text or
model identity changes, including resolved model commits. Set
`EMBEDDING_MODEL_REVISION` to pin a revision. The vector schema remains 384-dimensional; providers
with another dimension fail explicitly rather than storing incompatible data.
