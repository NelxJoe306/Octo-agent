# 长期记忆：持久化底座

本切片提供 SQLite 事实库、提炼任务、学习进度和处理收据。存储接口已接入独立后台提炼与用户偏好召回，详见 [异步记忆说明](async-memory.md)。事实的项目/环境适用范围仍待确认；数据库级查询不代表允许把全部事实自动注入任何会话。

## 公开接口

实现位于 `octo_agent/memory/`，入口为 `MemoryStore(SQLiteSessionDatabase(path))`。所有数据库操作在线程中执行。

- `enqueue(session_id, through_entry_id)`：冻结从学习进度后一条至指定原文条目的连续序号范围。重复端点返回原任务；同会话只允许一个未完成任务，不同端点须等待，不能重叠处理。新增尾部留到下次。
- `ready_jobs(limit=100)`：发现待领取及租约已到期的任务，最多 1000 条；不暴露仍有效的运行任务。`get_job` 查看状态，`progress` 查看已学习的原始日志序号。
- `claim(job_id, lease_seconds=300)`：事务内领取，生成新的令牌并累加尝试次数。租约最长 3600 秒；没有续租接口，后续 worker 应按任务规模选择时长。到期可重新领取，旧令牌不能提交或释放新任务。
- `sources(job_id)`：只读取冻结范围内原始 MessageEntry，不把压缩摘要或自定义条目当作证据。返回消息正文、原文时间戳、来源会话/条目 ID，以及请求中冻结的话题 ID 和路径。工具消息保留其实际持久化内容，不自动展开工具产物正文。
- `apply(lease, operations)`：校验操作及来源，同事务提交事实变化、进度、任务 applied 状态和不可改写收据。空结果同样推进进度。相同令牌及相同操作的重放返回原收据；变更内容或旧令牌重放报冲突。
- `release(lease, error)`：有效租约的处理者主动释放任务，保留最多 2000 字符错误信息，进度不动。异常退出后无需全局重置，等待租约到期重新领取。
- `facts(source_session_id=None, include_inactive=False, category=None, limit=None)`：查询有效事实，或显式查看旧版本。来源会话筛选是审计过滤，不是项目适用范围。`receipt(job_id)` 返回原操作和新事实 ID，未提交则为空。

`pending_job(session_id)` 查询未完成任务；`unlearned(session_id, limit=100)` 从学习进度后读取最多 1000 条原始消息，供编排器选择下一批范围。facts 可按 category 筛选并限制条数，有限查询优先返回最近事实。

## 事实与证据

操作模型为 `AddFact`、`SupersedeFact`、`RetractFact`，禁止未知字段，必须带冻结范围内的真实消息 ID。新增和更正保留原文时间戳与来源；更正生成新事实、旧事实标记 superseded 并指向新 ID；撤回标记 retracted，原文与旧事实不删除，撤回证据保存在收据中。目标必须仍有效，否则整个批次回滚。

`AddFact.category` 可为 fact 或 preference。preference 的全部证据必须是真实 user 消息；更正继承目标类别，撤回偏好同样要求用户证据。通用事实允许助手或工具消息，但保留其来源身份；来源校验不能证明语义正确，下一切片仍需校验提炼内容。不同会话可以通过明确事实 ID 更正同一数据库中的事实，本切片不自动判断哪些事实属于同一对象。

## 一致性与恢复

```text
pending → claim → running → apply → applied
                    ├─ release → pending
                    └─ 租约过期 → 新令牌重新 claim
```

任务范围在入队时持久化；BEGIN IMMEDIATE 串行领取和提交。处理操作、学习游标、收据在同一事务中提交，部分操作失败或最终收据写入失败都回滚。重启后可通过 ready_jobs 发现待办；租约未到期时不会夺取仍可能正常运行的任务。调用方取消等待线程，不等于 SQLite 操作已取消，应通过任务/收据判断是否提交；重复同一操作不会再应用一次。

## 升级与验证

会话库升级到 schema 5，增加 memory_progress、memory_jobs、memory_facts、memory_receipts；既有会话、话题和工具产物不变。旧程序不支持 schema 5。首次启动前应关闭旧进程并备份 SQLite（或使用 SQLite backup）；JSONL 不导出记忆及工具产物，不能用它替代完整数据库备份。本次开发只迁移临时测试库。

运行 `python -m unittest tools.test_memory_storage -q`。测试使用真实临时 SQLite、独立子进程退出和数据库写入故障，覆盖重启、租约竞争、旧令牌拒绝、空结果、幂等重放、批次回滚、原始证据校验和 schema 4 迁移。没有调用真实模型 API。
