# memory.md：单向可读镜像

SQLite 继续负责事实、来源、任务、学习进度和收据的原子提交。memory.md 由当前数据库生成，不参加事实事务，不作为提炼或召回输入，也没有隐式 Markdown 导入。

## 导出行为

实现位于 src/long_term_memory/mirror.py，公开接口为 `MemoryMirror(store, path).export()`，返回文件是否发生变化。应用将路径设为 OCTO_HOME/memory.md；默认目录由现有 OctoPaths 管理。

启用后台记忆时，worker 启动先修复镜像，成功提交后再更新。只在 SQLite 事务完成后读库和发布文件；文件 I/O 失败单独记录 last_export_error，/session 显示诊断，已提交的事实/进度/收据不回滚，也不会因导出失败重复提炼。下一次提交、worker 重启或显式导出可恢复。

内容按有效偏好、有效通用事实、折叠历史分节，每项带事实 ID、原文日期、来源会话/消息与角色。更正/撤回保留历史和替代 ID。正文换行折为单行，HTML 和 Markdown 控制字符转义。相同数据库快照生成相同内容，不更新文件时间戳。

独占导出锁覆盖“读快照→发布”，多进程不会让先读到的旧快照最后覆盖新文件；Windows 使用 msvcrt，其他平台使用 flock。内容写入同目录临时文件、flush/fsync 后原子替换。异常清理临时文件；锁由操作系统在进程退出后释放。镜像允许滞后或丢失，数据库不依赖镜像恢复。

## 手动重建

```powershell
.\.venv\Scripts\python.exe tools/export_memory.py --home <Octo状态目录>
```

目录中需有 octo.db。命令不启动 Agent、不调用模型。手工修改 memory.md 会在下次导出时被数据库内容覆盖；若要更正事实，继续走正常记忆提炼/提交接口。显式人工导入和 TTL 属于后续独立功能，本次不新增 schema 或改变事实有效性规则。

测试覆盖幂等导出、原始来源、历史保留、并发发布、文件写入失败、手改不回流，以及后台导出失败后的启动修复。所有测试使用临时数据库和目录。
