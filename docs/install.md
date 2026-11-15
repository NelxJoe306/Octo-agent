# 安装与常用命令

本文说明依赖隔离、环境变量、Web 话题地图的构建，以及日常使用命令。架构与模块边界见 [module-layout.md](module-layout.md)。

## 前置要求

- Python 3.12+
- `uv`
- 若使用 `/map` 网页话题地图：Node.js 与 `npm`

## 依赖隔离

Python 环境安装到 `dependencies/python/.venv`，Web 包安装到 `dependencies/web/node_modules`。根目录的 `.venv` 与 `web/node_modules` 是指向上述目录的链接，供原有工具继续使用：Windows 使用 Junction，其他系统使用符号链接。

安装器会校验目标路径不越出仓库根目录，避免写到仓库之外。已有但不是本次安装目标的目录会被保留并报错，不会被静默删除。受版本管理的依赖声明仍是 `pyproject.toml` 和 `web/package.json` / `package-lock.json`；安装区域内的清单副本和包不进 Git。

运行安装器前，请先退出占用该环境的终端或构建进程。

```powershell
# 只装 Python 依赖
python tools/install_dependencies.py --python-only

# 只装 Web 依赖（使用 /map 前需要）
python tools/install_dependencies.py --web-only
```

可用 `--offline` 使用已缓存的构建工具与包；全新安装通常需要联网。Linux 上的解释器路径为 `dependencies/python/.venv/bin/python`。源码布局不依赖本机绝对路径。

## 验证与启动

```powershell
# 离线冒烟（11 项，无需 API Key），成功时打印 JSON
.\.venv\Scripts\python.exe tools/verify.py

# 配置模型 API Key 并初始化 OpenAI 兼容 Provider
$env:OPENAI_API_KEY = "sk-xxx"
.\.venv\Scripts\octo.exe setup

# 查看已生效的 Provider 配置与凭据状态
.\.venv\Scripts\octo.exe providers

# 启动交互式 TUI
.\.venv\Scripts\octo.exe
```

`octo setup` 的默认模型是 `gpt-5.4`。若使用第三方 OpenAI 兼容接口，请加上 `--base-url` 和 `--model`。超时和重试写入 Provider 配置（默认 `60.0` 秒、重试 `2` 次），用 `octo setup --timeout-seconds`、`--max-retries` 修改。

Linux / macOS 将 `.\.venv\Scripts\octo.exe` 换成 `./.venv/bin/octo`，将 `.\.venv\Scripts\python.exe` 换成 `./.venv/bin/python`。

## 环境变量

| 变量名 | 是否必填 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `OPENAI_API_KEY` | **必填**（调用模型时） | 无 | 默认模型凭据变量名；若在 `octo setup --api-key-env` 指定了自定义变量名，则以指定变量为准 |
| `OPENAI_BASE_URL` | 选填 | `https://api.openai.com/v1` | 在默认 Provider（`openai` 且凭据变量为 `OPENAI_API_KEY`）下，运行时覆盖已保存的接口地址；也可用 `octo setup --base-url` 写入配置 |
| `OCTO_HOME` | 选填 | `~/.octo` | 全局状态、凭据与 `octo.db` 存储目录（必须为绝对路径或以 `~` 开头） |
| `OCTO_DISABLE_MEMORY` | 选填 | `0` | 设为 `1` 临时关闭后台记忆提炼与跨会话偏好召回；`0` 沿用 `${OCTO_HOME}/settings.json` 配置 |
| `OCTO_OFFLINE` | 选填 | 未设置 | 设置任意值时跳过联网版本检查与外部模型目录拉取 |

`OPENAI_TIMEOUT_SECONDS` 和 `OPENAI_MAX_RETRIES` 不会作用于普通 CLI / TUI；超时和重试以 Provider 配置为准。

## 常用命令

```powershell
# 单次提问并打印结果后退出
.\.venv\Scripts\octo.exe --print "列出当前目录的顶层模块并概括职责"

# 列出本地已索引的会话
.\.venv\Scripts\octo.exe sessions

# 将会话导出为 HTML 或 JSONL
.\.venv\Scripts\octo.exe export <SESSION_ID> ./session-export.html
```

在交互式终端内：

```text
/topic-list                # 查看当前会话的话题分支列表
/topic-new 接口重构方案     # 从当前位置新建并切换到子话题
/topic-use main            # 切回主干话题
/session                   # 查看当前会话、上下文预算与后台记忆状态
/map                       # 打开 Web 话题地图（需先构建前端）
```

## 构建 Web 话题地图

```powershell
python tools/install_dependencies.py --web-only
npm --prefix web run build
```

构建完成后，在运行中的 `octo` 终端输入 `/map`，会在本机浏览器打开附着当前会话的话题树。接口只监听 `127.0.0.1`，浏览器不接收 Provider 凭据。

## 重建 memory.md

```powershell
# 使用默认的 OCTO_HOME，不启动 Agent，不调用模型
.\.venv\Scripts\python.exe tools/export_memory.py

# 指定其他目录时请用绝对路径
.\.venv\Scripts\python.exe tools/export_memory.py --home $env:USERPROFILE\.octo
```

目录中需有 `octo.db`。该命令只重建 Markdown 镜像，不能恢复或重建数据库。
