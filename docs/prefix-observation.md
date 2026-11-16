# 请求前置观测

在启动 Octo 前设置 OCTO_PREFIX_LOG 为 JSONL 日志的绝对路径。目录需已存在。
OpenAI-compatible 的 Chat / Responses 请求构建完成后记录 system_hash、tools_hash、prefix_hash。
哈希覆盖发送载荷的 system/developer 消息或 instructions，以及工具定义，保留当前键序和工具顺序。
日志带 model 和 session_id，不含原始正文或凭据。

此切片仅观察当前组装结果。日志中的一致性表示本地前置一致，服务端缓存用量需另外检查。
环境信息、项目文件或工具变化可能造成哈希变化。历史压缩改变共同前缀，但不一定改变这里观察的 system/tools。
构建结果和请求载荷均不会被观测函数改写。删除 OCTO_PREFIX_LOG 可停止日志。
