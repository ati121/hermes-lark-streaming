# Changelog

This public changelog intentionally omits deployment topology, private service
identifiers, production log excerpts, credentials, and environment-specific
filesystem paths.

## v1.6.6 (2026-08-20, personal fork)

### Fixed — output speed for multi-call turns

- Uses the final API call's output tokens for the `speed` footer field instead
  of Hermes's cumulative session total, preventing inflated values such as
  `330 t/s`, `475 t/s`, and `1976 t/s` on turns with tool loops.
- Keeps the cumulative token total available in the existing `tokens` footer
  field.

The `v1.6.5` tag remains available as the rollback point for the original
output-speed implementation.

## v1.6.5 (2026-08-20, personal fork)

### Added — output speed footer field

- Adds a `speed` footer field showing output throughput such as `50 t/s`,
  enabled by default. Remove it from `footer.fields` to hide it.
- Measures the rate over the window from the first answer token to completion,
  so time-to-first-token and tool execution do not dilute the figure. For agent
  turns with tool loops, the numerator is taken from the final API call rather
  than Hermes's cumulative session output token total. Falls back to the whole-
  message duration when the answer arrives in one piece rather than streaming.
- Suppresses the field when output tokens are absent or the measurement window
  is too short to be meaningful.

The `v1.6.4` tag remains the rollback point for the previous footer layout.

## v1.6.4 (2026-08-19, personal fork)

### Fixed — plugin scan compatibility

- Moved the Feishu integration-test notification code from workflow YAML into a
  Python script so environment-variable handling receives the scanner's normal
  source-code treatment.
- Removed an unused notification script that read an unrelated secret at import
  time.
- Removed secret-length and broad environment dumps from the release workflow.
- Reduced integration-test checkout permissions to read-only and disabled
  persisted checkout credentials before installing upstream dependencies.
- Clarified the initial card state as `等待上游模型响应` / `Waiting for upstream
  model...`.

The existing `v1.6.2` and `v1.6.3` tags remain rollback points.

## v1.6.3 (2026-08-18, personal fork)

### Added — tool progress labels and status markers

- Shows the currently called tool beside the streaming indicator, with localized
  Chinese names for Hermes tools and Hindsight memory operations.
- Keeps the last tool label visible until the next tool starts, so fast tools do
  not make the status flicker away immediately.
- Adds green, stop-sign, and red markers to completed, `/stop`-interrupted, and
  failed footer statuses.
- Localizes the unified panel title to `执行过程` while retaining the English
  `agent loop` title.
- Falls back to the unified panel header when the CardKit transport does not
  accept a partial update to the streaming indicator.

### Fixed — tool label flush timing

Tool-label updates now run before the streaming flush phases. This prevents
phase-level early returns from skipping the update entirely.

### Tests

- Added coverage for real Hermes tool names, Hindsight naming, localized labels,
  sticky tool status, CardKit fallback behavior, and footer markers.
- Focused card tests and end-to-end tests pass.


### Fixed — gateway startup deadlock

Resolving Hermes internals could hang the gateway permanently before it wrote
a single log line or connected any platform.

`HermesCompat._resolve_modules()` used blocking imports (`from gateway.run
import GatewayRunner` and five similar sites). Hermes calls plugin `register()`
from a background discovery thread while holding its plugin-discovery lock, and
the main thread asks for that same lock from inside `import gateway.run`,
because tool-module import triggers plugin discovery as a side effect. The two
threads then wait on each other: the discovery thread wants a module import lock
the main thread owns, and the main thread wants the discovery lock the discovery
thread owns. Neither wait has a timeout, so the process never recovers.

The fix adds `_try_import()`, which reads `sys.modules` first — never blocking,
and tolerating partially initialized modules — and only falls back to a real
import when no host module import is in flight on another thread. The main
thread is exempt because a module import lock is re-entrant for its owner. All
six resolution sites and the `platform_registry` lookup now use it.

Because any target can now start out unresolved, the deferred patch thread
retries every unfinished patch (gateway runner, conversation loop, agent, cron,
platform adapter, adapter-creation hook) instead of only the gateway runner. Its
backoff starts at 0.1s and grows to 2s, so patches land before the first message
is dispatched; the 60-second deadline is unchanged. Per-target completion flags
prevent a retry from wrapping an already-wrapped callable.

Also fixed: resolution no longer replaces `sys.modules["agent.conversation_loop"]`
while the host is still executing that module, which corrupted the host's own
import.

Note on the host side: plugin discovery executes arbitrary third-party imports
while holding a lock, with no timeout. This release removes our side of the
cycle, but the same pattern is reachable by any plugin whose import blocks.

### Documentation

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
