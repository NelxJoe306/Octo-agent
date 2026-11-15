# 目录与模块约定

## 职责与依赖方向

| 目录 | 职责 | 可依赖 |
| --- | --- | --- |
| src/octo_agent | 消息/工具协议、主循环、上下文门控与共享会话存储 | 标准库和基础库 |
| src/octo_ai | 模型服务适配器与请求预算协议 | octo_agent |
| src/octo_memory | 长期事实/任务/收据、后台提炼、单向 Markdown 导出 | octo_agent、octo_ai 的协议 |
| src/octo_coding | CLI/TUI、工具、provider 生命周期和应用路径适配 | 上述三个模块 |
| web | 独立构建的辅助话题窗口 | 本机 HTTP 接口，不导入 Python 源码 |

tests/core/test_module_boundaries.py 检查依赖方向，阻止基础模块反向导入终端层。长期记忆不再位于 octo_agent.memory 或 octo_coding.memory_runtime；所有调用者一次迁移到 octo_memory，不保留旧 Python 包转发层。共享 SQLite schema 仍由基础会话存储管理，不改变现有原子提交。

## 依赖与安装

Python 安装到 dependencies/python/.venv，Web 包安装到 dependencies/web/node_modules。根 .venv 与 web/node_modules 通过目录链接供原有工具使用；Windows 使用 Junction，其他系统使用符号链接。运行安装器前退出占用该环境的终端/构建进程。

```powershell
python tools/install_dependencies.py --python-only
python tools/install_dependencies.py --web-only
```

需要 uv 和 npm。可用 --offline 使用已缓存构建工具与包；全新安装通常需要联网。安装器保留已有非目标目录并报错，不静默删除它们。受版本管理的依赖声明仍是 pyproject.toml 和 web/package.json/package-lock.json，安装区域中的清单副本和包不进 Git。源码布局不依赖本机绝对路径，EC2 可正常 pip install 项目；Linux 的解释器路径为 dependencies/python/.venv/bin/python。

## 测试与维护脚本

- tests/core：上下文、压缩、模块边界。
- tests/providers：模型窗口与输出预算兼容。
- tests/storage：SQLite 会话与工具产物。
- tests/memory：记忆事务、后台提炼和 Markdown 镜像。
- tests/sessions：主干/话题和模型恢复。
- tests/test_smoke.py：原有 11 项基础检查；tools/verify.py 是启动入口。
- web/tests：原 lib/*.test.ts 的前端测试。

```powershell
.\.venv\Scripts\python.exe tools/verify.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -t . -p 'test_*.py' -q
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run build
```

完整 Python 发现现在也包含那 11 项基础检查。非记忆测试继续在夹具中显式禁用后台提炼，记忆测试显式启用，无需外部环境开关。

## 源码、指令与运行数据

系统提示词中的身份、职责和风格未抽取或更改。仓库约定沿用现有 AGENTS.md，不新增角色 agent.md。Octo 状态仍位于 OCTO_HOME：octo.db 是权威，memory.md 是自动生成的只读视图，不读取它作为新指令或事实。详见 [Markdown 镜像](memory-mirror.md)。
