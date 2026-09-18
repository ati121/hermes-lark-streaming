# hermes-lark-streaming 安装与维护指南

> 高信息密度参考文档，供 Hermes Agent 或其他自动化 Agent 解析。
> 最后更新：2026-09-19（v1.6.26，个人复刻版）

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

插件通过 `hermes_constants.get_hermes_home()` 解析配置根目录（未安装该宿主
API 时依次回退 `HERMES_HOME` 环境变量、`~/.hermes`）。多 Profile 网关
（multiplex）下每次调用都返回当前 Profile 的 home，因此同一个进程服务多个
Profile 时不会串配置或串凭据。

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
| `footer.fields` | status/elapsed/speed/model/cost/compression_exhausted | array | 页脚字段排列 |
| `text_sizes` | `{}` | mapping | PC/手机端设备差异字号（本地新增） |

可选的页脚字段名：`status`、`elapsed`、`speed`、`model`、`tokens`、`context`、
`cache`、`cost`、`api_calls`、`history_offset`、`compression_exhausted`。未知字段名
会被静默跳过。`speed` 默认开启，显示最后一次模型调用的可见输出速度（如 `50 t/s`）：
分子为该调用的可见输出 token，分母默认是工具调用之后最终可见正文的首个到末个流式块
间隔。部分上游会把一段短答案整段一次性下发，这个间隔会塌缩到测量下限以下；此时改用
同一次模型调用内「首个上游活动（推理、工具名或首个可见块）→ 末个可见块」的间隔兜底，
所以短答案不再让速度消失。兜底窗口计入该次调用先前的推理/预填时间，因此它衡量的是这次调用
的上游窗口吞吐：若一次调用先有长推理再整段下发答案，数值会明显偏低，可见正文间隔可测量时
始终优先使用它。两个窗口都在每次模型调用边界被清零——工具开始执行，或两次调用之间的上下文
压缩——因此既不会把工具耗时、压缩耗时算进去，也不会跨调用拼接相邻两次调用的正文。
Hermes 经 interim 回调交付答案（verify_on_stop、截断续写等）时正文与记时同步更新，该次调用起点
已知就能算出速度；若整段答案只经一次 interim 回调下发（非流式重放），没有任何窗口可测，
仍按原行为隐藏。标准 OpenAI 用量中的
输出数包含 reasoning token，需要扣除；部分 Gemini 兼容接口的输出数已排除推理，插件根据
接口原始的输入、输出、推理和总数识别此口径，避免重复扣除。原始计数在 Hermes 归一化前
单独保存，不影响其用量和费用统计。
无法取得可靠 usage、没有可见输出，或两个窗口都短于 0.3 秒时不显示。完成时若速度为空，
日志会打印一行 `HLS: speed hidden …`，含 `reason`（`no_usage`/`no_visible_output`/
`window_too_short`）、`delta_span` 与 `call_span` 便于排查。不想要可以从 `fields` 里去掉。

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
      - [status, elapsed, speed, model, cost, compression_exhausted]

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
`on_memory_prefetch_updated`、`on_background_review_message`、`on_cron_deliver`。

## 记忆工具显示

状态行按记忆来源使用不同图标，所有文案都带上来源名称。Hindsight 的四个工具映射
统一用 👁️，OpenViking 的六个工具统一用 📖；Hermes 内置记忆用 🧠，会话检索用 🔎。
工具面板同步使用带来源的中英文名称。

Hermes 在模型调用前自动预取 OpenViking 记忆时，卡片显示
`📖 OpenViking · 自动检索记忆`，结束后恢复等待模型的提示。此过程包含
`POST /api/v1/search/search`，以及检索降级、读取记忆内容等自动预取工作。
插件包装当前 agent 的 `MemoryManager._prefetch_provider` 等待边界，因此请求失败或
达到 Hermes 的等待超时后也会结束提示；自动预取不计入模型的工具调用记录。
正常的模型输出、工具调用和上下文压缩优先于此准备状态。

| 工具 | 状态行 | 用途 |
|------|--------|------|
| `memory` | 🧠 Hermes · 内置记忆 | 新增、替换或删除内置记忆 |
| `session_search` | 🔎 Hermes · 会话检索 | 检索历史会话 |
| `hindsight_retain` | 👁️ Hindsight · 记忆写入 | 写入长期记忆 |
| `hindsight_recall` | 👁️ Hindsight · 记忆回溯 | 检索历史记忆 |
| `hindsight_reflect` | 👁️ Hindsight · 记忆推演 | 综合已有记忆进行推演 |
| `hindsight_operation` | 👁️ Hindsight · 记忆操作 | 其他记忆操作的显示映射 |
| `viking_search` | 📖 OpenViking · 检索记忆 | 语义检索记忆库，支持 auto/fast/deep 模式 |
| `viking_read` | 📖 OpenViking · 读取记忆 | 按摘要、概览或全文读取指定内容 |
| `viking_browse` | 📖 OpenViking · 浏览记忆库 | 查看目录、层级或条目元信息 |
| `viking_remember` | 📖 OpenViking · 记住信息 | 提交长期信息，由 OpenViking 提炼、合并或跳过 |
| `viking_forget` | 📖 OpenViking · 删除记忆 | 按精确 URI 删除一条指定记忆 |
| `viking_add_resource` | 📖 OpenViking · 导入资料 | 导入网址、本地文件或目录并建立索引 |

OpenViking 的六个工具名对应 [Hermes OpenViking 工具定义](https://github.com/NousResearch/hermes-agent/blob/b9271bcb34e1a8b8fe0eeaef0ef4a6e1f93ba543/plugins/memory/openviking/__init__.py#L377-L450)。

检索、读取和浏览也涵盖记忆库中的知识资料。“记住信息”表示提交记忆提炼，
## 多 Profile 网关（multiplex）

Hermes `gateway.multiplex_profiles`（官方默认开启）用**一个进程**服务多个
Profile，并且会**按 profile 各加载一次目录插件**（模块名
`hermes_plugins.hermes_lark_streaming__home_<digest>`）。插件为此做了三件事：

1. **按 Profile 隔离**——控制器、配置、凭据都以当时 `get_hermes_home()` 的
   home 为准；凭据读 host 的 profile secret scope，multiplex 下不再回退
   `os.environ`（那是启动 Profile 的凭据）。`hermes ... status` 会打印 home。
2. **跨副本去重**——补丁标记写在**共享的宿主对象**上（`GatewayRunner` /
   `FeishuAdapter` 类属性 + 被包装函数的 `_hls_wrapped`），副本之间互相可见，
   所以只装一层 wrapper、只建一张卡片、只回复一次。
3. **进程级状态共享**——每个 home 一个控制器，登记表挂在宿主模块上
   （`runtime_globals.shared_store`），避免两份副本各建一个控制器。

排查时可核对：日志里 `feishu inbound ids` 与 `HLS: session created` 每条消息
应各出现 **1 次**；`FeishuClient initialized` 的 `app_id` 与 `home` 要和消息所
在 Profile 一致。若出现 3 次或 `230002 Bot/User can NOT be out of the chat`，
说明去重或作用域失效。

## 故障排查

| 现象 | 检查项 |
|------|--------|
| 没有卡片 | `hermes plugins list` 是否启用、凭据是否存在、日志是否有 `HLS:` |
| 元素超限（300305） | 降低 `max_tool_steps`/`max_reasoning_rounds`；代码有最终安全网 |
| Schema 错误（300315） | 检查 CardKit v2 卡片结构和字号值是否合法 |
| 流式卡片卡住 | 增大 `card_ttl_sec`，确认卡片未被删除/撤回 |
| 字号未变化 | 确认 `text_sizes` 缩进、角色/字号合法；旧卡片不会被新配置改变 |
| 速度时有时无 | `grep "HLS: speed hidden" "$HERMES_HOME/logs/agent.log"`；`window_too_short` 表示该次上游整段下发，仍会隐藏 |
| 卡片退化为纯文本 / 230002 | 多 Profile 场景先看上面「多 Profile 网关」：同一条消息的 `feishu inbound ids`、`HLS: session created` 是否各只有 1 次 |
| Profile 用错 bot | 核对 `FeishuClient initialized` 的 `app_id`/`home` 对应该 Profile |

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
