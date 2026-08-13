# hermes-lark-streaming 安装与维护指南

> 高信息密度参考文档，供 Hermes Agent 或其他自动化 Agent 解析。
> 最后更新：2026-08-13（v1.6.2，个人复刻版）

## 项目概览

| 项目 | 值 |
|------|-----|
| 名称 | hermes-lark-streaming |
| 作用 | Hermes Agent 的飞书/Lark CardKit v2.0 流式卡片插件 |
| 许可证 | MIT |
| Python | >=3.11 |
| 依赖 | lark-oapi>=1.4.0, PyYAML>=6.0 |
| 插件类型 | standalone |
| 当前仓库 | https://github.com/ati121/hermes-lark-streaming |
| 复刻来源 | https://github.com/Aowen-Nowor/hermes-lark-streaming |
| 更上游 | https://github.com/Cheerwhy/hermes-lark-streaming |

本仓库按个人使用需求维护，仅供自用。相对 Aowen-Nowor 上游，当前最重要的
本地修改是卡片正文、推理、工具、提示和页脚支持 PC/手机端独立字号。

## 安装

### Hermes CLI（推荐）

```bash
# HTTPS
hermes plugins install https://github.com/ati121/hermes-lark-streaming.git

# SSH
hermes plugins install git@github.com:ati121/hermes-lark-streaming.git
```

提示时输入 `Y` 启用插件，然后重启网关：

```bash
hermes gateway restart
```

插件会读取 `HERMES_HOME` 作为安装和配置根目录，未设置时使用
`~/.hermes`。

### 本地目录

```bash
git clone https://github.com/ati121/hermes-lark-streaming.git
cd hermes-lark-streaming
hermes plugins add .
hermes gateway restart
```

### 让 Agent 自动安装

将本文件 URL 交给 Hermes Agent：

```text
https://raw.githubusercontent.com/ati121/hermes-lark-streaming/main/docs/AGENT_GUIDE.md
```

## 更新

```bash
hermes plugins update hermes-lark-streaming
hermes gateway restart
```

手动更新已安装目录时，使用当前仓库的 `main` 分支：

```bash
cd "$HERMES_HOME/plugins/hermes-lark-streaming"
git pull origin main
hermes plugins reload hermes-lark-streaming
hermes gateway restart
```

如果当前目录来自重写前的历史，且工作区没有本地改动，可以直接重新安装当前仓库，
避免把已清理的旧历史带回来。

## 卸载

```bash
# 插件代码还在时清理自动注入的配置
HERMES_PYTHON=$(python3 "$HERMES_HOME/plugins/hermes-lark-streaming/__main__.py" python)
$HERMES_PYTHON "$HERMES_HOME/plugins/hermes-lark-streaming/__main__.py" cleanup

hermes plugins uninstall hermes-lark-streaming
hermes gateway restart
```

## 凭据配置

插件复用 Hermes 已有的飞书/Lark 凭据，不要在仓库文件中写入真实值。

```bash
# $HERMES_HOME/.env
FEISHU_APP_ID=your_app_id
FEISHU_APP_SECRET=your_app_secret
FEISHU_DOMAIN=feishu          # 国内版；国际版使用 lark
```

也可以使用环境变量，或在 `$HERMES_HOME/config.yaml` 的 `feishu`/`lark` 节中配置。
优先级为：环境变量 > `.env` > 配置文件。

## 配置项

配置文件：`$HERMES_HOME/config.yaml`，默认值见下表。

| 配置键 | 默认值 | 范围/类型 | 说明 |
|--------|---------|-----------|------|
| `panel_expanded` | `false` | bool | 完成态统一面板是否展开 |
| `streaming_panel_expanded` | `false` | bool | 流式态统一面板是否展开 |
| `print_strategy` | `delay` | `fast`/`delay` | 打字机效果策略 |
| `print_step` | `4` | 1–10 | 每次渲染字符数，需飞书 7.23+ |
| `flush_interval_ms` | `200` | 70–2000 | 插件发送间隔（毫秒） |
| `card_ttl_sec` | `600` | >0 | 卡片存活检测超时（秒） |
| `max_tool_steps` | `20` | 1–100 | 面板显示的工具步骤上限 |
| `max_reasoning_rounds` | `20` | 1–100 | 面板显示的推理轮次上限 |
| `footer.show_label` | `false` | bool | 是否显示页脚字段标签 |
| `footer.fields` | status/elapsed/model/cost/compression_exhausted | array | 页脚字段排列 |
| `text_sizes` | `{}` | mapping | PC/手机端设备差异字号（本地新增） |

基础示例：

```yaml
hermes_lark_streaming:
  panel_expanded: false
  streaming_panel_expanded: false
  print_strategy: delay
  print_step: 4
  flush_interval_ms: 200
  card_ttl_sec: 600
  max_tool_steps: 20
  max_reasoning_rounds: 20
  footer:
    show_label: false
    fields:
      - [status, elapsed, model, cost, compression_exhausted]

display:
  show_reasoning: true
```

### `text_sizes`（PC/手机端独立字号）

完整形式如下：

```yaml
hermes_lark_streaming:
  text_sizes:
    body: {default: normal, pc: normal, mobile: large}
    reasoning: {default: small, pc: small, mobile: normal}
    tool: {default: x-small, pc: x-small, mobile: small}
    notice: {default: x-small, pc: x-small, mobile: small}
    footer: {default: x-small, pc: x-small, mobile: small}
```

支持的角色为 `body`、`reasoning`、`tool`、`notice`、`footer`；设备字段为
`default`、`pc`、`mobile`。每个角色也可直接写一个字符串，例如 `body: large`。

支持的字号值：`heading-0`、`heading-1`、`heading-2`、`heading-3`、`heading-4`、
`heading`、`normal`、`notation`、`xxxx-large`、`xxx-large`、`xx-large`、
`x-large`、`large`、`medium`、`small`、`x-small`。

缺少 `pc`/`mobile` 时继承 `default`；缺少 `default` 时使用角色默认值：
`body=normal`、`reasoning=small`、`tool/notice/footer=x-small`。

未配置 `text_sizes` 时，Card JSON 必须保持原样。配置启用后，飞书使用普通
interactive IM 卡片的整卡更新路径来可靠应用设备字号别名。字号配置在卡片创建时
快照，同一张卡片整个流式生命周期固定不变；`/aowen config reload` 只影响新卡片。

## `/aowen` 命令

| 命令 | 说明 |
|------|------|
| `/aowen help` 或 `/aowen` | 显示命令列表 |
| `/aowen status` | 显示插件状态与当前配置 |
| `/aowen monitor` | 显示卡片/API/错误统计 |
| `/aowen monitor reset` | 重置监控计数 |
| `/aowen config reload` | 重新读取配置并作用于后续卡片 |

命令由插件直接处理，不会发送给 Hermes AI。AI 回复进行中发送命令时，会收到提示卡，
不会把命令误交给模型。

## 提供的钩子

`pre_gateway_dispatch`、`on_feishu_normalize`、`on_message_started`、
`on_message_completed`、`on_message_aborted`、`on_message_interrupted`、
`on_answer_delta`、`on_thinking_delta`、`on_reasoning_delta`、`on_tool_updated`、
`on_background_review_message`、`on_cron_deliver`。

## 故障排查

| 现象 | 检查项 |
|------|--------|
| 没有卡片 | `hermes plugins list` 是否启用、凭据是否存在、日志是否有 `HLS:` |
| 元素超限（300305） | 降低 `max_tool_steps`/`max_reasoning_rounds`；代码有最终安全网 |
| Schema 错误（300315） | 检查 CardKit v2 卡片结构和字号值是否合法 |
| 流式卡片卡住 | 增大 `card_ttl_sec`，确认卡片未被删除/撤回 |
| 字号未变化 | 确认 `text_sizes` 缩进、角色/字号合法；旧卡片不会被新配置改变 |

## 验证安装

```bash
hermes plugins list
grep hermes_lark_streaming "$HERMES_HOME/logs/agent.log"
HERMES_PYTHON=$(python3 "$HERMES_HOME/plugins/hermes-lark-streaming/__main__.py" python)
$HERMES_PYTHON "$HERMES_HOME/plugins/hermes-lark-streaming/__main__.py" status
$HERMES_PYTHON "$HERMES_HOME/plugins/hermes-lark-streaming/__main__.py" verify
$HERMES_PYTHON "$HERMES_HOME/plugins/hermes-lark-streaming/__main__.py" doctor
```

## 开发与测试

```bash
git clone https://github.com/ati121/hermes-lark-streaming.git
cd hermes-lark-streaming
python -m pip install -e ".[dev]"
python -m pytest tests/
```

需要更新上游时，先阅读 `docs/MAINTENANCE_CONTEXT.md`，不要直接覆盖带有本地字号修改的
代码文件。

## 相关文档

- [维护上下文](MAINTENANCE_CONTEXT.md)
- [项目技能说明](SKILL.md)
- [更新日志](CHANGELOG.md)
- [Issue 模板](ISSUES_TEMPLATE.md)

## 相关链接

- 当前仓库：https://github.com/ati121/hermes-lark-streaming
- 复刻来源：https://github.com/Aowen-Nowor/hermes-lark-streaming
- 更上游：https://github.com/Cheerwhy/hermes-lark-streaming
