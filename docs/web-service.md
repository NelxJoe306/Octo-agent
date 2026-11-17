# Octo 单用户 FastAPI Web 服务运维与验证说明

更新日期：2026-11-17。

本文档对应 `feat/fastapi-web-service` 分支实现，涵盖 `src/web_api`、`octo-web` CLI、前端最小聊天入口及部署模板（`deploy/octo-web.service`、`deploy/Caddyfile.example`、`deploy/web.env.example`）。

## 1. 架构与能力边界

```text
浏览器（同源页面 + Fetch / EventSource）
  -> Caddy（HTTPS + 单用户 Basic Auth，整站鉴权，SSE 实时透传）
  -> octo-web / FastAPI（监听 127.0.0.1:18300，单 worker，关闭 reload）
  -> WebRuntime（单活动 CodingSession、单前台 driver、运行快照与去重、SSE 订阅）
  -> CodingSession（复用现有 Agent Loop、Context Harness、模型适配、SQLite 与长期记忆）
```

- **权限与隔离边界**：服务以普通用户 `octo` 运行，`--cwd`（如 `/home/octo/workspace`）是工具执行起始目录，不提供容器级或多租户沙盒隔离；`octo-web` 默认仅允许绑定回环地址（如 `127.0.0.1`），直接传入 `--host 0.0.0.0` 会被默认拒绝（需显式 `--allow-non-loopback` 才会放开），因为应用层不内置账号认证，公网访问必须经过 Caddy HTTPS + Basic Auth 整站认证。
- **取消保活语义**：`POST /api/runs/{run_id}/cancel` 触发 `cancelling` 状态广播时，`/api/runs/{run_id}/events` SSE 连接保持打开，直到后台 driver 完成清理和持久化并推送 `cancelled` / `completed` / `failed` 终态事件后再正常结束流。
- **单活动会话并发约定**：一个进程同时仅持有一个活动 `CodingSession` 和一个前台运行任务。生成期间允许查看或新建/重命名话题，再次提交普通消息返回 `409 agent_busy`，忙碌时切换或新建会话返回 `409 agent_busy`。
- **双模式前端兼容**：不带 `#token=` 打开页面时自动探测 `GET /api/health` 进入 `service` 模式；终端输入 `/map` 打开的 `#token=...` 链接继续走原有只读 `map` 协议，隐藏聊天输入框。
- **不做的范围**：不实现多用户、远程 TUI 同步、任意项目目录选择器、浏览器 Shell、模型市场、API Key 导入、Redis/Celery/Kubernetes 或数据库 schema 迁移（继续使用 schema 6）。

## 2. 依赖安装与前端构建

从仓库根目录执行：

```bash
# 安装 Python 依赖（fastapi 与 uvicorn 已包含在主依赖中，也兼容 -e ".[web]"）
python3 tools/install_dependencies.py --python-only

# 安装前端依赖并导出静态页面到 web/out
python3 tools/install_dependencies.py --web-only
npm --prefix web run build
```

确认已生成 `web/out/index.html` 与 `dependencies/python/.venv/bin/octo-web`。

## 3. 配置文件与凭据隔离

### 3.1 `web.env`（systemd 环境变量）

不要把原有的 Bash 风格 `model.env`（含 `export` 与 `%q` 转义）直接用作 systemd `EnvironmentFile`。按 `deploy/web.env.example` 创建 `/home/octo/.octo/web.env`（权限设为 `600`）：

```ini
OCTO_WEB_HOST=127.0.0.1
OCTO_WEB_PORT=18300
OCTO_WEB_CWD=/home/octo/workspace
OCTO_WEB_GRACEFUL_TIMEOUT=15
# 启用公网 Caddy 后追加真实外部 Origin：
# OCTO_WEB_ALLOWED_ORIGINS=https://octo.example.com
# OCTO_WEB_TRUSTED_PROXIES=127.0.0.1,::1
# DEEPSEEK_API_KEY=REPLACE_WITH_REAL_KEY
```

模型凭据仅保存在服务端进程环境变量或 `$OCTO_HOME` 凭据存储中，不暴露给前端静态产物或 `/api/health`、`/api/state`。

## 4. 两阶段启动与固定网址部署

### 阶段一：固定回环端口 + SSH 隧道（无域名时先交付此模式）

在服务端（或先在 tmux 内测试）运行：

```bash
/home/octo/apps/octo/dependencies/python/.venv/bin/octo-web \
  --host 127.0.0.1 --port 18300 --cwd /home/octo/workspace
```

本机通过固定端口转发访问：

```bash
ssh -i ~/.ssh/octo-ecs -N -L 18300:127.0.0.1:18300 octo@47.97.86.80
```

随后浏览器打开 `http://127.0.0.1:18300/`。

### 阶段二：systemd 常驻 + Caddy HTTPS 与 Basic Auth

1. 安装并启用 systemd 服务（已确认原独立 TUI 已正常退出）：

```bash
sudo cp deploy/octo-web.service /etc/systemd/system/octo-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now octo-web
```

2. 安装 Caddy，生成密码哈希并配置 `/etc/caddy/Caddyfile`（参考 `deploy/Caddyfile.example`）：

```bash
caddy hash-password
# 将域名与哈希填入 /etc/caddy/Caddyfile，并在 /home/octo/.octo/web.env 设置 OCTO_WEB_ALLOWED_ORIGINS=https://你的域名
sudo systemctl reload caddy
sudo systemctl restart octo-web
```

3. 常用运维命令：

```bash
# 查看状态与日志
sudo systemctl status octo-web
journalctl -u octo-web -f

# 重启与停止
sudo systemctl restart octo-web
sudo systemctl stop octo-web
```

## 5. 上线前备份、历史恢复与失败回退

### 5.1 停止旧 TUI 与备份 SQLite（必须包含 WAL）

禁止仅在写入期间直接 `cp octo.db` 而遗漏 WAL。先停止写入进程，或通过 Python `sqlite3.Connection.backup()` 执行一致性在线备份：

```bash
# 1. 检查监听端口、tmux 与旧进程
ss -tlnp
tmux ls || true
pgrep -af 'octo|uvicorn|caddy' || true

# 2. 若需停止旧 TUI，切勿只做 tmux detach；进入会话正常退出 Octo，等待记忆与 provider 收尾。

# 3. 使用 sqlite3 backup API 备份 octo.db（即使在运行中也能合并 WAL 得到一致快照）
/home/octo/apps/octo/dependencies/python/.venv/bin/python -c "
import sqlite3, time
src = sqlite3.connect('/home/octo/.octo/octo.db')
dst = sqlite3.connect(f'/home/octo/.octo/octo.db.bak.{int(time.time())}')
src.backup(dst)
dst.close()
src.close()
"
cp -a /home/octo/.octo/memory.md /home/octo/.octo/memory.md.bak 2>/dev/null || true
cp -a /home/octo/.octo/providers.json /home/octo/.octo/providers.json.bak 2>/dev/null || true
```

### 5.2 恢复历史与切回终端

- `octo-web` 启动时默认自动加载 `--cwd` 下最近一次 SQLite 会话，也可传 `--session <session_id>` 指定会话或在前端下拉列表切换。
- 如需临时改回终端 TUI 操作同一数据库：先执行 `sudo systemctl stop octo-web`，再运行 `octo`，禁止同时由 `octo-web` 和独立 TUI 持有同一活动会话。
- 本次未改动 SQLite schema 6，若 `octo-web` 异常需要回退：

```bash
sudo systemctl stop octo-web
sudo systemctl disable octo-web
# 直接使用原有终端命令恢复会话，保留同一份 octo.db 数据：
/home/octo/apps/octo/dependencies/python/.venv/bin/octo --cwd /home/octo/workspace
```

## 6. 验证记录（区分环境与范围）

| 验证类别 | 覆盖范围与条件 | 结果 |
| --- | --- | --- |
| 离线基础与代码规范检查 | `tools/verify.py`（含模块边界、禁止 docstring、中文注释、`web_api` 编译导入与 CLI 检查） | 通过（11 项） |
| 离线单元与集成测试 | `tests/` 全量 `unittest`（临时 `OCTO_HOME`/SQLite、`FakeProvider`/可控桩 provider、默认关闭异步记忆，记忆顺序测试显式注入提取器） | 通过（见最终报告总数） |
| 真实本地 Uvicorn + 反向代理验证 | `tests/web_api/test_sse_and_reconnect.py`（真实本地 Uvicorn TCP 服务 + 自建真实 HTTP Basic Auth 流式代理，覆盖断连续跑、重连快照、慢订阅者隔离、401/403 拦截、实时 SSE） | 通过 |
| 真实 `octo-web` 子进程验证 | `tests/web_api/test_cli_process.py`（真实子进程加载导出的 `web/out/index.html`、`/api/health`、`/api/state`、`/api/snapshot` 与路径越权拦截） | 通过 |
| 前端检查与生产构建 | `npm --prefix web test`、`npm --prefix web run typecheck`、`npm --prefix web run build` | 通过（14 项测试 + 类型检查 + 静态导出） |
| 真实 DeepSeek 模型冒烟 | 付费公网 API 调用 | 未在本机离线环境执行（需实机凭据后人工冒烟） |
| 阿里云 ECS (`47.97.86.80`) 实机部署 | 本机使用 `~/.ssh/octo-ecs` 探测连接在 `kex_exchange_identification: Connection closed by 47.97.86.80 port 22` 被远端关闭，且尚未提供域名与备案/DNS 条件 | 未执行实机部署（待 SSH 恢复、备份旧进程并确认外部入口后执行） |
