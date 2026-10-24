# Codex Responses 兼容

## 目标

DS2API 将 Chat Completions 上游转换为 Codex 使用的 Responses API。兼容层必须同时
处理传输协议、工具模型、上下文预算和账号调度，不能只返回一个相似格式的 SSE。

## 请求转换

- Codex 的真实请求包含 `function`、`namespace`、`custom` 和 `web_search` 工具。
- `CODEX_TOOL_MODE=core` 默认只暴露编码核心工具：
  `exec_command`、`write_stdin`、`view_image`、`request_user_input`、
  `apply_patch`、`get_goal`、`create_goal`、`update_goal`。
- `CODEX_TOOL_MODE=all` 会把 namespace 子工具摊平为
  `namespace__tool`，响应时再还原 `namespace` 与裸工具名。
- `custom` 工具在上游使用 `{"input":"..."}` 参数，在响应中还原为
  `custom_tool_call` 生命周期。
- Codex 每次都会声明 `web_search`。默认 `CODEX_WEB_SEARCH_ENABLED=false`，
  防止每个 coding 请求都意外开启上游联网搜索。

## 流式事件

转换器按以下顺序维护 item：

1. reasoning item 在首个 reasoning delta 前创建。
2. 第一个正文 delta 关闭 reasoning item，再创建 message item。
3. tool call 出现时立即创建对应 item 并实时发送参数 delta。
4. 流结束时关闭所有 item，再发送 `response.completed`。
5. 失败时发送 `error` + `response.failed`，不发送 completed。

`response.output_text.*`、content part 和 tool 参数事件包含 Codex 严格解析所需的
`logprobs`、`call_id`、`name`、`output_index` 等字段。

## 模型目录

带 `client_version` 查询参数或 Codex User-Agent 的 `GET /v1/models` 返回：

```json
{"models":[{"slug":"deepseek-chat","display_name":"DeepSeek Chat","context_window":128000}]}
```

完整 descriptor 还包含 reasoning、tool、truncation 和 API 能力字段。Codex
配置应使用：

```toml
model_catalog_url = "https://dsapi.example.com/v1/models"
```

这样客户端不会使用错误的 fallback 上下文窗口，并会按 provider 的实际预算执行
自动压缩。

自定义模型没有 Codex 内置的压缩默认值，因此目录还必须返回有限的
`auto_compact_token_limit`：

- 128K 上下文模型：64K 压缩阈值
- MiMo 32K 输入预算：12K 压缩阈值。MiMo Web 自身还会注入约 19K token
  的隐藏提示，因此 64K 甚至 1M 的 catalog 都会高估实际可用空间。
- `effective_context_window_percent=85`
- 工具输出按 `tokens` 截断，单项上限 10K

如果旧线程已经超过硬上限，Codex 仍会要求新建线程或手动 `/compact`；
自动压缩只对尚未耗尽且重新加载了模型目录的会话生效。Codex 请求不依赖客户端
是否正确触发压缩：DS2API 会在每轮请求前截断超大工具调用参数，并按 provider
预算合并旧对话。MiMo 为 12K，文心为 32K，DeepSeek/Kimi/GLM 等为 64K。
携带工具的 Codex 请求还会注入本地执行约束：工具在当前用户电脑的工作区运行，
创建或编辑文件必须调用工具；禁止把整份文件作为聊天正文让用户下载或手工保存。
大文件要求按约 8K 字符分块写入，避免单次工具参数过大导致流中断。

## Remote compaction v2

Codex 的自动压缩请求仍是 `POST /v1/responses`，但会在 `input` 末尾附加：

```json
{"type":"compaction_trigger"}
```

DS2API 会：

1. 提取当前对话中非静态的部分，向上游生成一份结构化压缩摘要。
2. 如果摘要请求失败，使用本地保留首尾内容的降级摘要，避免压缩回合直接失败。
3. 输出恰好一个 `type=compaction` 的 output item，并发送 `response.completed`。
4. 同时通过 `encrypted_content` 保存版本化摘要、通过 `summary` 暴露可见摘要；
   下一轮收到 `type=compaction` 时优先解码内部摘要，无法解码则使用 `summary`。

同时兼容 `/v1/responses/compact`、`/responses/compact` 及 `/detail` 别名。
流式压缩会先返回 `response.created` / `response.in_progress`，摘要生成超过
15 秒时发送 SSE 心跳，避免 AiMaMi 或前置反代把长时间无数据误判为断流。

## AiMaMi 中转

当 Codex 通过 AiMaMi 智能路由访问 DS2API 时，链路为：

```text
Codex -> AiMaMi /codex/router/v1 -> DS2API
```

当前 AiMaMi 会用自己的模型族默认值生成 catalog，而不是直接透传 DS2API 的
`context_window` 和 `auto_compact_token_limit`。它可能把 MiMo 标成 1,048,576、
DeepSeek 标成 1,000,000，并输出 `auto_compact_token_limit=null`。这会让 Codex
误以为离上下文上限很远。因此在 AiMaMi 中转设置中应覆盖模型上下文：

- MiMo：“上下文窗口”填 `32768`；当前 AiMaMi 不支持压缩阈值时，服务端仍会在
  12K 安全预算处压缩工具调用参数和旧对话。
- DeepSeek：96K–128K
- Kimi / GLM / 其他 Web 上游：96K–128K

如果当前 AiMaMi 版本不支持压缩阈值，可在 `~/.codex/config.toml` 的 AiMaMi
管理块之外设置一个保守回退值，例如当前使用 MiMo 时：

```toml
model_context_window = 32768
model_auto_compact_token_limit = 12000
```

Sub2API 在相同 AiMaMi catalog 条件下能工作，关键原因是 Sub2API 实现了
remote compaction v2，而不是因为它的 catalog 阈值更准确。本项目的对应实现
见上节。修改后需要完全重启 AiMaMi、Codex，并为已经超限的旧线程新建任务。

## 错误与调度

- `query is too long`、context length 等错误映射为
  `context_length_exceeded`，返回 413 或 `response.failed`，不将账号标记为坏凭据。
- 有账号但全部 busy 时先等待 `UPSTREAM_ACCOUNT_WAIT_TIMEOUT`（默认 15 秒）；
  超时后返回带 `Retry-After` 的 503。
- Codex Desktop 会并行发起子请求，生产环境建议每个 Provider 配置多个账号，
  并保持 `DEEPSEEK_MAX_CONCURRENCY` 与上游承受能力一致。

## 验证

- 真实 Codex CLI 请求捕获：`data/diagnostics/codex-request-capture.json`
- 文本与工具两轮回路的本地协议测试：`data/diagnostics/codex-protocol-probe.json`
- 自动化：`python -m pytest -q`
- 前端：`npm --prefix webui-new run build`
