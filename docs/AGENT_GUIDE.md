# hermes-lark-streaming 安装与维护指南

> 高信息密度参考文档，供 Hermes Agent 或其他自动化 Agent 解析。
> 版本号以 `plugin.yaml` 为准，变更记录见 [CHANGELOG.md](CHANGELOG.md)。

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
| `flush_interval_ms` | `500` | 70–2000 | 面板刷新间隔（毫秒）；普通 interactive 整卡更新另有至少 1000ms 的间隔，CardKit 正文使用独立流式节流 |
| `card_ttl_sec` | `600` | >0 | 卡片存活检测超时（秒） |
| `max_tool_steps` | `20` | 1–100 | 面板显示的工具步骤上限 |
| `max_reasoning_rounds` | `20` | 1–100 | 面板显示的推理轮次上限 |
| `footer.show_label` | `false` | bool | 是否显示页脚字段标签 |
| `footer.fields` | status/elapsed/speed/model/cost/compression_exhausted | array | 页脚字段排列 |
| `busy_supersede_new_card` | `true` | bool | agent 忙时收到新消息 → 封口当前卡片，后续输出开新卡（见下） |
| `text_sizes` | `{}` | mapping | PC/手机端设备差异字号（本地新增） |

可选的页脚字段名：`status`、`elapsed`、`speed`、`model`、`tokens`、`context`、
`cache`、`cost`、`api_calls`、`history_offset`、`compression_exhausted`。未知字段名
会被静默跳过。

`speed` 默认开启，显示**当前这一轮对话中所有主模型请求的加权生成速度**（如
`50 t/s`），不再只取最后一次请求或最后一段正文。不想要可以从 `fields` 里去掉。

- 公式：`本轮各请求可测生成 token 的合计 / 对应生成窗口秒数的合计`。不是各次 t/s 的
  算术平均值；多次工具调用前后的模型请求都会累计，新用户回合重新统计。
- 计时直接观察 Hermes Relay 的提供方原始流，在正文缓冲、工具文本抑制和卡片回调
  之前记录；从首个有效内容到提供方流结束，不计首字等待、预填、客户端收尾、
  工具执行或请求间空闲。role-only、空包、usage 包不作为首字。
- 推理处理参考 magpie：有推理计数且有正文时，只用非推理输出和首段正文之后的
  窗口；没有正文时，仅连续可见的推理流可以计速。隐藏推理不能硬塞进很短的正文
  窗口。提供方把推理单列时先按原始总数校正，避免重复计数；不修改正常用量或费用。
- 小于 100ms、超过 10,000 t/s、明显缓冲后突发下发的窗口不显示。非流式、只有一次
  内容下发、缺少原始流观测接口的旧宿主，不用整次请求耗时伪装成解码速度。
- 同一轮重复安装回调不会清零；复用 agent 的新回合、打断后的新回合独立统计。
  同卡续接继续累计；`api_calls` 优先使用与速度相同的累计请求数，不取最后一次
  续接的局部计数。后台复审等 detached fork 不计入，嵌套 API 包装不重复计数。
- 任一请求失败、缺少可靠 usage/生成窗口、内部多次尝试无法对应或仍未结束时，速度隐藏，避免
  把只有部分请求的数据误当作全量平均值。日志 `HLS: speed hidden` 中的
  `measured`、`requests` 可定位缺失；`HLS: speed measured` 记录合计 token 和生成秒数，
  DEBUG 日志记录单次采样结果，只含计数和计时。不会回退到上一轮或整张卡片耗时。

基础示例：

```yaml
hermes_lark_streaming:
  panel_expanded: false
  streaming_panel_expanded: false
  print_strategy: delay
  print_step: 4
  flush_interval_ms: 500
  card_ttl_sec: 600
  max_tool_steps: 20
  max_reasoning_rounds: 20
  busy_supersede_new_card: true
  footer:
    show_label: false
    fields:
      - [status, elapsed, speed, model, cost, compression_exhausted]

display:
  show_reasoning: true
```

### agent 忙时收到新消息（busy follow-up）

agent 还在跑、用户又发了一条消息时，Hermes 把它交给自己内部的 busy 入口：把新消息
排队成下一回合，并打断正在跑的回合。这条路径**不经过** gateway 的 inbound 入口
（Hermes 源码注释：busy callbacks bypass the message handler），插件拿不到新的
message_id —— 没有 start 钩子，也就不会建新卡。

默认行为（`busy_supersede_new_card: true`）：把当前这张卡片封口，标记成**被新消息
接续**（不是正常完成，避免看起来像答完了），再开一张新卡继续输出；新卡锚在触发它的
那条消息上。打断那一轮的后续回调仍带着旧 message_id，控制器在会话查找时统一转发到
新卡，答案、思考和工具行都落过去。

**可以连着打断。** 同一个 chat 里再打断一次时，上一轮开出来的那张续写卡本身就是封口
目标 —— 每次打断都开一张新卡，续写映射成链（`M1 → M2-cont-1 → M3-cont-1`），回调
一路转发到链尾。防重落在两处：同一条触发消息只认领一次，已经封过的卡不再封（它已有
续写映射）。（v1.6.39 之前是按"续写卡不再封"来防重的，结果是第二次打断永远静默失效。）

关掉就是旧行为：后续输出继续写在被打断的那张卡片里，用户得往上翻才看得到。

后台委派完成、心跳这类内部事件走同一个 busy 入口，会被过滤掉，不会拿来封用户的卡片。

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

普通 interactive 整卡更新采用独立保护：v1.31 起至少间隔 1 秒（v1.30 为 1.5 秒）；收到飞书
`230020` 后冷却 12 秒，期间合并内容而不反复 PATCH。完成态使用
1/2/4/8/16 秒退避重试；仍受限时优先发送保留排版和字号的静态结果卡，并在
后台间隔 20/30/60 秒有限次修复原卡。删除、会话清理或快照失效后停止修复；
只有静态卡发送也失败时才退回纯文本。后台修复只在当前进程内生效，不跨重启恢复。

### 思考过程块

`display.show_reasoning: true` 且配置了 `text_sizes`（即走普通 IM 交互卡片）时，
卡片顶部在折叠面板之外常显一个 `🫧 思考过程` 按钮：

- 生成中：按钮下方自动跟随最新约两行思考（按显示宽度计，中文算两格）。
  模型自己的硬换行会先折平，所以「一行一个短句」的模型同样是稳定两行，
  不会变成十几行碎句（v1.9）。
- 正文输出完：只留按钮，思考自动收起。
- 点按钮：展开全部思考过程（超过 5000 字截断并标注总字数），再点收起。
- 折叠面板「执行过程」只列工具步骤，不再重复显示推理，标题也不带「N 轮」；
  没有工具调用的回合不显示面板。

按钮在会话释放后仍可用：插件保留最近 60 张封口卡的整卡快照，用封口同一条整卡
PATCH 重渲染。更早的卡片快照已被挤掉，点按钮会回一条文本说明。流式正文里带的
`<think>`/`<thinking>`/`Reasoning:` 段也会被抽到这里，不再丢弃；标签对劈在两个
chunk 之间时按会话保持开合状态，正确分流。

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
`on_busy_superseded`、`on_answer_delta`、`on_thinking_delta`、`on_reasoning_delta`、
`on_tool_updated`、`on_memory_prefetch_updated`、`on_background_review_message`、
`on_cron_deliver`。编号与职责见 [SKILL.md「Hook 索引」](SKILL.md#9-hook-索引)。

## 入站图片预识别状态

Hermes 走图片转文字路径时（包括主模型无视觉能力、单独配置视觉模型），
插件在网关 `_enrich_message_with_vision` 的等待期间显示“图像分析中…”。
结束、异常或取消后移除该准备状态；等待主模型时恢复“等待上游模型响应”。
已经开始的模型输出、工具调用和上下文压缩具有更高优先级。
原生图片直传在网关确认附图后显示“图像分析中…”，首次模型活动后切换正常状态；
记忆预取和上下文压缩期间优先显示对应状态。模型主动调用 `vision_analyze` 仍显示“图像分析”。
这只更新卡片，不修改图片路由、视觉模型或识图结果，也不增加工具步骤。

`image_generate` 使用 GPT Image 系列模型时，状态行和工具面板显示
“GPT · 生成图片”。工具开始时快照模型参数或当前 Profile 的生图模型；
Octopus 使用已加载插件的模型解析逻辑，包含环境变量、插件专属配置和默认模型。
无法确定模型时保留通用名称，不从提示词猜测模型。

生图/生视频工具运行期间，状态行固定显示该行；同一批调用里同时发起的旁路工具
（读取文件、终端命令）不会再把它顶掉。生成结束后状态行照旧跟随后续步骤，
面板内容与折叠行为都不受影响。

本地 `terminal` 执行自定义 Python 文件或 `python -c`，且代码直接调用
`tools.image_generation_tool._handle_image_generate` 时，也使用同一生图标签。
识别只在工具开始时静态检查限长的脚本内容，不执行脚本，也不依赖文件名。
模块顶层和 `__main__` 分支中的直接调用可识别；仅导入、注释、字符串、未调用函数、
查看帮助和复制图片不会被判定为生图。远程终端、无法读取的文件、复杂 Shell/Python
控制流保留终端名称。标签快照用于运行状态和完成后的面板，实际工具参数保持不变。

## 记忆工具显示

状态行按记忆来源使用不同图标，所有文案都带上来源名称。Hindsight 的四个工具映射
统一用 👁️，OpenViking 的状态行和六个工具统一使用官方帆船图标；Hermes 内置记忆用 🧠，会话检索用 🔎。
工具面板同步使用带来源的中英文名称。
OpenViking 图片随插件打包，由各 bot 的飞书客户端上传并缓存；上传不可用时回退到标准检索图标。

Hermes 在模型调用前自动预取 OpenViking 记忆时，统一显示
`OpenViking · 查询记忆`（官方图标）；结束后恢复等待主模型的提示。
过程面板保留“OpenViking · 已完成记忆检索”和耗时，即使预取在首卡创建前已结束也可查看。
失败或等待超时分别标注，不表示检索命中数量，也不展示私有检索内容。
该状态表示整次预取，不声明当前正在执行意图分析还是检索，也不显示准备提示。
插件只观察 Hermes 的预取开始和结束，不改 OpenViking 服务端、Compose 或启动入口，
不包装 provider 的 HTTP 客户端，不发送自定义进度请求头。
按 [OpenViking 官方检索机制](https://docs.openviking.ai/en/concepts/07-retrieval)，
意图分析生成检索计划，随后进行层级检索和重排；分析使用 `query_planner` 模型，
未单独配置时回退到 `vlm`。结合 OpenViking 0.4.22 与 Hermes provider 的实现：

- 自动预取先准备会话记忆，再按当前问题检索。有会话 ID 时优先调用
  `POST /api/v1/search/search`；没有会话 ID 或发生可降级错误时使用 `search/find`。
- `search` 在启用意图分析且存在会话摘要或历史消息时才调用分析模型。
  分析可以生成零条查询，此时不执行后续检索；`find` 直接检索，不调用意图分析模型。
- 预取还包含读取命中内容等工作。原版 `search` 的 telemetry 随最终结果返回，
  不能用于实时切换。`search` 和 `find` 都使用“查询记忆”表示整次预取。
- 超时结束后，旧请求的迟到结束通知不会清除新请求的状态或污染下一轮卡片。
- v1.12–v1.13 的服务端扩展已撤除，旧部署的清理方式见
  [迁移说明](../integrations/README.md)。

插件包装当前 agent 的 `MemoryManager._prefetch_provider` 等待边界，因此请求失败或
达到 Hermes 的等待超时后也会结束提示；只写卡片过程记录，不向模型历史增加工具调用。
正常的模型输出、工具调用和上下文压缩优先于此准备状态。

| 工具 | 状态行 | 用途 |
|------|--------|------|
| `memory` | 🧠 Hermes · 内置记忆 | 新增、替换或删除内置记忆 |
| `session_search` | 🔎 Hermes · 会话检索 | 检索历史会话 |
| `hindsight_retain` | 👁️ Hindsight · 记忆写入 | 写入长期记忆 |
| `hindsight_recall` | 👁️ Hindsight · 记忆回溯 | 检索历史记忆 |
| `hindsight_reflect` | 👁️ Hindsight · 记忆推演 | 综合已有记忆进行推演 |
| `hindsight_operation` | 👁️ Hindsight · 记忆操作 | 其他记忆操作的显示映射 |
| `viking_search` | OpenViking · 检索记忆 | 语义检索记忆库，支持 auto/fast/deep 模式 |
| `viking_read` | OpenViking · 读取记忆 | 按摘要、概览或全文读取指定内容；URI 在 `viking://resources/` 下时显示为 OpenViking · 知识库 |
| `viking_browse` | OpenViking · 浏览记忆库 | 查看目录、层级或条目元信息；路径在 `viking://resources/` 下时显示为 OpenViking · 浏览知识库 |
| `viking_remember` | OpenViking · 记住信息 | 提交长期信息，由 OpenViking 提炼、合并或跳过 |
| `viking_forget` | OpenViking · 删除记忆 | 按精确 URI 删除一条指定记忆 |
| `viking_add_resource` | OpenViking · 导入资料 | 导入网址、本地文件或目录并建立索引 |

OpenViking 的六个工具名对应 [Hermes OpenViking 工具定义](https://github.com/NousResearch/hermes-agent/blob/b9271bcb34e1a8b8fe0eeaef0ef4a6e1f93ba543/plugins/memory/openviking/__init__.py#L377-L450)。

检索、读取和浏览也涵盖记忆库中的知识资料。“记住信息”表示提交记忆提炼，
是否真正写入由 OpenViking 决定（提炼、合并或跳过）。

## 工具面板图标与名称

工具面板每一行由 **彩色 emoji + 彩色粗体名称** 组成，例如 `⚙️ **进程管理**`。
早先版本用飞书的 `standard_icon` 图标 token 渲染行首，但该图标库是单色线稿，
任何 token 都只能渲染成灰白方块——五个 `browser_vault_*` 工具因此看起来是同一把灰锁。
现在行首改为 emoji，`standard_icon` 只保留在非工具位置（面板折叠箭头、上下文时钟）。

emoji 必须写在粗体**外面**：飞书会丢弃混入 emoji 的粗体段，
`**⚙️ 进程管理**` 会丢样式，`⚙️ **进程管理**` 才正常。

`state/tooluse.py` 的 `_TOOL_SPECS` 是唯一名称来源，键必须是 Hermes 的**实际注册名**。
旧表写的是更早版本的名字（`todo`、`cronjob`），而现行构建用的是
`todo_list` / `cronjob_manage` / `process_manage`，匹配不上就会落到
`_humanize_tool_name` 的英文兜底，中文卡片里就出现英文行。

| 家族/平台 | 前缀 | 示例 | 行首 emoji |
|-----------|------|------|-----------|
| 浏览器凭据库 | `browser_vault_*` | `浏览器 · 解锁凭据` | 🗂️ 🔓 ⌨️ 💾 🔢 |
| 看板协作 | `kanban_*` | `看板 · 请求审查` | 👀 🗒️ ✅ ⛔ 📤 … |
| Spotify | `spotify_*` | `音乐 · 播放控制` | ▶️ 🔊 📜 🔎 🎵 💿 📚 |
| Discord | `discord*` | `Discord · 服务器管理` | 💬 🛡️ |
| 元宝 | `yb_*` | `元宝 · 发送私聊` | 👥 👤 ✉️ 😀 🎴 |
| 桌面 GUI | `desktop_*` 等 | `桌面预览` | 📝 🖼️ ✖️ 🪟 📁 ☁️ 🧭 💡 |
| xAI 视频 | `xai_video_*` | `视频延长` | ✂️ ➡️ |
| Home Assistant | `ha_*` | `智能家居调用` | 🏠 📟 🛎️ 🎛️ |
| Hermes 内部 | 无 | `密钥规则` | 🚧 🔑 🏷️ 🧪 🧬 |

家族前缀统一写成 `家族 · 动作`，与既有的 `Hindsight · 记忆写入` 对齐。
`process_manage` 管的是 `terminal(background=true)` 起的后台终端进程
（poll/wait/kill/log/write/submit/close/handoff），所以是「进程管理」而非「流程管理」。
`browser_vault_*` 必须逐个列出精确名：`_TOOL_DESCRIPTORS` 的前缀别名
`browser` 会抢先匹配，把它们全部塌缩成一行 "Browser"。

MCP 工具（`mcp__server__tool`）保持英文小写拼接显示，这是设计如此，不做翻译。

自装的命令行工具经 `terminal` 调用时，Hermes 上报的工具名是 `terminal`，卡片会显示
🖥️ 终端命令。想让它以自己的名字出现，在 `state/tooluse.py` 里加规则：

- `_TERMINAL_PROGRAM_SPECS`：精确匹配，`程序名或脚本名: (中文名, 英文名, emoji)`。
  目前有 `smart-search` → 🔍、`gh` → GitHub。
  `gh` 只在实际访问 GitHub 时才换行显示；`gh --version`、`gh --help`、`gh help`、
  `gh config …`、`gh alias …`、`gh completion …`、`gh licenses`、`gh extension list`
  这类纯本地调用仍是 🖥️ 终端命令（见 `_gh_is_local_only`）。`gh auth status` 会联网
  校验 token，所以算 GitHub 调用。
- GitHub 行不再用 emoji，而是 `feishu/assets/github.png` 真实图标：`_tool_brand`
  给命中行打 `brand="github"`，`cardkit/elements.py` 把它放进 div 的 `icon` 槽，
  由 `feishu/branding.py` 按 bot 上传并缓存 `img_key`（与 OpenViking 同一套机制，
  上传失败回退 `setting_outlined`）。新增品牌图标时在 `_ASSETS` 和 `_BRAND_ICONS`
  各加一条即可。
- 品牌图标在面板工具行里走 `div.icon`；在状态行里必须走 `column_set`。飞书
  `div.icon` 只能放一个图标对象（没有数组形式），`lark_md` 也不支持行内图片
  （官方明确写「不支持在 text 元素的 lark_md 类型中使用」），所以「动画 + 品牌」
  只能拆成两个元素。状态行因此**恒定**是两列 `column_set`：左列放
  `_LOADING_IMG_KEY` 动画，右列放品牌 mark 与标签（无品牌时右列只有标签）。
  形状不能随品牌有无而变——`partial_update_element` 禁止改 `tag`，一旦在
  `div` 与 `column_set` 间切换，`gh` 一开始就会被拒。`_LOADING_ELEMENT_ID`
  始终挂在外层 `column_set` 上，封卡的删除路径与标签更新都按同一个 id 寻址。
  状态行带 `_STATUS_ROW_INSET`（8px 左缩进），与面板 8px 内边距对齐。
- `_IMAGE_SCRIPTS`：只识别已知生图入口 `gpt_image_gen.py` / `zimage_gen.py`，
  并由 `_image_script_action` 按参数确定动作。GPT 脚本可覆盖模型，标签使用家族名，
  不把默认模型版本写死在界面上。
- `patching/image_labels.py`：补充识别自定义 Python 脚本对 Hermes 生图入口的直接调用，
  在回调中快照模型和生图标记，渲染时不重复读文件。

| 调用 | 显示 |
|------|------|
| GPT Image 脚本携带提示词 | 🎨 GPT · 生成图片 |
| GPT Image 脚本携带提示词和 `--image` | 🎨 GPT · 编辑图片 |
| GPT Image 脚本 `-h` / `--help` | 📖 GPT · 查看帮助 |
| Zimage 脚本携带提示词 | 🎨 Zimage · 生成图片 |
| 空参数、读取/编译脚本、其他含 image 的程序 | 🖥️ 终端命令 |

生图匹配尊重引号和注释，不把示例字符串、内联 Python 或 heredoc 里的脚本文本
当成实际执行；无法确认的复杂 shell 命令保留终端名称。

匹配的是每个 shell 段开头的程序名，跳过 `sudo`/`env`/变量赋值并去掉路径；程序是
`python3`/`bash`/`node` 这类解释器时，看它后面的脚本名。只出现在参数里不算
（`pip install imageio`、`ls …/images/` 仍是终端命令）。命中后工具行、状态行和面板
标题都按该程序显示，detail 里去掉程序名和脚本名只留参数。

变量引用同样算数：同一行里的 `NAME=value`、`export NAME=value` 先收集成一张表，
随后出现的 `$NAME` / `${NAME}` 按这张表解析。所以 `GH=/opt/data/.local/bin/gh; …
$GH api repos/…`（serveom 的常见写法）和 `export GH=…` 都能命中 🐙 GitHub，
`$PY zimage_gen.py` 也走解释器规则。表里查不到或与程序无关的引用不动
（`$DC exec …`、`SSH_OPTS="-F …"`、未赋值的 `$NOT_SET` 仍是 🖥️ 终端命令）。

程序在后面的段里才出现时，detail 从**那一段**重新开始，而不是停在行首的赋值上：
`GH=gh; echo …; $GH api repos/x` 的 detail 是 `api repos/x`。

匹配用的是 Hermes 在 `tool.started` 事件里一并传来的完整参数字典，不是预览文本：
飞书平台的预览默认截到 40 字（`display.tool_preview_length`），带长路径的命令预览里
根本没有脚本名。命中别名的行 detail 也从完整参数重建，超过 80 字截断成一行。

## 多 Profile 网关（multiplex）

Hermes `gateway.multiplex_profiles`（官方默认开启）用**一个进程**服务多个
Profile，并且会**按 profile 各加载一次目录插件**（模块名
`hermes_plugins.hermes_lark_streaming__home_<digest>`）。插件为此做了三件事：

1. **按 Profile 隔离**——控制器、配置、凭据都以当时 `get_hermes_home()` 的
   home 为准；凭据读 host 的 profile secret scope，multiplex 下不再回退
   `os.environ`（那是启动 Profile 的凭据）。`hermes ... status` 会打印 home。
2. **跨副本去重**——补丁标记写在**共享的宿主对象**上（`GatewayRunner` /
   `FeishuAdapter` 类属性 + 被包装函数的 `_hls_wrapped`），副本之间互相可见，
   所以只装一层 wrapper、只建一张卡片、只回复一次。类标记只用于诊断（记录首个
   安装者的 home）——逐方法 pass 每个副本都会跑：别的副本装过的显示 `adopted`，
   只有自己独有的新方法才补装。**副本之间版本不一致时这一点才生效**（v1.6.38
   起；此前命中类标记就整体 early-return，新版本的副本会连自己的新补丁一起跳过）。
3. **进程级状态共享**——每个 home 一个控制器，登记表挂在宿主模块上
   （`runtime_globals.shared_store`），避免两份副本各建一个控制器。

排查时可核对：日志里 `feishu inbound ids` 与 `HLS: session created` 每条消息
应各出现 **1 次**；`FeishuClient initialized` 的 `app_id` 与 `home` 要和消息所
在 Profile 一致。若出现 3 次或 `230002 Bot/User can NOT be out of the chat`，
说明去重或作用域失效。

### 陷阱：被中断的 `hermes plugins install` 会留下"会被加载"的暂存目录

Hermes 的 `hermes plugins install` 用
`tempfile.TemporaryDirectory(prefix=".install-", dir=plugins_dir)` 克隆到
`$HERMES_HOME/plugins/.install-<随机串>`，正常路径由 `__exit__` 删除。
**但进程被 SIGTERM 杀掉时不会删。** 典型触发链：

1. 自动化脚本（容器 entrypoint、自愈脚本）在 profile 缺插件时调用该命令，
   没加 `--force`，也没关掉 stdin。
2. Hermes 的插件安全扫描把本仓库 `docs/AGENT_GUIDE.md` 里讲 multiplex 凭据作用域
   的那句 `os.environ` 判为 HIGH `exfiltration`；社区来源 + CAUTION 直接
   `BLOCKED`，安装器转而等待 `Install anyway? [y/N]` 确认。
3. 脚本没有 TTY，命令一直挂着，直到外层 `timeout` 发 SIGTERM，`__exit__` 来不及
   执行，带着完整 clone 的 `.install-*` 留在磁盘上。网络是好的，卡点是确认提示。

后果：Hermes 的目录插件发现是 `for child in sorted(path.iterdir())`
（`hermes_cli/plugins_discovery.py`），`.` 的 ASCII 小于任何字母，于是同一 home 内
残留目录**先于正式目录注册**，日志里表现为 `capability_check plugin=.install-<id>/plugin`。
两份副本共用进程级去重标记，**先装 wrapper 的一方生效**；后到者只补装自己独有的
新方法（v1.6.38 起；此前是命中类标记就**静默 early-return**，`patching/__init__.py`
连 `adopted` 都不打），于是两份 `patch summary` 都显示 `GatewayRunner=✓`，日志上
分不出到底是哪份在渲染卡片。残留的是安装当时的版本，之后 `git pull` 更新正式目录
也不会影响它。

**修法**：调用改为 `hermes plugins install --force --enable "$PLUGIN_URL" </dev/null`，
并把超时放宽（例如 120→300 秒）。`--force` 直接接受 CAUTION 判定、不再弹确认，
`</dev/null` 让将来任何提示立刻失败而不是挂住。

排查与处置：

```bash
# 每个 home 都要看，不要只看默认 home
ls -a "$HERMES_HOME/plugins/" "$HERMES_HOME"/profiles/*/plugins/

# 有 .install-* 就挪出 plugins/（不要直接删，先隔离）
mkdir -p "$HERMES_HOME/.quarantine"
mv "$HERMES_HOME/plugins/.install-"* "$HERMES_HOME/.quarantine/"

# 重启后确认：版本计数 = profile 数，且没有 .install- 条目
grep -oE "hermes-lark-streaming v[0-9.]+" "$HERMES_HOME/logs/agent.log" | sort | uniq -c
grep -oE "capability_check plugin=[^ ]+" "$HERMES_HOME/logs/agent.log" | sort -u
```

## 故障排查

| 现象 | 检查项 |
|------|--------|
| 没有卡片 | `hermes plugins list` 是否启用、凭据是否存在、日志是否有 `HLS:` |
| 元素超限（300305） | 降低 `max_tool_steps`/`max_reasoning_rounds`；代码有最终安全网 |
| Schema 错误（300315） | 检查 CardKit v2 卡片结构和字号值是否合法 |
| 流式卡片卡住 / 230020 | 确认使用 v1.30 或更新版本；限流时会合并刷新并尝试封口修复，仍失败则先发静态结果卡；确认消息未被删除/撤回，增大 TTL 不能解除飞书限流 |
| 字号未变化 | 确认 `text_sizes` 缩进、角色/字号合法；旧卡片不会被新配置改变 |
| 速度时有时无 | 检查 `HLS: speed hidden`：`incomplete_requests` 表示本轮有失败、缺失 usage、计时无效或未结束的请求，`measured/requests` 是已完整测量/全部请求数；整段下发本身不会隐藏速度 |
| 卡片退化为纯文本 / 230002 | 多 Profile 场景先看上面「多 Profile 网关」：同一条消息的 `feishu inbound ids`、`HLS: session created` 是否各只有 1 次 |
| 改了但没效果 | 先看上面「陷阱：`hermes plugins install` 的中断残留」：`ls -a` 每个 home 的 `plugins/`，有 `.install-*` 就隔离掉再重启 |
| Profile 用错 bot | 核对 `FeishuClient initialized` 的 `app_id`/`home` 对应该 Profile |
| 思考过程按钮没反应 | `grep "reasoning toggle" "$HERMES_HOME/logs/agent.log"`：`no live session and no snapshot` 表示快照已被挤掉（只保留最近 60 张）；`card update failed` 多半是整卡超过约 30KB，降低 `REASONING_EXPANDED_LIMIT` |

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
