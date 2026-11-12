# Octo Web

TypeScript + Next.js 的桌面风格 Web 前端，源码集中在本目录。

## 运行

在 `D:\Octo\web` 执行：

```powershell
npm ci
npm run dev
```

打开 http://127.0.0.1:18300 。开发服务只绑定本机。

```powershell
npm test
npm run typecheck
npm run build
```

生产构建静态导出到 `out/`，可由普通静态服务器托管。依赖精确版本和完整传递依赖记录在 `package.json` 与 `package-lock.json`。

## 这一版可以体验什么

- 左侧会话列表、搜索、置顶、新建，以及每会话独立草稿。
- 中间消息时间线、演示流式回复、停止、复制；切换会话不改变回复归属。
- 底部输入、模式和模型选择；已去掉项目分组与项目/运行环境条。附件仅预览文件名。
- 右侧可收起的对话树预留窗口；没有实现树、节点或分支操作。左右边界支持拖拽调宽、方向键调节和双击复位。
- 设置中的主题、字号、输入提示和演示速度，以及选中、悬停、按压、键盘焦点和减少动态状态。

当前是**本地交互预览**：回复来自前端定时器，没有调用模型、工具或 Python 后端；会话、界面偏好与当前模型选择存在页面内存，刷新恢复默认。模型定义来自用户选择的本机 JSON 文件。它没有接入已有 SQLite 会话存储。

## 实现位置

- `app/page.tsx`：页面布局、交互与演示流式调度。
- `lib/chat.ts`：会话状态机、固定请求归属和停止守卫；`lib/chat.test.ts` 验证行为。
- `components/settings.tsx`：原生 dialog 与设置控件。
- `components/local-models.tsx`、`lib/models.ts`：本机配置文件访问、编辑、校验和模型能力。
- `components/model-picker.tsx`：按模型名称/ID 搜索、选择与专属推理强度。
- `components/resize-divider.tsx`：Pointer Capture 拖拽及键盘分隔条。
- `styles/tokens.css`、`app/globals.css`：样式、状态和动效。
- [桌面设计与实现记录](docs/desktop-design.md)。
- [参考源码证据](docs/reference-evidence.json) 与 [核对工具](tools/audit_reference.py)。

## 核对参考证据

```powershell
python tools/audit_reference.py "参考预览根目录" --output docs/reference-evidence.json --check
```

会话页参考用户提供的 Codex 截图；设置与控件参考 agentes。原预览包没有作为运行依赖，也没有被修改。


## 本机模型配置

打开「设置 → 模型 → 打开配置文件」，选择自己的 JSON。可从 [示例文件](docs/octo-models.example.json) 复制后修改；示例名称与 ID 只是占位，推理能力也需要按实际模型填写。模型配置不经过服务器，也不写入 localStorage。

- `name` 为显示名称，`id` 为调用时的模型 ID，`provider` 为来源名称，`baseUrl` 为可选 HTTP/HTTPS 地址。
- 只给支持强度调节的模型添加 `reasoning.options` 与 `reasoning.default`；选项 value 为实际参数值，label 为显示文字。
- 「应用到预览」只更新当前页面；「保存配置文件」才写回本机文件。保存前会检查文件是否被外部修改。
- 支持 File System Access 的浏览器通过文件选择器授予读写；不支持时通过文件导入及显式下载链接导出。重新编辑会使旧下载链接失效，避免保存旧内容。
- 刷新后需要重新打开文件。当前尚未接入 Python 配置加载器、凭据或真实模型调用；本文件是前端配置合同。

浏览器文件 API 参考：[MDN FileSystemFileHandle](https://developer.mozilla.org/en-US/docs/Web/API/FileSystemFileHandle)。


## 窗口适配

按容器实际宽度切换：760px以上保持三栏并为聊天保留至少320px；600–759px左栏收进菜单；不足600px右窗采用独立视图并提供返回对话。窗口变矮时收紧间距，避免输入框越界。窗口大小变化不改会话与生成结果归属。
