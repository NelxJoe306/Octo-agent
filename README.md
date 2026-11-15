# Octo-agent

面向开发者的终端编程 Agent（CLI / TUI），内置文件与 Shell 工具、SQLite 持久会话、分支话题树（含 Web 话题地图）与后台异步长期记忆。**状态：实验性（`v0.1.0`，适合个人与内部使用，SQLite schema 仍在演进）。**

## 快速开始

**前置要求**：Python 3.12+、`uv`；如需使用 `/map` 网页话题地图，还需 Node.js 与 `npm`。

```powershell
# 1. 克隆仓库并进入目录
git clone https://github.com/NelxJoe306/Octo-agent.git
cd Octo-agent

# 2. 仅安装 Python 依赖（环境隔离在 dependencies/python/.venv，并在根目录生成 .venv 链接）
python tools/install_dependencies.py --python-only

# 3. 跑通离线冒烟验证（使用本地假模型与临时目录，无需 API Key）
.\.venv\Scripts\python.exe tools/verify.py

# 4. 配置模型 API Key 并初始化 OpenAI 兼容 Provider
$env:OPENAI_API_KEY = "sk-xxx"
.\.venv\Scripts\octo.exe setup

# 5. 查看已生效的 Provider 配置与凭据状态
.\.venv\Scripts\octo.exe providers

# 6. 启动交互式 TUI（Linux / macOS 将 .\.venv\Scripts\octo.exe 换为 ./.venv/bin/octo）
.\.venv\Scripts\octo.exe
```

第 3 步成功时会打印 JSON，例如 `"tests": 11, "failures": 0, "errors": 0, "success": true`。第 4 步默认模型是 `gpt-5.4`；若使用第三方 OpenAI 兼容接口，请加上 `--base-url` 和 `--model`。超时和重试写入 Provider 配置（默认 `60.0` 秒、重试 `2` 次），用 `octo setup --timeout-seconds`、`--max-retries` 修改。

### 环境变量

| 变量名 | 是否必填 | 默认值 | 说明 |
| :--- | :--- | :--- | :--- |
| `OPENAI_API_KEY` | **必填**（调用模型时） | 无 | 默认模型凭据变量名；若在 `octo setup --api-key-env` 指定了自定义变量名，则以指定变量为准 |
| `OPENAI_BASE_URL` | 选填 | `https://api.openai.com/v1` | 在默认 Provider（`openai` 且凭据变量为 `OPENAI_API_KEY`）下，运行时覆盖已保存的接口地址；也可用 `octo setup --base-url` 写入配置 |
| `OCTO_HOME` | 选填 | `~/.octo` | 全局状态、凭据与 `octo.db` 存储目录（必须为绝对路径或以 `~` 开头） |
| `OCTO_DISABLE_MEMORY` | 选填 | `0` | 设为 `1` 临时关闭后台记忆提炼与跨会话偏好召回；`0` 沿用 `${OCTO_HOME}/settings.json` 配置 |
| `OCTO_OFFLINE` | 选填 | 未设置 | 设置任意值时跳过联网版本检查与外部模型目录拉取 |

## 用法

### 1. 命令行单次执行（非交互模式）

```powershell
# 在当前仓库执行单次提问并直接打印结果退出
.\.venv\Scripts\octo.exe --print "列出当前目录的顶层模块并概括职责"
```

### 2. 交互式会话与话题分支

在 `octo` 交互式终端内直接使用斜杠命令管理上下文与分支：

```text
/topic-list                # 查看当前会话的话题分支列表
/topic-new 接口重构方案     # 从当前位置新建并切换到子话题分支
/topic-use main            # 切回主干话题继续提问
/session                   # 查看当前会话、上下文预算与后台记忆状态
```

### 3. 打开 Web 话题地图（可选）

```powershell
# 首次使用前安装前端依赖并构建静态产物
python tools/install_dependencies.py --web-only
npm --prefix web run build
```

构建完成后，在运行中的 `octo` 终端输入 `/map`，会自动在浏览器打开附着当前会话的话题树视图。

### 4. 查看与导出历史会话

```powershell
# 列出本地已索引的会话 ID
.\.venv\Scripts\octo.exe sessions

# 将指定会话导出为独立可浏览的 HTML 或 JSONL
.\.venv\Scripts\octo.exe export <SESSION_ID> ./session-export.html
```

## 目录结构

- `src/agent_core/`：消息与工具协议、ReAct 主循环、上下文预算门控、SQLite / JSONL 会话与话题树存储
- `src/model_adapters/`：OpenAI 兼容与 Anthropic 模型适配器、流式事件解析与请求预算控制
- `src/long_term_memory/`：长期记忆事实库、租约任务队列、后台异步提炼与 `memory.md` 单向镜像导出
- `src/terminal_app/`：CLI / TUI 交互界面、内置文件与 Shell 工具、Provider 生命周期与本地话题地图接口
- `web/`：独立构建的 Next.js 话题地图前端（通过终端 `/map` 附着当前会话，不直接接触模型凭据）
- `tools/`：依赖隔离安装（`install_dependencies.py`）、离线冒烟验证（`verify.py`）与记忆镜像重建脚本（`export_memory.py`）
- `tests/`：按 `core`、`providers`、`storage`、`memory`、`sessions` 划分的离线测试套件
- `docs/`：架构与模块边界细节，详见 [模块职责与依赖方向](docs/module-layout.md)、[异步记忆说明](docs/async-memory.md)、[记忆存储底座](docs/memory-storage.md)、[持久工具产物](docs/tool-artifacts.md)、[Web 话题地图说明](web/README.md)

## 已知限制

- **SQLite 库不向下兼容**：当前会话数据库为 `schema 6`。升级前请先关闭旧进程并备份 `${OCTO_HOME}/octo.db`，旧版程序无法直接打开升级后的数据库。
- **JSONL 导出不等于完整备份**：`octo export` 导出的 JSONL 不包含超长工具输出产物（`tool_artifacts` BLOB）和长期记忆表，完整备份请直接复制 `octo.db`。
- **`memory.md` 仅为只读镜像**：`${OCTO_HOME}/memory.md` 由 SQLite 单向幂等导出，手动修改该文件不会写回数据库，下次导出时会被覆盖；损坏或滞后时可执行 `.\.venv\Scripts\python.exe tools/export_memory.py` 重建（默认使用 `${OCTO_HOME}`）。若指定其他目录，请传绝对路径，例如 `--home $env:USERPROFILE\.octo`。
- **后台记忆会增加模型调用**：默认开启后台记忆提炼（每累计 32 条原始消息、出现“记住/更正/忘掉”等触发词或正常退出会话时触发）。如需零额外调用，设置 `OCTO_DISABLE_MEMORY=1` 或在 `${OCTO_HOME}/settings.json` 中将 `memory.enabled` 设为 `false`。
- **Web 窗口只读交互**：`/map` 仅用于浏览消息记录、创建和切换话题分支，不支持在网页端直接发送聊天消息、配置模型或执行工具。

## 参与

仓库模块依赖方向与代码修改红线见 [docs/module-layout.md](docs/module-layout.md)（基础模块严禁反向导入 `terminal_app`）。提 Issue 或 PR 前，请确认以下离线检查全绿（无需配置真实 API Key）：

```powershell
# 运行 11 项核心冒烟检查
.\.venv\Scripts\python.exe tools/verify.py

# 运行全量 Python 单元测试（含模块边界与异步记忆测试）
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p "test_*.py" -q

# 若改动涉及 web/，同步运行前端测试、类型检查与构建验证
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run build
```

许可证：MIT（见 [LICENSE](LICENSE)）。
