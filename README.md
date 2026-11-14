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

`/topic list` 列出话题，`/topic new <标题>` 建立子话题，`/topic use <ID>` 切换后续提问归属。Web 进入分支或切换标签即同步当前位置，后续提问沿本线最新位置继续。关闭浏览器不会停止终端任务。

见 [Web 运行说明](web/README.md)。

## 离线验证

安装依赖后运行：

```powershell
python tools\verify.py
```

验证使用临时目录与本地假模型，不需要 API Key，也不访问真实用户会话。

## 许可证

保留上游 MIT 许可证和原作者版权声明，见 `LICENSE`。此版本基于上游 0.4.5 的本机发行源码进行精简和更名。
