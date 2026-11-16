# 固定前置与 Plan 模式

## 请求组织

默认将身份、工具规则、项目指令和技能说明组成系统前置，工具列表按名称排列，Schema 对象键稳定排序。Schema 中的数组保留原顺序。
日期（ISO 日期）、工作目录、环境、话题归属、偏好快照和 mode 放到历史后的运行提醒。
用户明确提供的整段 system 文本保留其语义，程序不会解析或删除其中的自定义动态字段。
会话摘要仍留在历史投影中的摘要位置。memory.md 是数据库镜像，未参与请求组装。

固定前置以配置版本为范围。项目指令、技能和工具变化会产生新指纹。压缩会改变历史共同前缀，即使 system/tools 指纹相同。
服务端管理实际计算缓存，本机观测指纹和保留已构建的会话 system。

## 日志与开关

启动前设置 OCTO_PREFIX_LOG 为现存目录下的绝对 JSONL 路径。适配器记录实际 Chat/Responses 载荷的前置哈希，响应结束后记录 request_metrics。
指标包括 request_id、model、session_id、优化开关、mode、system_hash、tools_hash、prefix_hash、input_tokens、cached_input_tokens、first_output_ms、first_text_ms、elapsed_ms、重试数及结束状态。
首个输出可能是思考或工具调用，first_text_ms 则从正文文本增量计时。时间覆盖适配器请求过程，和终端端到端等待时间有区别。
未报告缓存用量时 cached_input_tokens 为 null。正文、工具参数及凭据不写日志。

设置 OCTO_DISABLE_PREFIX_CACHE=1 后重新启动会话，可使用旧的日期/目录位置及工具序列化顺序。
该开关控制本地请求组装。服务端缓存由服务商管理。
前缀组装造成消息丢失、工具历史不完整时关闭优化后重开会话；模式执行检查出错时撤回模式切片。性能变化通过交替重复测量判断。
此双路径用于评测和回退，评测验收后由后续清理提交删除旧路径，对照保留基线提交。当前先保留开关，等待使用者验收数据。

## 模式与执行

默认编码工具集合固定包含 enter_plan_mode 和 exit_plan_mode。两者直接切换 execute / plan，无额外确认状态。
模式变化通过现有 CustomEntry（octo.mode）持久化，不新增数据库 schema。
在下一次模型调用的尾部提醒中注入模式。执行前每个工具调用都重新检查最新状态，同一批中进入 Plan 后后续写入也会被阻止。
内置 read、read_artifact 声明 read 权限，模式工具声明 control。write、edit、通用 Shell 以及未声明权限的扩展工具在 Plan 下被拒绝。
扩展包装器保留本地权限元数据，该元数据不进入发送给模型的工具 Schema。
拒绝产生正常的错误工具结果，模型可以继续处理。已有工具前置检查继续生效。

模型可以自行调用 exit_plan_mode，因此保证范围是 Plan 状态下拒绝禁止操作。execute 状态恢复正常工具执行。
显式传入 CodingSessionConfig.tools 的调用方继续拥有其完整工具集合，需自行决定是否添加模式工具；默认 CLI 使用内置集合。

## 评测

eval/prefix_cache/run.py 固定短历史、22 轮长历史、模式变化三组各 20 个请求，两种组装路径各重复五次，共 600 次独立流式请求。
固定历史来自合成消息，真实 provider 只生成最后的简短 OK 响应，输出上限 32。模式组包含 execute / plan；日期按三个固定日期变化，偏好快照按三个版本变化。
每次两路径的输入内容、历史和输出设置相同，交替调用先后顺序。普通前置内容和历史可在不同样本之间自然复用。
缓存比例按成功且没有重试的请求计算缓存读取 token / 总输入 token。延迟先对同一样本五次取中位数，再报告各样本中位数的分布。
第一次测量不保证服务端冷缓存，服务商缓存状态、排队及网络影响会在结果交接中说明。

运行：

```powershell
.\.venv\Scripts\python.exe eval/prefix_cache/run.py --model deepseek-flash --output prefix-run.jsonl
```

脚本生成 JSONL、CSV、trace.jsonl 和 summary.json。使用全新输出文件名，真实评测会消耗 API 额度。
