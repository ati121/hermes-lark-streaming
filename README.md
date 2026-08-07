<h1 align="center">hermes-lark-streaming</h1>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-4caf50.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-1.6.1-ff9800.svg" alt="Version">
  <img src="https://img.shields.io/badge/use-personal-blueviolet.svg" alt="Personal use">
</p>

<p align="center">
English | <a href="README.zh-CN.md">中文版</a>
</p>

Fork of [Aowen-Nowor/hermes-lark-streaming](https://github.com/Aowen-Nowor/hermes-lark-streaming),
modified for personal use. The Aowen-Nowor project is based on
[Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming)
v0.7.0 with extensive refactoring.

This fork keeps all upstream functionality and additionally adds independent card
text sizes for PC and mobile. It is maintained for personal use and may diverge
from the upstream project.

## Local modification: PC/mobile text sizes

`hermes_lark_streaming.text_sizes` is opt-in. When omitted, the original Card JSON
and transport behavior are preserved.

```yaml
hermes_lark_streaming:
  text_sizes:
    body:      {default: normal, pc: normal,  mobile: large}
    reasoning: {default: small,  pc: small,   mobile: normal}
    tool:      {default: x-small, pc: x-small, mobile: small}
    notice:    {default: x-small, pc: x-small, mobile: small}
    footer:    {default: x-small, pc: x-small, mobile: small}
```

Roles: `body`, `reasoning`, `tool`, `notice`, `footer`. Device keys: `default`,
`pc`, `mobile`. A role may also use a single size, e.g. `body: large`.

Supported sizes:

```text
heading-0, heading-1, heading-2, heading-3, heading-4, heading,
normal, notation, xxxx-large, xxx-large, xx-large, x-large,
large, medium, small, x-small
```

Missing `pc`/`mobile` inherit `default`; a missing `default` falls back to
`normal` for `body`, `small` for `reasoning`, and `x-small` for `tool`, `notice`,
`footer`. Enabling `text_sizes` switches that card to the full-card interactive
update path. Sizes are snapshotted when a card is created, so one card keeps the
same sizes for its whole lifecycle.

## Installation and usage

See [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md) for installation, configuration,
commands, and troubleshooting. Other references:

- [Maintenance context](docs/MAINTENANCE_CONTEXT.md) — upstream baseline, local
  changes, invariants, and future sync workflow.
- [Project skill](docs/SKILL.md) — architecture and code map.
- [Changelog](docs/CHANGELOG.md)

MIT licensed. This fork is maintained for personal use.