# Changelog

This public changelog intentionally omits deployment topology, private service
identifiers, production log excerpts, credentials, and environment-specific
filesystem paths.

## Unreleased (personal fork)

- Corrected the documented fork source to
  `Aowen-Nowor/hermes-lark-streaming` and restored full installation,
  configuration, troubleshooting, and maintenance documentation.
- Documented the local `hermes_lark_streaming.text_sizes` feature for
  independent PC/mobile sizing of body, reasoning, tool, notice, and footer
  text.
- Added a maintenance context with the upstream baseline, sync workflow,
  lifecycle invariants, regression commands, and private-information rules.

## v1.6.1 (2026-08-07, personal fork)

- Simplified the README into a fork-of-upstream, personal-use statement that
  records the local `text_sizes` feature.
- Restored upstream release tooling (`scripts/`, `.workflow/release-pipeline.yml`)
  deleted from an earlier cleanup.
- Bumped version to 1.6.1.

## v1.6.0 (2026-07-21)

- Fixed Clarify cards after Hermes platform adapters are loaded lazily.
- Added adapter-creation hook coverage and Clarify dispatch regression tests.
- Kept compatibility checks for supported Hermes releases.

## v1.5.0 (2026-07-08)

- Simplified the CardKit streaming lifecycle and removed unused fallback paths.
- Consolidated CardKit error handling.
- Removed obsolete header transition and repatching code.

## v1.4.x (2026-06-30 to 2026-07-07)

- Fixed Clarify card action handling and suppressed invalid `/card` fallback
  commands.
- Fixed stale bound-method behavior in the Feishu SDK integration.
- Fixed repeated element-not-found retries during streaming updates.
- Improved compatibility with deferred-loaded Feishu platform adapters.

## v1.3.x (2026-06-23 to 2026-06-25)

- Improved concurrency, session cleanup, retry behavior, and streaming-card
  finalization.
- Fixed markdown placeholder leakage and several CardKit edge cases.
- Expanded unit, integration, and end-to-end coverage.

## v1.2.x (2026-06-22 to 2026-06-23)

- Added configurable card presentation and operational diagnostics.
- Improved configuration reload behavior, rate-limit handling, and error cards.
- Added broader compatibility and regression testing.

## v1.1.x and earlier

- Introduced the CardKit v2 streaming lifecycle, unified reasoning/tool panel,
  plugin commands, monitoring, and installation diagnostics.
- Added Feishu/Lark compatibility, message fallback handling, and packaging
  fixes.
