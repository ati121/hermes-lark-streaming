# 维护上下文

本文件是维护本复刻仓库前的必读说明。它把上游关系、本地功能、生命周期约束和验证
流程集中记录，避免以后同步上游时误覆盖本地修改或再次提交私有信息。

## 仓库关系

| 名称 | 地址/分支 | 用途 |
|------|-----------|------|
| 当前仓库 | `https://github.com/ati121/hermes-lark-streaming` / `main` | 个人自用版本，发布源 |
| 直接复刻来源 | `https://github.com/Aowen-Nowor/hermes-lark-streaming` / `github_sync` | 上游代码和文档基线 |
| 更上游 | `https://github.com/Cheerwhy/hermes-lark-streaming` | Aowen 版本的历史来源 |

当前直接上游基线（2026-08-07）为：

```text
4793ac08a9f8d5979c045c49756e95f51901134d
```

本仓库曾经为清理公开历史而重写过 Git 历史，因此本地 `main` 与上游历史没有可用的
共同祖先。同步时应按“上游提交补丁 + 本地修改核对”的方式处理，不能直接 merge
或把上游分支强行推到当前仓库。

## 本地修改

当前代码基于 Aowen-Nowor 版本，保留其 CardKit v2 流式卡片、统一推理/工具面板、
`/aowen` 命令、元素上限安全网和 Hermes 兼容层，并增加以下个人修改：

- `hermes_lark_streaming.text_sizes`：按 `body`、`reasoning`、`tool`、`notice`、
  `footer` 角色配置字号。
- 每个角色支持单一字号，或 `default`/`pc`/`mobile` 三项设备映射。
- 缺省设备项继承 `default`；缺省 `default` 使用角色默认值。
- 设备映射使用 `hls_*` CardKit 样式别名，并在普通 IM 交互卡片传输中生效。
- 卡片创建时把字号配置写入 `CardSession.text_sizes`，同一张卡片的整个流式生命周期
  使用同一份快照；配置 reload 只影响新卡片。
- 文档、测试和安装脚本指向当前 GitHub 仓库；不保留上游的私有邮箱、群链接、Gitee
  部署路径、凭据、Token、日志或服务器信息。

## 不变量

修改卡片渲染、配置读取或控制器传递时必须保持：

1. 未配置 `hermes_lark_streaming.text_sizes` 时，生成的 Card JSON 必须与修改前完全
   一致（尤其不能无条件添加 `config.style`）。
2. 同一张卡片从创建到完成、异常、终止和重试的所有渲染路径都使用同一份字号快照。
3. `text_sizes` 的角色、设备键和字号值必须经过 `normalize_text_sizes` 校验；非法值
   报出包含精确配置路径的 `ValueError`。
4. 新增配置必须同时更新配置校验、卡片渲染、控制器传递、文档和回归测试。
5. 不得把真实的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、Webhook、会话 ID、服务器路径、
   IP、Token、邮箱邀请链接或生产日志写入 Git。

## 关键文件

- `config/reader.py`：字号允许值、角色默认值和配置归一化。
- `cardkit/elements.py`、`cardkit/cards.py`：角色字号和 `hls_*` 设备别名的渲染。
- `controller/linear_mixin.py`：卡片创建时快照配置及 interactive 传输选择。
- `state/session.py`：`CardSession.text_sizes` 生命周期字段。
- `tests/test_config.py`：配置校验、继承和默认值。
- `tests/test_cardkit.py`：无配置 JSON 不变、设备别名和各角色渲染。
- `tests/test_controller.py`：快照传递和整卡更新路径。
- `docs/AGENT_GUIDE.md`、`README.md`、`README.zh-CN.md`：公开使用说明。

## 默认验证

在仓库根目录执行：

```bash
python -m pytest tests/
```

`pyproject.toml` 已将 `tests/integration/` 排除在默认套件之外。Hermes 源码兼容性测试
需要设置 `HERMES_SRC_DIR`，再单独执行：

```bash
HERMES_SRC_DIR=/path/to/hermes-agent python -m pytest tests/integration/ -v
```

发布前还应检查：

```bash
python -m ruff check .
git grep -nE 'FEISHU_APP_SECRET=|cli_[A-Za-z0-9]{12,}|token=[A-Za-z0-9_%=-]{12,}' -- . ':!tests/e2e/.env.example'
git status --short
```

最后一条敏感信息扫描允许命中占位符时人工确认，真实值不得提交。

## 同步直接上游

以下流程适用于上游 `github_sync` 分支有新提交时。先阅读本文件和当前工作区状态，
确认没有未提交的功能修改。

```bash
git remote add upstream https://github.com/Aowen-Nowor/hermes-lark-streaming.git  # 只需一次
git fetch upstream github_sync

# 对比上次记录的上游基线和最新分支
git diff --stat 4793ac08a9f8d5979c045c49756e95f51901134d upstream/github_sync
git diff 4793ac08a9f8d5979c045c49756e95f51901134d upstream/github_sync -- . ':!README.md' ':!README.zh-CN.md' ':!docs/AGENT_GUIDE.md' ':!docs/MAINTENANCE_CONTEXT.md'
```

确认差异后，将上游代码变更作为补丁应用到当前分支，并人工解决字号代码和公开文档的
冲突：

```bash
git diff --binary 4793ac08a9f8d5979c045c49756e95f51901134d upstream/github_sync -- . ':!README.md' ':!README.zh-CN.md' ':!docs/AGENT_GUIDE.md' ':!docs/MAINTENANCE_CONTEXT.md' > /tmp/hermes-upstream.patch
git apply --3way /tmp/hermes-upstream.patch
```

重点复核 `config/reader.py`、`cardkit/`、`controller/linear_mixin.py`、`state/session.py`
和三组字号回归测试。同步完成后更新本文件中的上游 SHA、更新 CHANGELOG，运行默认测试
套件，再提交到当前仓库 `main`：

```bash
git add .
git commit -m "sync: merge upstream changes"
git push origin main
```

README 和本地维护文档应继续使用当前仓库地址；不要把上游的私有联系信息复制回来。
