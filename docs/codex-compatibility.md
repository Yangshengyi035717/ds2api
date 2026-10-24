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
