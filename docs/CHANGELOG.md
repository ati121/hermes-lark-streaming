# Changelog

This public changelog intentionally omits deployment topology, private service
identifiers, production log excerpts, credentials, and environment-specific
filesystem paths.

## v1.6.13 (2026-08-23, personal fork)

### Fixed — first upstream activity now shows model thinking

- Treats reasoning markers such as `<think>` and `Reasoning:` as upstream
  activity even when the first chunk has no visible answer text.
- Treats whitespace-only control chunks the same way without starting the
  visible-answer speed timer.
- Ensures the spinner transitions from `等待上游模型响应` to `模型思考中...`
  during the first model generation phase, including after a tool call.

## v1.6.12 (2026-08-22, personal fork)

### Removed — upstream Gitee release tooling

- Deleted `scripts/create_release.py` and `.workflow/release-pipeline.yml`.

Both belong to upstream's Gitee Go pipeline and cannot fire in this fork: the
workflow triggers on pushes to `github_sync`, a branch that does not exist here
(this repo is `main`), and the script posts to the Gitee v5 API using
Gitee-issued `OWNER`/`TOKEN` credentials that were never configured. Releases
in this repository have always been created by hand — v1.6.11 included — so
neither file has ever run.

Recorded because v1.6.1 restored this same pair after an earlier cleanup
removed it: the tooling looks like it belongs to the release process, and the
only way to tell it doesn't is to notice which forge it targets. It is not
dead code to be revived — it is live code for a different repository.

## v1.6.11 (2026-08-22, personal fork)

### Changed — spinner row reads as a status line, not a sentence

- Each tool now shows an emoji before its name, resolved from the icon token
  the tool already declares in `_TOOL_SPECS`. One entry per token covers a
  whole family, so both the exact specs and the legacy alias table are handled
  without a per-tool mapping; a by-name table sits above it for the tokens that
  span unrelated tools (`time_outlined` is both memory and cron,
  `report_outlined` is every media tool). MCP and unknown tools fall back to a
  default mark, so the row never loses its shape.
- Dropped the `正在调用` / `Calling ...` prefix from the row. The spinner
  already reads as in-progress and every tool title is itself a verb phrase, so
  the prefix was a third repetition — and produced doubled verbs on titles like
  `读取文件`. The panel-title fallback keeps its prefix: there the label is
  appended to `agent loop · …` and needs the verb to parse.
- The thinking state carries its own emoji for the same reason, so the text
  doesn't shift sideways when a tool takes over the row and hands it back.
- Added leading padding (EN SPACE, not ASCII — a leading ASCII run is the sort
  of thing a renderer feels free to collapse) so the text isn't crowded against
  the spinner.

Note: these are Unicode emoji, which Feishu renders in colour but not animated.
Animated stickers need `lark_md`, and the row must stay `plain_text` — Feishu
rejects a changed `tag` on a partial update, which would drop the status line
into its degraded path.

## v1.6.10 (2026-08-22, personal fork)

### Fixed — 模型思考中 is now actually visible

v1.6.9 pushed the status onto `context_loading_hint`, but that element is
deleted in the same flush that renders the first visible content. The update
and the deletion were one or two API round-trips apart, so the line was gone
before it could be read: the card still appeared to jump from
`等待上游模型响应` straight to the answer, and the status push was a wasted
request. The scheduling was self-defeating too — `on_answer` flipped to
`thinking` and fired a status-only flush, but that flush is fire-and-forget, so
by the time it ran `answer_dirty` was already set and it took the full render
path.

- The status now lives on the spinner row (`loading_icon`), which survives
  until the card is sealed. It shows `模型思考中...` / `Model is thinking...`
  from the first upstream token, a running tool's name while a tool holds the
  turn, and returns to thinking when the tool finishes.
- A card created mid-stream opens directly on `模型思考中...` with no waiting
  hint, instead of flashing a stale `等待上游模型响应`.
- The waiting hint is dropped on the first upstream token even when that token
  renders nothing visible (reasoning with the panel hidden), so the card can no
  longer show `等待上游模型响应` above a spinner that says thinking.
- Removed the now-redundant status-only flush from the answer path; the hint
  rewrite is kept only as a fallback for cards where Feishu has rejected
  spinner-row updates.
- Content elements now anchor their `insert_before` to the hint when present
  and to the spinner otherwise — the hint is absent on mid-stream cards, and
  previously a discarded hint would have broken the insert.

## v1.6.9 (2026-08-22, personal fork)

### Fixed — first upstream byte always shows model thinking

- The placeholder switches from `等待上游模型响应` / `Waiting for upstream
  model...` to `模型思考中...` / `Model is thinking...` on the first upstream
  token of any kind. Previously only reasoning/thinking callbacks triggered
  the switch; a turn whose first delta was answer text jumped straight past
  it, leaving the stale waiting hint (or a blank card) until the answer
  element rendered.
- Tool-only openings (a model call that starts with a tool call and emits no
  text) are also treated as upstream activity for the creation-time snapshot.

## v1.6.8 (2026-08-21, personal fork)

### Fixed — distinguish upstream waiting from model thinking

- Detects native reasoning and interim thinking callbacks even when the
  configured reasoning panel is hidden.
- Changes the initial placeholder from `等待上游模型响应` / `Waiting for
  upstream model...` to `模型思考中...` / `Model is thinking...` as soon as
  upstream reasoning begins.
- Keeps tool activity and visible answer streaming as separate response phases,
  so the placeholder and spinner do not report stale status between phases.

## v1.6.7 (2026-08-21, personal fork)

### Fixed — align output-speed numerator and timing window

- Resets the speed measurement window when a tool starts, so pre-tool text and
  tool execution time do not dilute the final model call's output rate.
- Measures from the first to the last visible answer delta instead of ending at
  completion bookkeeping.
- Subtracts reasoning tokens from the final API call's output token count.
- Hides `speed` when final-call usage or a meaningful streamed-answer window is
  unavailable. It no longer mixes cumulative session tokens or whole-message
  duration with a final-call window.

The `v1.6.6` tag remains available as the rollback point for the first
final-call token implementation.

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
