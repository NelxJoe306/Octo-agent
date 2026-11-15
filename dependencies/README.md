# 本机依赖

- `python/.venv/`：Python 运行依赖与启动命令。
- `build/`：构建中间文件和包元数据，仅目录占位进 Git。
- `web/node_modules/`：Web 依赖；此目录的 package.json/package-lock.json 是安装时生成的副本，源清单在 web/。

从仓库根运行 `python tools/install_dependencies.py`，可用 --python-only / --web-only。离线模式要求构建工具及包已缓存。
安装器在根 .venv 和 web/node_modules 建立目录链接供原有工具使用；已有的非目标目录不会被删除或覆盖。Windows 使用 Junction，Linux/macOS 使用符号链接。
依赖版本声明仍由根 pyproject.toml 和 web/package.json/package-lock.json 管理，安装包及缓存不进 Git。
