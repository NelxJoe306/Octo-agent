# Octo

Python 编程助手，保留流式模型调用、文件与命令工具、交互终端、扩展和持久会话。

## 运行

需要 Python 3.12 或更新版本。推荐使用 uv：

```powershell
cd D:\Octo
uv run octo --help
uv run octo
uv run octo --print "查看当前项目结构"
```

也可以安装到独立虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\octo.exe --help
```

## 配置

默认状态目录为 `~/.octo`，可用绝对路径环境变量 `OCTO_HOME` 覆盖。项目配置目录为 `.octo/`。已有项目 `AGENTS.md` 的发现和读取保持可用。

```powershell
$env:OPENAI_API_KEY = "你的 API Key"
uv run octo setup
uv run octo providers
```

源码副本不会迁移其他应用的凭据、配置或历史会话。按需要在 Octo 中重新配置。模型目录仍可用 `octo models-refresh` 刷新；这不会升级 Octo 应用。

## 代码结构

- `octo_agent/`：消息协议、Agent 循环、工具调用与会话存储。
- `octo_ai/`：模型协议适配、流式响应与网络处理。
- `octo_coding/`：编程工具、会话管理、CLI、TUI 和扩展。


## 话题地图

模型、凭据、推理强度、聊天和工具继续在终端使用。在 `web/` 执行 `npm ci`、`npm run build` 后，在运行中的终端输入 `/map`，打开连接当前会话的话题树和记录窗口。

`/topic-list` 列出话题，`/topic-new <标题>` 建立并切换子话题，`/topic-use <ID>` 切换后续提问归属。Web 进入分支或切换标签即同步当前位置，后续提问沿本线最新位置继续。关闭浏览器不会停止终端任务。

见 [Web 运行说明](web/README.md)。

恢复会话不要求旧模型名仍然可用：未显式指定模型时，仍可用的历史选择继续沿用；旧模型已移除时改用该提供商当前模型，旧提供商已移除时使用当前可用配置。终端会显示模型变化，原消息、分支和归属不变。用户本次显式输入的无效模型仍会报错，不静默替换。

## 持久工具输出

SQLite 会话中的大段文本工具结果会保存为持久产物，模型收到首尾预览和 artifact ID，并可通过 read_artifact 分段读取。默认每次 2048 UTF-8 字节、最多 4096；重启恢复同一会话后 ID 仍有效。详见 [产物设计与边界](docs/tool-artifacts.md)。

本版升级会话库到 schema 6（增加记忆待处理水位）。首次运行前关闭旧进程并备份 SQLite；旧版无法直接打开升级后的库，JSONL 导出不包含产物正文或长期记忆。

## 长期记忆

已提供事实新增/更正/撤回、持久提炼任务、原始证据引用，以及事实/学习进度/收据原子提交。已接入独立后台提炼：默认复用当前模型，累计 32 条消息、明确记忆请求和正常关闭尾部时触发。跨会话先只自动召回用户偏好，主聊天不等待提炼。会增加模型调用量；可在本机 settings.json 的 memory.enabled 关闭，或用 OCTO_DISABLE_MEMORY=1 临时关闭。配置、恢复与模型能力边界见 [异步记忆说明](docs/async-memory.md)，存储接口见 [记忆存储说明](docs/memory-storage.md)。

## 离线验证

安装依赖后运行：

```powershell
python tools\verify.py
```

验证使用临时目录与本地假模型，不需要 API Key，也不访问真实用户会话。

## 许可证

保留上游 MIT 许可证和原作者版权声明，见 `LICENSE`。此版本基于上游 0.4.5 的本机发行源码进行精简和更名。
