# Changelog

## Unreleased

- Added a sanitized `clearact.json.example` configuration template.
- Removed the local plaintext provider key from the working configuration.
- Isolated the default workspace to the repository-local `workspace/` directory.
- Added safer default autonomy (`yellow`) and aligned default run budgets.
- Added `clearact doctor` for local installation diagnostics.
- Made run-record writes atomic and prevented deleting or restarting active topics.
- Added bounded MCP import validation and expanded security documentation.
