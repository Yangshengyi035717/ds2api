# 长流断开修复与跨平台排查（2026-10-23）

## 已捕获的故障

先前线上诊断记录位于 `data/diagnostics/zai-long-stream-diagnosis-20261023.json`
（本地忽略目录，不包含在提交中）。该记录显示：

- 实际模型为 `glm-5.3-flash`，不是 DeepSeek。
- Sub2API 有客户端等待约 125 秒后取消的 499；最终一次上游 HTTP 200
  在约 104 秒后发生 curl (18) / IncompleteRead，没有正常终止信号。
- 当时两个 Nginx 站点读写超时均为 300 秒且关闭 buffering，不能将其
  简化为“线上 API 固定只支持两分钟”。
- z.ai 账号被传输异常标成 error；仅凭该异常不能证明凭据失效。
- Responses 转换器累积 reasoning 而不实时转发；思考期间缺少事件，
  进一步触发下游空闲超时。同步生成器还在 ASGI 事件循环内读网络。

## 本次修复

1. `responses_format.py` 实时发送推理摘要 delta，使用稳定 item ID / index /
   sequence number；只在明确成功后发送 `response.completed`。
2. `server.py` 检查 Chat finish reason 和 `[DONE]`。缺失终态、传输异常、
   错误帧或无效 JSON 均输出 `error` + `response.failed`，不伪造成功。
   UTF-8 增量解码支持字节与 CRLF 跨 chunk 边界。
3. `streaming.py` 每 15 秒空闲输出心跳：Chat SSE 注释、Responses
   `response.in_progress`、Anthropic `ping`。心跳不产生模型文本或 token 用量。
   所有流响应带 `X-Accel-Buffering: no`。
4. 读上游、非流式请求与账号排队/会话初始化移出事件循环。排队与活跃流
   使用不同 worker 执行通道，避免排队占满读取 worker 后相互等待。
5. 单流最多一个 pending read，不取消/重建正在读的上游连接来发送心跳。
   客户端取消或 socket 发送失败都会关闭流并释放账号；若正在阻塞读取，
   等读取结束再关闭，避免跨线程 close 或提前复用忙账号。
6. 网络故障统一为临时上游错误，不污染账号凭据状态；非流式返回
   HTTP 503 + Retry-After，已发 HTTP 200 的流通过 SSE 报错。
7. DeepSeek/Kimi 已输出内容后不重放同一生成，避免重复正文与工具调用。
   Kimi token 保存异常仍释放账号与并发槽。

## 各平台检查结果

| 平台 | 成功终态 | 本次检查 |
|---|---|---|
| DeepSeek | FINISHED（含 message status/BATCH）或非 resume 的 close | 流式/非流式补齐，传输异常归一；已输出不重试 |
| z.ai | done=true 或 phase=done | 流式/非流式补齐，curl 中断不再污染账号 |
| Kimi | Connect JSON done | 半截流不算成功，已输出不重试 |
| 千问 | data.status=complete | 缺终态/传输错误转临时故障 |
| 混元 | [DONE] | 缺终态/传输错误转临时故障 |
| 豆包 | SSE_REPLY_END，观察到的正常 end_type=1 | 非正常/缺终态不算成功 |
| 文心 | message.metaData.state=generate-complete | 区分推理组件结束和整个消息完成；无 state 时兼容已结束 markdown-yiyan |
| MiMo | finish / [DONE] | 原有校验保留，纳入公共心跳/释放/超时与回归矩阵 |

这是**代码路径和离线协议样本**检查，不表示逐个线上账号均已实测可用。
旧账号 error 状态不会被无条件清除；部署后需根据实际错误重新验证，避免把
真正失效、封禁或被限流账号重新投入请求。

## 超时语义

`UPSTREAM_STREAM_IDLE_TIMEOUT` 默认 300 秒，限制在 30–1800 秒。
连接建立上限 15 秒。基于当前安装的 curl_cffi 实现，stream=True 使用
LOW_SPEED_TIME（连接+读取预算），不是活跃 token 流的总时长上限。
DeepSeek 缓冲式非流式请求用同一数值作为总耗时预算；健康检查保持原配置。

不要把下游心跳误认为上游账号“续期”或让上游永不超时：
- 上游主动关闭、代理故障、账号限制仍会导致请求失败，区别是现在明确报错。
- 取消发生在阻塞读取时，账号可能保持 busy 至本次读取返回/超时；不会提前复用。
- 心跳开始于会话创建之后；极长排队/初始化的首字节超时仍受客户端/网关约束。
- 部署链路如果过滤心跳、强制总时长或缓冲响应，仍需单独核查。

## 可复现验证（不使用真实账号）

```powershell
Set-Location "F:/codex_project/deepseek-web2api-free"
& ".venv/Scripts/python.exe" -m pytest -q --disable-warnings --tb=short
& ".venv/Scripts/python.exe" -m tests.test_long_stream_http --seconds 130
```

扩展 HTTP 测试只绑定 loopback 的临时端口，启动一个最小应用并 mock
账号/上游，在 130 秒内不发送上游正文，同时发起成功和失败两个请求。
检查客户端 30 秒读超时不会触发、心跳持续、健康检查不阻塞、推理立即转发、
成功/失败终态正确、两个账号都释放。进程退出时关闭临时服务。

测试覆盖：八个平台 × 流式/非流式 × 正常/缺终态/传输中断；三种 API
格式的错误映射与账号释放；取消与 socket send 失败；部分输出不重放；
UTF-8/CRLF 半包；Kimi 持久化失败；超时配置边界。

## 发布状态

2026-10-23 本地验证结果：
- 完整回归 `595 passed in 20.09s`；`git diff --check` 无空白错误。
- loopback 成功请求 130.232 秒，以 `response.completed` 结束。
- loopback 模拟断流请求 130.203 秒，以 `response.failed` 结束。
- 两个请求最大事件间隔分别 15.017 / 15.016 秒，各 9 个 in_progress 事件
  （包括初始事件）；并发健康检查延迟 0.085 秒。
- 两个模拟账号均释放，真实上游请求数为 0。

以上为本地验证记录，不代表逐个线上账号均已实测可用。部署需保留线上账号凭据、
代理和生产配置，并检查容器健康、代码版本及长流终态；实际提交、镜像和回滚信息
单独记录在忽略目录 `data/diagnostics/` 的发布清单中，避免把运行态信息写入源码。
