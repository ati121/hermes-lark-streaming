# OpenViking 实时阶段扩展

飞书插件和本扩展配合显示：意图分析 → 记忆检索。实际阶段返回前只显示加载动画。
阶段来自真实方法调用，不使用计时器猜测。直接检索跳过分析，零查询计划跳过检索。

扩展已在 OpenViking 0.4.22 环境验证。它包装 `IntentAnalyzer.analyze` 和
`HierarchicalRetriever.retrieve`，仅对带 `X-Hermes-Progress: 1` 的 search/find 请求
返回阶段事件及最终 HTTP 结果。认证、查询参数、结果和错误仍由 OpenViking 处理。
其他客户端得到原来的 JSON。飞书插件连接未安装扩展的服务器时也可正常工作，
但无法显示 `search` 内部的阶段。

## 安装

先更新飞书插件。将本目录下两个 Python 文件放到 OpenViking 环境，使用该环境的
Python 执行；检查命令不会修改文件，安装命令不会自动重启服务。

```bash
python install_openviking_progress.py --check
python install_openviking_progress.py --install
```

安装器检查已知方法和 `create_app` 结构，将扩展复制进 OpenViking 包，并在
应用工厂返回前加入初始化调用。重复安装不会重复插入。安装完成后重启
OpenViking，更新后的飞书插件在下一轮预取中会自动协商使用阶段协议。

Docker 部署不能只修改运行容器的文件层，否则容器重建或镜像更新时会丢失。
可以将本目录挂载到 `/opt/hermes-progress:ro`，将入口设为
`["/bin/sh", "/opt/hermes-progress/with-progress.sh"]`。包装脚本每次启动时安装扩展，
然后执行原来的 `openviking-entrypoint`；未来版本不兼容时记录警告并正常启动服务。
此方式适用于原入口为 `openviking-entrypoint` 的官方镜像，保留服务原有的 command。

也可以在自己的镜像构建中运行安装器（在原 OpenViking 镜像基础上）：

```dockerfile
COPY integrations/openviking_progress.py integrations/install_openviking_progress.py /opt/hermes-progress/
RUN python /opt/hermes-progress/install_openviking_progress.py --install
```

## 移除

```bash
python install_openviking_progress.py --uninstall
```

随后重启 OpenViking。移除操作只删除本扩展的初始化段和模块，保留应用文件的其他改动。

## 生命周期

每个请求有独立事件队列，通过 ContextVar 隔离并发请求。插件只在自动预取期间
启用协议，沿用 Hermes 的认证重试和超时边界。预取完成或超时后关闭通知，服务端
迟到的事件不能再改动卡片；正常模型输出、工具调用和上下文压缩仍具有更高显示优先级。
