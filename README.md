<h1 align="center">hermes-lark-streaming</h1>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-4caf50.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-1.6.0-ff9800.svg" alt="Version">
  <img src="https://img.shields.io/badge/use-personal-blueviolet.svg" alt="Personal use">
</p>

<p align="center">
English | <a href="README.zh-CN.md">中文版</a>
</p>

Feishu/Lark CardKit v2.0 streaming cards for Hermes Agent, with real-time AI
responses, a typewriter effect, a unified collapsible reasoning/tool panel,
completion statistics, and device-specific text sizes.

> Personal-use fork of
> [Aowen-Nowor/hermes-lark-streaming](https://github.com/Aowen-Nowor/hermes-lark-streaming),
> customized for my own deployment.
>
> The Aowen-Nowor project is based on
> [Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming)
> v0.7.0 and includes extensive refactoring and optimizations.
>
> This fork additionally supports independent card text sizes for PC and mobile.
> It is intended for personal use and may diverge from the upstream project.

---

## Effect preview

<table align="center">
  <tr>
    <td><img src="assets/screenshots/img1.png" width="200px" /></td>
    <td><img src="assets/screenshots/img2.png" width="200px" /></td>
    <td><img src="assets/screenshots/img3.png" width="200px" /></td>
    <td><img src="assets/screenshots/img4.png" width="200px" /></td>
  </tr>
</table>

## Quick start

### Prerequisites

- [Hermes Agent](https://github.com/NousResearch/hermes-agent), running with the
  Feishu/Lark platform configured
- Hermes CLI with the plugin system available (`hermes plugins`)
- Python 3.11 or later

### Installation

You can give Hermes Agent the following instruction:

```text
Install the Feishu streaming-card plugin by following:
https://raw.githubusercontent.com/ati121/hermes-lark-streaming/main/docs/AGENT_GUIDE.md
```

Or install it directly:

```bash
# HTTPS
hermes plugins install https://github.com/ati121/hermes-lark-streaming.git

# SSH
hermes plugins install git@github.com:ati121/hermes-lark-streaming.git
```

Enter `Y` when prompted to enable the plugin, then restart the gateway:

```bash
hermes gateway restart
```

The plugin reads `HERMES_HOME` to locate the Hermes data directory and falls
back to `~/.hermes` when the variable is not set.

### Update

```bash
hermes plugins update hermes-lark-streaming
hermes gateway restart
```

### Verify installation

```bash
hermes plugins list
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py status
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py verify
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py doctor
```

If the card effect does not appear, confirm that the plugin is enabled, there
are no stale `*.bak` plugin directories, and the Feishu/Lark credentials are
available to Hermes.

### Uninstallation

```bash
# Clean the injected config while the plugin code is still installed.
HERMES_PYTHON=$(python3 ~/.hermes/plugins/hermes-lark-streaming/__main__.py python)
$HERMES_PYTHON ~/.hermes/plugins/hermes-lark-streaming/__main__.py cleanup

hermes plugins uninstall hermes-lark-streaming
hermes gateway restart
```

## Configuration

Settings belong under `hermes_lark_streaming:` in
`$HERMES_HOME/config.yaml` (normally `~/.hermes/config.yaml`).

```yaml
hermes_lark_streaming:
  panel_expanded: false
  streaming_panel_expanded: false
  print_strategy: delay            # fast or delay
  print_step: 4                    # 1-10; Feishu 7.23+
  flush_interval_ms: 200           # 70-2000 ms
  card_ttl_sec: 600
  max_tool_steps: 20               # 1-100
  max_reasoning_rounds: 20         # 1-100

  footer:
    show_label: false
    fields:
      - [status, elapsed, model, cost, compression_exhausted]
```

### PC/mobile text sizes added by this fork

`text_sizes` is opt-in. When it is omitted, the original card JSON and
transport behavior are preserved.

```yaml
hermes_lark_streaming:
  text_sizes:
    body:
      default: normal
      pc: normal
      mobile: large
    reasoning:
      default: small
      pc: small
      mobile: normal
    tool:
      default: x-small
      pc: x-small
      mobile: small
    notice:
      default: x-small
      pc: x-small
      mobile: small
    footer:
      default: x-small
      pc: x-small
      mobile: small
```

Supported roles are `body`, `reasoning`, `tool`, `notice`, and `footer`.
Supported device keys are `default`, `pc`, and `mobile`. A role can also use a
single size, such as `body: large`, to apply the same size everywhere.

Supported sizes are:

```text
heading-0, heading-1, heading-2, heading-3, heading-4, heading,
normal, notation, xxxx-large, xxx-large, xx-large, x-large,
large, medium, small, x-small
```

Missing `pc` or `mobile` values inherit `default`. If `default` is missing, the
role fallback is `normal` for `body`, `small` for `reasoning`, and `x-small`
for `tool`, `notice`, and `footer`.

Feishu currently applies per-device Markdown aliases reliably through ordinary
interactive IM cards. Therefore, enabling `text_sizes` switches that card to
the full-card interactive update path. The configuration is snapshotted when a
card is created, so one card keeps the same sizes for its complete lifecycle;
reloading configuration affects newly created cards.

After editing the configuration, send `/aowen config reload` in Feishu or
restart the gateway.

### Reasoning panel display

This is a Hermes global display setting, not part of
`hermes_lark_streaming:`:

```yaml
display:
  show_reasoning: true
```

### Card element limit

Feishu Card 2.0 limits a card to 200 tagged elements. The plugin folds early
reasoning/tool items and applies a final card-level safety check before sealing
the card. `max_tool_steps` and `max_reasoning_rounds` control how much remains
visible in the unified panel; the answer and footer are not trimmed.

### `/aowen` commands

| Command | Description |
|---|---|
| `/aowen help` | Show available commands |
| `/aowen status` | Show plugin status and current configuration |
| `/aowen monitor` | Show card/API/error metrics |
| `/aowen monitor reset` | Reset monitoring counters |
| `/aowen config reload` | Reload `config.yaml` without restarting Hermes |

### Feishu/Lark credentials

The plugin reuses the credentials already configured for Hermes:

```bash
FEISHU_APP_ID=cli_xxxxxx
FEISHU_APP_SECRET=xxxxxx
FEISHU_DOMAIN=feishu          # feishu for China, lark for international
```

Do not commit real credentials, chat identifiers, deployment paths, webhook
URLs, tokens, or production logs to this repository.

## Maintenance and development

- [Maintenance context](docs/MAINTENANCE_CONTEXT.md) records the upstream
  baseline, local changes, invariants, test commands, and future sync workflow.
- [Agent guide](docs/AGENT_GUIDE.md) is the installation/configuration reference
  for automated agents.
- [Project skill](docs/SKILL.md) describes the architecture and code map.
- [Changelog](docs/CHANGELOG.md) records public changes without private
  deployment information.

Run the default regression suite before publishing changes:

```bash
python -m pytest tests/
```

## Issues

Please use the [issue template](docs/ISSUES_TEMPLATE.md). Include the Hermes
version, plugin version, a redacted log excerpt, and a minimal reproduction;
never include credentials or private deployment details.

## Acknowledgments

- [Aowen-Nowor/hermes-lark-streaming](https://github.com/Aowen-Nowor/hermes-lark-streaming)
- [Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming)

MIT licensed. This fork is maintained for personal use.
