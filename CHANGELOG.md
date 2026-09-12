# Changelog

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
