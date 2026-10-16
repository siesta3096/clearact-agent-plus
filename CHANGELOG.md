# Changelog

## 0.2.1 — 2026-10-16

- Add optional declarative acceptance checks for files, successful tool calls and distinct opened
  web sources. Count evidence from the current user turn across phases; search snippets, stale
  files, reused results and planning markers do not satisfy new checks.
- Verify recorded file content and bounded DOCX/XLSX core structure without executing commands,
  calling a model or reading outside the current workspace. Preserve external edits.
- Add terminal `needs_review` status when completion issues remain. Allow one bounded repair
  opportunity for unmet checks when iteration budget permits; clear warnings after successful repair.
  Failed or denied work alone does not trigger an extra model request or approval retry.
- Persist per-condition results and completion events; show reports and honest status in Web/CLI.
  Load legacy records and clear stale reports on follow-up without imposing earlier-turn checks.
- Add acceptance regression scenarios and document the implementation, validation and remaining limits.

## 0.2.0 — 2026-10-13

- Added explicitly approved command execution with argv input, workspace cwd, bounded stdout/stderr,
  exit codes, effective timeout reporting and cancellation cleanup. Windows commands run in a
  kill-on-close job; command execution is not a filesystem or network sandbox.
- Added native DOCX and XLSX generation: document headings, paragraphs, lists and tables; workbook
  sheets, styles, formulas and charts. Formulas recalculate in Excel; they are not evaluated locally.
- Added binary before-images, atomic artifact writes, safe phase rewind and task-scoped downloads
  that reject changed or unrelated files. External command effects remain explicitly unrecoverable.
- Reconcile unfinished tasks at gateway startup, preserve completed results, close unmatched tool
  calls as interrupted and require inspection before continuation; do not replay old approvals.
- Coordinate gateway, CLI execution and snapshot restoration with a process-reentrant OS data
  directory lock; reject a second writer before reconciling its active tasks.
- Fixed browser press/scroll/back/reload argument forwarding, common no-web instruction detection,
  and loss of intermediate user requirements during context compaction.
- Added reproducible execution, artifact, cancellation, recovery and permission regression scenarios.

## Unreleased

- Added a ready-to-copy local configuration and bounded MCP service discovery so an unavailable server cannot block a run.
- Kept all registered tools available across workflow phases, and made context compaction retain complete tool-call transactions within an explicit input budget.
- Persisted Web run execution settings so follow-ups retain their authorized directory, model profile, limits, and language; hardened stop, failure, title, and deletion recovery.
- Recorded failed, cancelled, and budget-exhausted tool actions as matching tool results, without creating success checkpoints for failed outcomes.
- Fixed Web runs ignoring the configured autonomy level and added in-console approval decisions.
- Prevented white autonomy from reading or listing files outside the selected workspace.
- Hardened Web rendering against attribute injection and unsafe link protocols.
- Replaced process-wide DNS patching with connection-level address pinning and disabled proxy bypasses for safe fetches.
- Added a sanitized `clearact.json.example` configuration template.
- Removed the local plaintext provider key from the working configuration.
- Isolated the default workspace to the repository-local `workspace/` directory.
- Added safer default autonomy (`yellow`) and aligned default run budgets.
- Added `clearact doctor` for local installation diagnostics.
- Made run-record writes atomic and prevented deleting or restarting active topics.
- Added bounded MCP import validation and expanded security documentation.
