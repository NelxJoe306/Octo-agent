# Octo Web（话题地图与单用户服务前端）

Octo 支持两种连接方式共用同一份 `web/out` 静态构建产物：

1. **FastAPI 常驻服务模式（`octo-web`）**：经由固定端口或 HTTPS 反向代理（如 Caddy Basic Auth）访问，支持查看/切换或新建会话、浏览对话轮次树、创建/切换/重命名话题、在当前浏览话题发送消息、查看流式输出与工具调用、停止生成，以及断线重连后从快照无缝续看（浏览器关闭不停止服务端任务）。
2. **终端 `/map` 只读模式**：继续保留独立终端命令 `/map` 打开的本机 `#token=...` 只读话题地图协议，隐藏聊天输入栏与会话切换入口。

模型凭据、OAuth 与工具配置继续只保存在服务端或本机 `$OCTO_HOME` 中；Web 前端不保存密钥，也不恢复项目管理或模型市场等桌面工作台功能。

## 构建与启动

首次构建前端静态产物，在仓库根执行：

```bash
python tools/install_dependencies.py --web-only
npm --prefix web run build
```

### 服务模式启动（推荐）

```bash
octo-web --host 127.0.0.1 --port 18300 --cwd /home/octo/workspace
```

默认仅允许绑定回环地址，公网发布与 Basic Auth 接入步骤见 [Web 服务运维说明](../docs/web-service.md)。

### 终端 `/map` 兼容模式

```bash
octo
```

在终端内输入 `/map`，打开带 `#token=...` 的本机页面；关闭浏览器不影响终端任务，退出终端会关闭接口。

## 交互与特性

- **话题与轮次树**：按深度分层与父子连线展示主干（`kind=trunk`）和分支，支持搜索、折叠、缩放、「适应窗口」及树/记录分隔条拖拽与键盘调宽。
- **浏览与发送分离**：点击历史轮次或「定位原文」可前后双向分页（每页最多 100 条）并支持「加载后续记录」/「回到最新记录」；发送消息明确携带当前浏览的 `topic_id`，生成期间切换浏览话题不会改变原回答的归属。
- **渲染安全**：助手回复支持 Markdown（GFM 表格/列表/代码块）与 KaTeX 公式（`$...$`、`$$...$$`、`\(...\)`、`\[...\]`），禁用原始 HTML 执行；用户输入与工具输出保持纯文本。

## 开发与验证

```bash
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run build
```

`npm --prefix web run dev` 仅用于开发静态页面；生产运行直接由 `octo-web` 或 `/map` 托管 `web/out/`，无需独立 Node 进程。

## 实现位置

- `src/web_api/`：FastAPI 应用工厂、lifespan、`WebRuntime`、SSE 事件流、安全中间件与 `octo-web` CLI。
- `src/agent_core/session/topics.py`：SQLite 话题表、稳定消息定位、请求与消息原子归属。
- `src/terminal_app/session.py`：冻结请求、`prompt(..., topic_id=...)`、预算门控与长期记忆编排。
- `src/terminal_app/topic_web.py`：终端 `/map` 兼容本机回环服务。
- `web/app/page.tsx`、`web/lib/topic-map.ts`：双模式适配、轮次树、流式事件归并及 Zod 响应校验。

更详细的部署与运维说明见 [Web 服务运维说明](../docs/web-service.md)；话题树与轮次布局交互背景见 [话题地图设计](docs/topic-map.md)。
