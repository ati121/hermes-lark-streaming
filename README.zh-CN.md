<h1 align="center">hermes-lark-streaming</h1>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/License-MIT-4caf50.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-3.11+-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/version-1.6.0-ff9800.svg" alt="Version">
  <img src="https://img.shields.io/badge/用途-个人自用-blueviolet.svg" alt="个人自用">
</p>

<p align="center">
<a href="README.md">English</a> | 中文版
</p>

复刻自 [Aowen-Nowor/hermes-lark-streaming](https://github.com/Aowen-Nowor/hermes-lark-streaming)，
按个人使用需求修改，仅供自用。Aowen-Nowor 的版本基于
[Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming)
v0.7.0，并进行了大量重构。

本仓库保留上游全部功能，额外增加了飞书卡片 PC 端与手机端独立字号配置。后续修改
可能与上游产生差异。

## 本地修改：PC/手机端独立字号

`hermes_lark_streaming.text_sizes` 是可选配置。完全不配置时，会保持上游原有
Card JSON 和传输行为不变。

```yaml
hermes_lark_streaming:
  text_sizes:
    body:      {default: normal, pc: normal,  mobile: large}
    reasoning: {default: small,  pc: small,   mobile: normal}
    tool:      {default: x-small, pc: x-small, mobile: small}
    notice:    {default: x-small, pc: x-small, mobile: small}
    footer:    {default: x-small, pc: x-small, mobile: small}
```

支持的内容角色：`body`（回答正文）、`reasoning`（推理）、`tool`（工具）、
`notice`（提示/错误）、`footer`（页脚）。支持的设备字段：`default`、`pc`、
`mobile`。也可以写成 `body: large`，表示所有设备使用同一字号。

支持的字号值：

```text
heading-0, heading-1, heading-2, heading-3, heading-4, heading,
normal, notation, xxxx-large, xxx-large, xx-large, x-large,
large, medium, small, x-small
```

未填写的 `pc` 或 `mobile` 会继承 `default`；连 `default` 也未填写时，
`body` 使用 `normal`，`reasoning` 使用 `small`，`tool`、`notice`、`footer`
使用 `x-small`。启用 `text_sizes` 后，该卡片会切换到整卡交互更新路径。每张卡片
创建时会固定保存当时的字号配置，整个流式生命周期不会中途变化。

## 安装与使用

安装、配置、命令和故障排查请参见 [docs/AGENT_GUIDE.md](docs/AGENT_GUIDE.md)。
其他参考资料：

- [维护上下文](docs/MAINTENANCE_CONTEXT.md)：上游基线、本地修改、不变量和以后同步上游的流程。
- [项目技能说明](docs/SKILL.md)：架构和代码地图。
- [更新日志](docs/CHANGELOG.md)

项目使用 MIT 协议。本复刻版本仅供个人自用。