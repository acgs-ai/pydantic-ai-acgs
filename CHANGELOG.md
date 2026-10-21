# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Entries for new releases are generated with `cz bump`.

## [Unreleased]

## 0.1.0 (unreleased)

### Added

- `ACGSCapability`, a Pydantic AI capability that checks each function tool call against an ACGS constitution in
  `after_tool_validate` and again in `before_tool_execute`. Denied calls raise `ToolFailed`, so the tool does not
  run and the model gets a failed tool result with the reason, without using up the tool's retries.
- `on_review` (`"defer"` or `"deny"`) for calls that only match review rules. `"defer"` (the default) raises
  `ApprovalRequired`, so the call becomes a deferred tool call carrying the ACGS decision in its metadata; it runs
  only once approved, and a blocking rule still denies an approved call.
- `CapabilityOrdering(position="innermost")`, so the execute-stage check sees arguments changed by other
  capabilities.
- Agent spec support (`ACGSCapability` in `capabilities:` with `custom_capability_types`).
- Fail-closed handling: any error while evaluating a call denies it instead of ending the run.
- `format_tool_call`, the documented text representation of a tool call that ACGS rules are evaluated against.
