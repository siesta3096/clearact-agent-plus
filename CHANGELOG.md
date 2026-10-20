# Changelog

## 0.3.3 — 2026-10-20

- Add an opt-in real localhost-model evaluation with isolated state, actual HTTP token usage and
  timings, independent code/browser/CSV/Office checks, and nullable usage for missing service counts.
  Preserve all 82 requests across baseline, intermediate failures and retests in a sanitized report.
- Omit tool schemas for explicit no-tool requests and self-contained arithmetic; omit Office schemas
  for plain text/source targets unless affirmatively requested. Recognize named source modifications
  as development while keeping source-reference text outputs in general execution.
- Match multiline edits across LF/CRLF/CR while preserving untouched bytes, local line endings,
  unique matching, version checks and snapshots.
- Let new-file writes satisfy inspection through existing destination/version checks. Keep prior
  inspection for existing-file changes and bound final-answer repair to one opportunity.
- Interpret Excel `number` as a numeric format and normalize built-in aliases without altering
  custom formats. Reopen original model-generated workbooks to verify numeric values and formulas.
- Keep located excerpts of older successful text reads before dropping transactions; preserve full
  original records and the latest transaction. Verify split-batch fact retention and cached re-reading.
  See [real evaluation and limitations](docs/maintenance-2026-10-20-real-evaluation.md).

## 0.3.2 — 2026-10-20

- Record the author's original simplicity, lightweight operation and context-efficiency principles.
- Make planning optional for general tasks as well as development. Permit direct answers and immediate
  work; omit tool schemas for greetings, while preserving optional plans and their completion checks.
- Omit returned reasoning from request copies by default, including below the hard budget; preserve
  complete local records and the `reasoningHistory: full` override. Compact old process transactions at
  a soft 32,000-unit target while retaining user constraints and the latest complete tool transaction.
- Shorten general and development guidance. Remove blanket source-count, architecture-document and
  new-HTML browser-interaction requirements; retain real-file delivery, post-edit verification and
  explicit file/test requirements. Check explicit text-file delivery even without a plan.
- Add deterministic request-size measurements, small-task regressions and current-turn history notices.
  See [maintenance and measurement](docs/maintenance-2026-10-20-lightweight.md).

## 0.3.1 — 2026-10-20

- Prevent long returned thinking from blocking the next tool round: when over budget, omit
  assistant reasoning from request copies before compacting useful transactions. Preserve the
  complete run ledger, user constraints, tool arguments/results and call IDs. Support the
  per-profile `reasoningHistory: full` override for strict reasoning replay.
- Treat short status questions as read-only inquiries; report actual work and preserve unfinished
  plan status instead of starting implementation again.
- Stop with a recoverable review status when required context itself cannot fit; preserve completed
  actions and files without automatically repeating them. Show context-compaction notices in Web.
- Add a read-only saved-run replay and regressions covering long thinking, real file delivery and
  browser interaction, genuine essential-content overflow, status inquiries and configuration.
  See [failure analysis and validation](docs/maintenance-2026-10-20-context.md).

## 0.3.0 — 2026-10-18

- Add development mode with immediately available file tools, workspace inspection, actual file delivery,
  project structure/documentation requirements and verification after the final edit. Planning becomes
  an optional progress aid for development tasks; discussion mode offers read-only tools.
- Add bounded code search, exact text editing with snapshots/version checks, Python/JSON/HTML/JS checks,
  and real local browser interactions, assertions, error collection and screenshots.
- Generate development delivery summaries from recorded files/checks instead of unverified model prose.
  Preserve the model draft for audit, permit bounded repair, and show incomplete delivery honestly.
- Recheck unfinished plans on execution follow-ups; refresh runtime-owned guidance in the model view
  without rewriting history. Record actual offered tool names. Preserve interrupted work and distinguish
  exploratory read failures overcome by verified delivery from unresolved work.
- Expose task modes, file lists, changes, historical deliveries, verified per-turn ZIP downloads and
  preview screenshots in Web; add CLI task mode. Make native text writes atomic and hash-addressed.
- Add real filesystem/test/browser regressions and an opt-in localhost model project evaluation.
  See [development design and validation](docs/development-agent-2026-10-18.md) for evidence and limits.

## 0.2.4 — 2026-10-18

- Replace the fixed 90-second model timeout with per-profile `requestTimeoutSeconds` (default
  600 seconds, range 1–7200). Keep connection, write and pool timeouts separate from generation waits.
- Stop automatically resending a generation after `ReadTimeout`; explain that the backend may
  still be running. Preserve bounded retries for connection failures and transient service errors.
- Expose the wait setting in Web, record waiting/received events and show a waiting banner while
  a response is pending. Preserve cancellation and successful tool results.
- Show warning markers for failed results and avoid attributing every planning failure to tool setup.
- Add timeout/configuration/cancellation regressions and verify a real delayed HTTP response beyond
  90 seconds, with a single request and no model or user-file side effects.

## 0.2.3 — 2026-10-18

- Fix stage restart silently doing nothing when feedback is empty. Blank or whitespace feedback
  now uses an explicit retry instruction shown in the existing scope preview before confirmation.
- Mark feedback optional, show immediate checking status and inline failures, and block duplicate
  submission while the restart flow is pending. Preserve typed feedback and rewind safeguards.
- Add blank/cancel/error/confirmation regressions and verify the reloaded UI in real Edge.

## 0.2.2 — 2026-10-18

- Fix local chat-template rejection after planning: combine trusted system-role notes into one
  leading system message at the OpenAI-compatible and Ollama transport boundaries. Preserve
  user/tool order, tool-call IDs, returned reasoning and the original run ledger.
- Recognize the specific system-message ordering error without exposing raw service responses.
- Explain the initial planning-only tool list and point to the next phase after planning.
- Add strict-service regressions for both providers and verify requests against the local model.

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
