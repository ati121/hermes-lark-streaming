# Changelog

This public changelog intentionally omits deployment topology, private service
identifiers, production log excerpts, credentials, and environment-specific
filesystem paths.

## v1.6.37 (2026-09-21, personal fork)

### Fixed — a message sent while the bot was busy kept answering in the old card

- A message that arrives while an agent is already running is handed to
  `_handle_active_session_busy_message`, which queues it as the next turn and
  interrupts the running one. That entry bypasses `_handle_message_with_agent`
  — Hermes's own note reads "busy callbacks bypass the message handler" — so the
  plugin never got a new message id: the start hook never fired, no new card was
  created, and everything the follow-up produced kept streaming into the card of
  the turn it had just interrupted. The output landed above the fold and the user
  had to scroll back to find it.
- The plugin now wraps that busy entry and, once it has consumed a real user
  message, seals the card in progress and continues on a fresh one. Internal
  events (background-delegation completions, heartbeats) share the same entry and
  are ignored — they are frequent and are not a user interruption. The sealed
  card is marked as superseded rather than completed, so it does not read as a
  finished answer, and the new card is anchored to the message that triggered it.
- Later callbacks still carry the interrupted turn's message id, so a
  continuation id is now resolved in `_get_active_session`: answers, reasoning
  and tool updates all land on the new card without each hook having to know
  about it. An aborted turn's completion no longer consumes the continuation —
  that card belongs to the next turn and is sealed by its own completion.
- Set `hermes_lark_streaming.busy_supersede_new_card: false` to keep the previous
  behaviour (everything stays on the card that was interrupted).

## v1.6.36 (2026-09-21, personal fork)

### Fixed — a CLI called through a shell variable still showed 终端命令

- The bots often pin a program to a variable on the same line and call it
  later — `GH=/opt/data/.local/bin/gh; … $GH api repos/…` or
  `export GH=…` followed by `$GH pr list`. The matcher only read literal
  program names, so every one of those rows fell back to 🖥️ 终端命令 even
  though `gh` alone has rendered as 🐙 GitHub since v1.6.33. `NAME=value`
  and `export NAME=value` assignments are now collected first and `$NAME` /
  `${NAME}` references resolved through them; the interpreter rule applies
  to the resolved name too, so `$PY zimage_gen.py` still reads as 🎨 生成图片.
- The detail line no longer stops at a leading assignment: when the program
  is only reached in a later shell segment, the row restarts from that
  segment, so the line reads `api repos/…` instead of `GH=gh; echo …`.
- Unresolved or unrelated variables are untouched — `$DC exec …` (docker),
  `SSH_OPTS="-F …"` and a bare `$NOT_SET` all stay 🖥️ 终端命令.

## v1.6.35 (2026-09-21, personal fork)

### Fixed — image scripts still showed 终端命令 on Feishu; OpenViking knowledge base reads labelled as memory

- On Feishu Hermes caps the tool preview at 40 characters
  (`display.tool_preview_length`, tier default), so the preview for
  `python3 /opt/data/.hermes/profiles/image/workspace/scripts/zimage_gen.py …`
  ends at `profiles/im...` and never contains the script name; the v1.6.34
  matcher had nothing to match. `tool.started` also carries Hermes's
  display-redacted argument dict as its fourth positional, which the plugin
  now keeps on the tool step and prefers over the preview: the row is
  identified from the full command, and its detail line is rebuilt from the
  full arguments (clipped to one line of 80 characters) so `gh api …` no
  longer stops at 40 characters either.
- `viking_read` / `viking_browse` on a `viking://resources/…` URI now render
  as 📖 OpenViking · 知识库 / 浏览知识库; `viking://user/…` keeps the memory
  labels. The URI is not a preview key, so this too comes from the argument
  dict.

## v1.6.34 (2026-09-21, personal fork)

### Added — image-generation scripts run through terminal render as 🎨 生成图片

- The image bot drives its generators as `python3 …/zimage_gen.py "prompt"`,
  `gpt_image_gen.py` and friends, which the card showed as 🖥️ 终端命令.
  Terminal matching now looks past an interpreter (`python3`, `bash`, `node`
  …) to the script it runs, and a new pattern table maps any program or
  script whose basename contains `image` to the native `image_generate`
  title and emoji. `image` appearing only in arguments (`pip install
  imageio`, `ls …/images/`) does not match. The detail line drops the
  interpreter and script and keeps the arguments.

## v1.6.33 (2026-09-21, personal fork)

### Added

- `gh` (GitHub CLI) joins `_TERMINAL_PROGRAM_SPECS` and renders as
  🐙 GitHub instead of 🖥️ 终端命令; the detail line keeps only the
  subcommand and arguments (`api repos/...`).

## v1.6.32 (2026-09-21, personal fork)

### Added — terminal programs can render as their own tool row

- A CLI the owner installs is reported by Hermes as a `terminal` call with
  the command line as the preview, so the card showed 🖥️ 终端命令 for it.
  `_TERMINAL_PROGRAM_SPECS` in `state/tooluse.py` maps a program name to its
  own title and emoji; a listed program renders as itself in the tool row,
  the spinner label and the panel-header fallback, and its name is dropped
  from the detail line. The program is matched at the head of each shell
  segment (after `sudo`/`env`/variable assignments, with any path stripped),
  so it does not fire when the name only appears as an argument. First
  entry: `smart-search` → 🔍 smart-search.

## v1.6.31 (2026-09-21, personal fork)

### Changed — the process panel lists tools only on interactive cards

- With the reasoning block pinned above it, the collapsible `执行过程` panel
  was rendering the same reasoning a second time. On the interactive card
  path the panel now shows tool steps only and its title drops the
  `N 轮` count; a turn with no tool calls renders no panel at all instead
  of an empty shell. The CardKit path has no pinned block and keeps
  reasoning inside the panel.

## v1.6.30 (2026-09-20, personal fork)

### Added — a pinned reasoning block with an expand/collapse button

- The interactive card now shows the model's reasoning outside the collapsed
  panel: while streaming, a `🫧 思考过程` button plus the latest two lines of
  thought; once the answer is sealed, only the button. Tapping it renders the
  full reasoning (capped at `REASONING_EXPANDED_LIMIT`, 5000 characters, to
  stay under the ~30KB message-card ceiling); tapping again collapses it.
- The button keeps working after the session is released: the controller
  keeps a bounded snapshot of the sealed card (`_REASONING_SNAPSHOT_CARDS`,
  60 cards) and re-renders it through the same whole-card PATCH the seal
  uses. When a snapshot has been evicted, the tap replies with a short note
  instead of doing nothing.
- Card-action callbacks carry `hls_action: reasoning_toggle`; the adapter
  wrapper routes those to the controller and suppresses the `/card` synthetic
  command. The `expanded` flag is parsed leniently because Feishu may return
  it as the string `"false"`.

### Changed — reasoning found inside the answer stream is no longer dropped

- `<think>` / `<thinking>` / `Reasoning:` segments in `stream_delta` are
  split out per session by `ReasoningStreamSplitter` and fed to the reasoning
  panel; tag pairs split across two chunks are handled by holding the
  open/close state and the partial tag tail until the next chunk.
- `reasoning.available` events from Hermes carry the first 500 characters of
  the answer body for models without native chain-of-thought; only real
  reasoning tag segments are shown as thinking now.

### Fixed

- `strip_reasoning_tags` never removed an unclosed `<think>` tail: the stray
  open tag was deleted first, so the "drop everything after an unclosed tag"
  step could never match, and the reasoning text leaked into the answer at
  completion reconciliation.
- Tapping the reasoning button while the seal PATCH was in flight could
  overwrite the sealed card with a non-final "Processing..." card. The toggle
  now only flips the flag once the session is `COMPLETING` or terminal.
- The auto-collapse at completion was wired into `enter_terminal`, which the
  normal completion path never calls; the reset now happens right before the
  seal card is built.
- `on_reasoning_toggle` iterated `_sessions` without the lock while worker
  threads mutate it; it now uses the locked snapshot helper.
- Two `tests/test_config.py` cases failed on Windows (cleared `USERPROFILE`
  broke `Path.home()`, and a forward-slash string assertion).
- `.pi/` planning scratch is now ignored like the other local agent dirs.

### Docs

- `SKILL.md` was three months stale: the Hook index now follows the injection
  numbering in `patching/hooks.py` (it had disagreed with the architecture
  diagram in the same file), the test list covers every file under `tests/`,
  the "3–4 elements" claim is corrected, and multiplex isolation, memory
  prefetch, the speed field and the reasoning block are summarised as design
  decisions 4.18–4.21.
- `AGENT_GUIDE.md` documents the reasoning block, splits the speed-field
  paragraph into a list, finishes a sentence that had been cut off, drops
  the hand-maintained "last updated" line, and strips deployment-specific
  paths and boot timestamps from the `.install-*` section (the CHANGELOG
  entry for v1.6.29 is trimmed the same way).
- `plugin.yaml` now declares `on_memory_prefetch_updated`, matching the code
  and the guide. `$HERMES_HOME` replaces the hard-coded `~/.hermes` in
  `SKILL.md` and `ISSUES_TEMPLATE.md`. `README.zh-CN.md` was a byte-for-byte
  copy of the Chinese-only `README.md` and is removed.

## v1.6.29 (2026-09-19, personal fork)

### Fixed — Chinese cards showing English tool rows, and five padlocks that all looked alike

- Translates 53 Hermes tools that were falling back to the English
  `_humanize_tool_name` output on a `zh_cn` card. The tool table in
  `state/tooluse.py` had been written against older Hermes builds, so its keys
  (`todo`, `cronjob`) no longer matched the live registrations
  (`todo_list`, `cronjob_manage`, `process_manage`). Exact specs are added for
  the whole reachable set: the gateway tools (`process_manage`, `todo_list`,
  `cronjob_manage`, `browser_vault_*`, `manage_connections`, `computer_use`),
  the kanban board (14), Spotify (7), Discord (2), Yuanbao (5), the desktop GUI
  toolset (8), xAI video (2), Hermes-internal schemas (5) and the `ha_*` /
  tooling helpers (7). Family labels read `家族 · 动作`, matching the existing
  `Hindsight · 记忆写入` rows.
- Fixes five `browser_vault_*` tools collapsing into a single "Browser" row.
  The legacy prefix alias `browser` in `_TOOL_DESCRIPTORS` matched them first;
  an exact spec now wins, and each half of the flow gets its own label
  (list / unlock / fill / save login / one-time code).
- Replaces the `standard_icon` token on a tool panel row with a coloured emoji.
  Feishu's `standard_icon` set is monochrome line art, so every token rendered
  as a grey block — that is why the five vault rows were indistinguishable. The
  emoji sits *outside* the bold run (`⚙️ **进程管理**`), because Feishu drops a
  bold span that mixes an emoji into it. `standard_icon` remains only where it
  is not a tool row (panel collapse arrow, context clock).
- Names `process_manage` 「进程管理」, not 「流程管理」: its Hermes schema
  (`tools/process_registry.py`) manages `terminal(background=true)` OS
  processes — poll / wait / kill / log / write / submit / close / handoff — so
  "workflow" would misdescribe it.
- MCP tools (`mcp__server__tool`) intentionally keep their English
  lowercased rendering; not translated.

### Tests

- Adds `TestHermesCoreToolNamesHaveChineseLabels` (every newly covered name must
  resolve to a non-English label and a non-default emoji),
  `TestBrowserVaultToolsAreDistinct`, `TestNewToolEmojiAreUniqueWithinFamily`,
  `TestNewToolLabelsAndEmoji`, `TestProcessManageIsProcessesNotFlows` and
  `TestToolStepTitleRendersEmoji` (emoji leads the text, sits outside the bold
  run, and no `standard_icon` slot survives).

### Fixed — an interrupted `hermes plugins install` could leave a second copy that outranked the real one

- `hermes plugins install` clones into a `.install-*` temp directory under
  `plugins/` and relies on `__exit__` to remove it; a SIGTERM (for example an
  automation script's `timeout`) skips that cleanup. The installer's security
  scan flags a HIGH `exfiltration` finding in this repo's own `AGENT_GUIDE.md`
  and then blocks on `Install anyway? [y/N]`, so a script without a TTY and
  without `--force` hangs until it is killed.
- Plugin discovery walks `sorted(path.iterdir())` and `.` sorts before any
  letter, so the leftover loads as a *second* plugin ahead of the real
  directory. The process-wide dedup markers make whichever copy installs first
  win, silently; both copies still print `GatewayRunner=✓`.
- Fix on the caller side: `hermes plugins install --force --enable "$URL"
  </dev/null` with a longer timeout. Detection and quarantine steps are in
  `AGENT_GUIDE.md` under the multiplex section, plus a troubleshooting row.

## v1.6.28 (2026-09-19, personal fork)

### Fixed — the speed field losing the live turn's usage to a background fork

- Keeps detached agent forks from taking over the live turn's usage ownership.
  Hermes runs its background memory/skill review (and `/btw` side questions) on a
  throwaway fork that deliberately shares the parent's session id and inherits
  the parent's ContextVars, so the operator's still-open turn also looks
  "current" to it. Wrapping that fork's callbacks overwrote the completion
  hook's agent reference with an agent that had not answered anything yet, so a
  turn whose own timing window was perfectly measurable was reported as
  `speed hidden … reason=no_visible_output visible=0` and lost its `speed`
  field. The review fork now keeps its own callbacks: `_persist_disabled` is
  Hermes' own marker for exactly these detach-from-persistence forks.
- Also stops the review fork's own reasoning/answer text from being routed into
  the operator's card while it is still streaming, which the same overwrite
  allowed.

### Tests

- Adds a regression test that starts a review-style fork mid-answer and asserts
  the live turn keeps both its usage ownership and its rendered `speed` figure.
  It fails against the previous build with the production symptom.

## v1.6.27 (2026-09-19, personal fork)

### Tests

- Extends the two-copy isolation suite to every remaining patch target
  (`conversation_loop`, `cron._deliver_result`, the `create_adapter` hook and
  the direct `AIAgent` patch).
- Covers both sides of the profile secret-scope reset: a failing host reset must
  not escape into `enabled`, and the normal path must restore the prior scope.

## v1.6.26 (2026-09-19, personal fork)

### Fixed — follow-up hardening after the multiplex fix

- `/aowen config reload` reaches the per-profile controllers again. A controller
  holds a `Config(profile_home)` instance, and those deliberately bypass the
  shared singleton, so a reload that only cleared the singleton left every
  controller reading the values it parsed at first access — `enabled: false` or
  `gateway_cards: false` needed a gateway restart to take effect. `Config.reload()`
  now bumps a class-level generation that every instance observes on its next
  read.
- Binds the gateway-card config lookup to the current profile. The unbound
  singleton cached whichever home resolved first, so `gateway_cards: false` in
  one profile silently downgraded every other profile's cards to plain text.
- Treats a class marker as proof only for the class that carries it. The check
  used `getattr`, which also sees an inherited marker, so a subclass of a patched
  `FeishuAdapter` (host variant, test double) was reported as already patched and
  never wrapped — cards degraded to plain text while the log still printed
  success.
- Ties the cached "enabled" verdict to the config generation, so a reload can
  still turn cards off for a profile that had them on.
- Creates the cross-copy lock atomically. Two plugin copies importing
  concurrently hold separate module locks, so the previous check-then-set could
  build two locks and defeat the mutual exclusion it exists to provide.
- Drops the duplicated, half-written test blocks appended during the multiplex
  work; the surviving copies are the ones that assert behaviour, plus new
  coverage for the reload contract, the profile-bound card config, and the
  inherited-marker case above.
- No longer lets a host secret-scope API change take the hot path down. The
  profile scope's reset is the only cleanup step in that block, so a signature
  change there would have propagated out of `enabled` and failed every hook
  that reads it; the reset now logs a warning and leaves the scope for the next
  caller's own check.

### Tests

- Extends the two-copy isolation suite to every remaining patch target
  (`conversation_loop`, `cron._deliver_result`, the `create_adapter` hook and
  the direct `AIAgent` patch), so the "second copy adopts, never re-wraps" rule
  is covered wherever it applies rather than only on the two targets that
  visibly stacked in production.
- Covers both sides of the scope reset: a failing host reset must not escape,
  and the normal path must still restore the previous scope.

## v1.6.25 (2026-09-19, personal fork)

### Fixed — correct cards on a multiplexed (multi-profile) gateway

- Isolates every streaming controller by Hermes profile. The gateway's
  `gateway.multiplex_profiles` mode (now the official default) serves several
  profiles from one process, and loads a directory plugin once **per served
  profile**. Each copy now resolves its own home, config and credentials, so a
  reply for one profile can no longer be sent with another profile's bot — that
  mismatch was rejected by Feishu with `230002 Bot/User can NOT be out of the
  chat` and silently downgraded the card to plain text.
- Reads credentials through the host's profile secret scope instead of
  `os.environ`. Under multiplexing the process environment holds only the launch
  profile's values, and the scope is fail-closed, so a profile can never borrow
  another's app id or secret. Older Hermes builds without that API still read
  the environment.
- Deduplicates the runtime patches across those per-profile copies. The
  "already patched" markers now live on the shared host objects — the
  `GatewayRunner` / `FeishuAdapter` classes and the wrapped callables themselves
  — instead of in per-copy module globals. Previously each copy wrapped the same
  class again, so one inbound message was processed once per copy: it produced
  repeated session creation and repeated card creation, all racing on the same
  chat.
- Shares process-wide plugin state (the per-home controller registry) through a
  host module, so two copies cannot each build a controller for the same profile
  and create two cards for one message.
- Parses the gateway's nested platform credentials
  (`gateway.platforms.<feishu|lark>.extra`) in addition to the flat `feishu:` /
  `lark:` shape, and honours `domain: lark` for the Larksuite base URL.

## v1.6.24 (2026-09-18, personal fork)

### Fixed — speed field disappearing on burst answers

- Falls back to the model call's own span (first upstream activity → last
  visible chunk) when a provider flushes a short answer in one burst. That
  collapsed the visible-chunk span below the 0.3 s measurement floor and hid
  the `speed` footer field on exactly those turns, so the field appeared on
  some replies and vanished on others.
- Keeps the tighter visible-chunk span whenever it is measurable, so answers
  that really stream token by token are reported unchanged. The fallback span
  includes that call's own reasoning/prefill time, so it reports the call's
  upstream throughput and reads low when long reasoning precedes a burst.
- Resets the whole measurement window at every model-call boundary — a tool
  start, and the context compaction that runs between two calls. Neither the
  visible-chunk span nor the fallback span can then include the tool or the
  compaction, and the next call cannot reuse the earlier call's anchor; a
  compaction used to leave the visible-chunk span open across it and report a
  badly deflated figure.
- Keeps the same window in step with interim-assistant answers (verify-on-stop,
  length continuations): they previously reached the card body without any
  timing, so a call whose start was known still lost the speed field. An answer
  delivered by a single interim callback with nothing streamed has no measurable
  window and stays hidden, as before.
- Logs one `HLS: speed hidden …` line with `reason`, `delta_span`, and
  `call_span` when the field cannot be shown, making "sometimes shown,
  sometimes not" diagnosable from the agent log.

## v1.6.23 (2026-09-15, personal fork)

### Fixed — output speed for separately counted reasoning tokens

- Preserves the provider's original usage totals before Hermes normalizes them.
  Gemini-compatible endpoints that count completion and reasoning separately no
  longer lose their speed field or have visible output tokens undercounted.
- Keeps standard OpenAI reasoning subtraction and the minimum streaming window.
  Missing usage or a failed final request cannot reuse an earlier call's counts.
- Refreshes usage ownership for cached agents without wrapping callbacks twice.

## v1.6.22 (2026-09-14, personal fork)

### Added — OpenViking automatic recall status

- Shows `📖 OpenViking · 自动检索记忆` while Hermes prepares OpenViking memory
  before a model turn, including its session-aware `/api/v1/search/search` request.
- Restores the waiting status when retrieval returns, fails, or times out;
  later model/tool activity takes precedence. Automatic recall does not add a
  model tool step.

### Changed — distinguish memory providers in tool status

- Adds Hermes, Hindsight, and OpenViking names to memory-tool labels in both
  languages, including session search, deletion, and resource import.
- Uses distinct status-row emoji for built-in memory (🧠), all Hindsight
  operations (👁️), and all OpenViking operations (📖).

## v1.6.21 (2026-09-14, personal fork)

### Added — OpenViking tool labels and icons

- Maps Hermes' six `viking_*` tools to localized names and operation icons in
  the tool panel, with memory, deletion, and resource-import emoji in the status row.
- Documents each operation, including OpenViking's memory extraction behavior.

## v1.6.20 (2026-09-06, personal fork)

### Reverted — incoming image analysis status

- Withdraws the image-preprocessing status changes introduced in v1.6.19.
- Restores the runtime code and existing tests from v1.6.18, including its
  earlier streaming recovery, race handling, and context-compression fixes.

## v1.6.19 (2026-09-06, personal fork)

### Fixed — show incoming image analysis before the main model runs

- Shows `正在识别图片...` / `Analyzing images...` while Hermes preprocesses
  incoming images with its vision model, including images sent without text.
- Restores the waiting status when analysis returns, fails, or is cancelled;
  later model activity keeps its current status.
- Supports both CardKit and device-sized interactive cards, including analysis
  that starts before the placeholder finishes being created.

## v1.6.18 (2026-08-23, personal fork)

### Fixed — preserve streaming updates across races and recovery boundaries

- Makes CardKit answer snapshots and dirty-flag consumption atomic, so tokens
  arriving while an update is in flight remain queued for the next flush.
- Restores pending answer content after cancellation, API failures, and
  unexpected transport errors instead of leaving the card stale.
- Seals a completing session against late answer callbacks and prevents a late
  callback from opening an unnecessary continuation card.
- Binds continuation flush controllers to their target event loop and adds
  regression coverage for worker-thread scheduling, final-seal races, and
  failed stream retries.

## v1.6.17 (2026-08-23, personal fork)

### Fixed — context compression is shown explicitly

- Changes the initial card status from `等待上游模型响应` to `上下文压缩中...`
  while Hermes compacts a large conversation before the next model request.
- Restores the previous waiting/model/tool state when compression completes,
  fails, is cancelled, or times out, while ensuring a first model stream event
  wins the race and remains `模型思考中...`.
- Preserves Hermes' original activity callback and covers worker-thread
  updates, card-creation races, CardKit fallback rendering, and all known
  compression terminal events.

## v1.6.16 (2026-08-23, personal fork)

### Fixed — thinking starts at the upstream stream boundary

- Stops treating Hermes' `on_first_delta` callback as a wire-level signal: in
  Hermes 0.20.x it fires only after a chunk contains renderable text,
  reasoning, or a complete tool name, which can be almost the end of a model
  call.
- Moves the card from `等待上游模型响应` to `模型思考中...` when the upstream
  produces its first raw stream chunk, with the existing visible delta
  callbacks retained as provider-compatible fallbacks. Opening HTTP headers
  alone does not change the state.
- Preserves Hermes' activity callback and adds idempotence and regression
  coverage for raw-chunk and waiting-state events.

## v1.6.15 (2026-08-23, personal fork)

### Fixed — first upstream activity now reaches the card immediately

- Schedules CardKit updates thread-safely when Hermes delivers stream events
  from its worker pool, so the card changes from `等待上游模型响应` to
  `模型思考中...` as soon as the first model activity arrives.
- Recognizes Hermes' first-delta boundary, tool-call generation, and
  `reasoning.available` events even when they contain no visible answer text.
- Resolves cached-agent callbacks against the current session before stale
  thread-local fallback context, preventing a new turn from updating the
  previous card.
- Adds regression coverage for the worker-thread wake-up and interactive-card
  status transition.

## v1.6.14 (2026-08-23, personal fork)

### Fixed — recovery turns keep the unified streaming card

- Assigns an internal message id when Hermes reconstructs a gateway recovery
  event without a platform message id, so the turn stays on the streaming-card
  path instead of producing standalone gateway cards.
- Sends recovery cards directly to the chat when there is no valid reply
  anchor, while preserving CardKit updates for the rest of the turn.
- Registers the active turn context by Hermes session id so worker-thread
  callbacks still reach the answer, reasoning, and tool-card handlers when
  `ContextVar` propagation is unavailable.

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
