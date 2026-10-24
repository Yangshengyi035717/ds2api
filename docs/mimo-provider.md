# 小米 MiMo Studio 接入

本次增加 `provider=mimo`，默认公开模型为 `mimo-v2.6-pro`，同时提供
`mimo-v2.6-flash` 和 `mimo-v2.6-pro-ultraspeed-studio`。三者与观察到的
上游模型 ID 一致。

## 协议证据

分析使用 `reverse-engineer-anything` 技能及 REA 工具。

| 证据 | 标识 | 范围 |
| --- | --- | --- |
| 用户提供的 HAR | SHA256 `932cd70d3335233920d281b907aa9afcb112bbb3ed06d9dee1ca733a25a679b2` | 共 12 个记录，包含两次创建会话和两次生成 |
| REA HAR 解码 | `ev_4dc34d831fc20b71ae42975a53f3338cff4374849e741d1ac49856bb907b0193` | 历史流量，不能单独证明当前运行行为 |
| REA 接口视图 | `ev_0141fbd64be64fe66c710e43e13161f5aae8088fb502f7b138271c298ab3f18e` | 路径、方法、响应类型及记录序号 |
| 当前公开前端静态分析 | `ev_599af605fcdeb221c19a6537de013208bb9d6cc6915553fdc80013e0b46cbb74` | `main.41a56ba0.chunk.js`；静态图部分覆盖，不代表执行追踪 |
| UltraSpeed HAR | SHA256 `45483b8e7db3f37ac13a9e4a792e2339933855156caca095bef9ddbe89daf32c` | 4 个记录；包含 fastchat SSE 与指标上报 |

HAR 的 Cookie 请求头和 Cookie 列表均为空。真实验证使用用户另外提供的
Cookie；没有把该凭据放入源码、测试、文档或诊断报告。

从抓包和当前前端资源确认：

1. `POST /open-apis/chat/conversation/save` 保存客户端生成的会话 ID。
2. `POST /open-apis/react/chat` 发起生成，参数包括 `msgId`、
   `conversationId`、`query`、`modelConfig`、`multiMedias`。
3. 登录态通过 Cookie 携带，`xiaomichatbot_ph` 同时作为 URL query 参数，
   必须按普通 query 编码，不能把 Cookie 中的 `+` 当作空格。
4. SSE 的 `message.content` 是正文增量，`message.reasoningContent` 是推理增量。
   `dialogId` 和重复的 `usage` 事件均不是正文；
   `finish` 事件包含 `{"content":"[DONE]"}`。
5. `genTitle` 用于生成会话标题，完成对话并不依赖它，本实现不调用。
6. `mimo-v2.6-pro-ultraspeed-studio` 使用
   `POST /fastchat/open-apis/bot/chat`。SSE 事件结构仍为
   `dialogId`、`message`、`usage`、`finish`，但推理位于
   `message.content` 的 `<think>...</think>` 区间，并带有 NUL 分隔符。

额外进行了真实请求验证：只读
`POST /open-apis/chat/conversation/list` 携带
`{"pageInfo":{"pageNum":1,"pageSize":1}}`，有效 Cookie 返回
HTTP 200 / `code=0` / `data.dataList`，去掉登录凭据后返回 HTTP 401 / `code=401`。
它可用作协议保活探针，避免保活发送聊天。

## 实现选择

- 每轮生成新的会话 ID 并经 `save` 响应确认，随后发送完整客户端历史；
  不复用抓包里的 ID，也不套用 DeepSeek 的 parent/delta 链。
- 账号按 provider 隔离，支持已有代理池及账号代理绑定。
- 凭据必须包含 `userId`、`xiaomichatbot_serviceToken`、
  `xiaomichatbot_ph`；Token 可为空。只接受 MiMo 域名的 HAR 登录数据。
- 上游返回的有效域 Cookie 回写账号池，后续请求使用轮换后的 `ph`。
  认证失败进入需要重新登录状态，网络故障按临时错误处理。
- HTTP 401 / 403、业务 JSON 错误、SSE error、限流、空流和未收到结束事件的
  提前断流分别处理；提前断流不能伪装成正常 `stop`。
- 三种兼容 API 共用项目现有格式层。工具调用沿用现有文本 DSML 方案，
  不宣称新增了 MiMo 原生工具协议。
- UltraSpeed 按模型 ID 选择 fastchat 路径；标签解析器保留跨 chunk 的
  半截 `<think>` / `</think>`，将推理和正文分别输出，并丢弃 NUL 分隔符。

## 使用与复验

1. 登录 MiMo Studio，在浏览器 Network 中选择本站请求，复制完整 Cookie。
2. 在账号池选择“小米 MiMo 导入”并粘贴 Cookie，或导入含 Cookie 的 HAR。
3. 检查账号 provider 为 `mimo`、状态可用；不需要填写 Token。
4. 触发一次保活探测，等待任务显示 active / HTTP 200。
5. 在 API 文档或 Playground 中选择 `mimo-v2.6-pro`，非流式发送
   `Reply only: MIMO_PRO_API_OK`，检查完整正文。
6. 选择 `mimo-v2.6-flash` 并打开流式，检查正文与推理分流，以及
   `finish_reason=stop` 和 `[DONE]`。
7. 保活后再次调用，确认空 Token 不会导致账号被误标记失效。
8. 选择 `mimo-v2.6-pro-ultraspeed-studio` 并打开流式，确认上游使用
   `/fastchat/open-apis/bot/chat`，客户端正文不含 `<think>`，推理出现在
   `reasoning_content`。

本地回归测试：

```powershell
& ".venv/Scripts/python.exe" -m pytest -q
npm --prefix "webui-new" run typecheck
npm --prefix "webui-new" run build
```

## 本次验证结果（2026-10-22）

- 444 项自动化测试通过，前端类型检查和生产构建通过。
- 隔离的本地完整应用通过正常管理登录、创建客户端 API Key、导入 Cookie、
  绑定代理后，Pro 非流式与 Flash 流式均收到完整测试正文及正常结束标记；
  两轮只读保活均返回 HTTP 200。
- 当前 `127.0.0.1:8080` 实例已加载新代码、模型与账号。Flash 非流式实测
  HTTP 200 / `finish_reason=stop`，完成后账号空闲、错误数为 0。
  模型回复重复了测试标记中的一段，严格字符串匹配未通过，保留实际正文，
  没有加入可能破坏正常文本的去重逻辑。
- 当前实例的另一次 Pro 请求等待超过客户端 120 秒，服务端最终返回 503；
  具体停滞阶段未采集到，不能归因为凭据失效或声称已解决上游超时。
  随后的同代理只读探活通过。
- 本地全局协议保活开关仍为关闭；账号任务可以配置，但周期执行需要启用
  `KEEPALIVE_ENABLED` 并重启服务。

脱敏验证记录保存在 `data/diagnostics/mimo-api-verification-20261022.json`，
文档页截图为 `data/diagnostics/mimo-api-docs.png`；这些本地产物不进入 Git。

## UltraSpeed 复验（2026-10-23）

- 使用本地已导入的 MiMo Cookie 发起一次短请求，HTTP 200、
  `text/event-stream`，收到 `dialogId`、7 个 `usage`、4 个 `message` 和
  `finish`，3.505 秒正常结束。
- 正文同时包含 `<think>...</think>` 和 NUL 分隔符；去除标签后的最终正文为
  `MIMO_ULTRA_OK`。这说明不能把原始 content 直接返回给兼容 API。
- HAR 只覆盖一次真实生成，未覆盖 401、429、空流和所有模型权限错误；
  这些边界由适配器现有错误映射及合成测试覆盖。

## 服务器同步

用户补充授权同步本地凭据后，已构建并更新服务器的 `dsapi` Docker 容器，
通过正常管理员认证将本地 MiMo Cookie 导入账号池，并绑定服务器代理池中的
独立空闲代理。线上账号从 8 个增加到 9 个，MiMo Token 保持为空。

线上 Pro 流式调用返回 HTTP 200、正文、`finish_reason=stop` 和 `[DONE]`，
结束后账号空闲、错误数为 0。测试正文为 `MIMO_SERVER_OK_OK`，与要求的
`MIMO_SERVER_OK` 不完全一致；不能把传输成功等同于严格测试标记匹配。
线上协议保活已开启，MiMo 任务周期为 900 秒，实测 active / HTTP 200，
连续失败数为 0。

部署前的源码、账号数据和保活数据库备份位于服务器
`/www/dk_project/dk_app/mimo-20261022-232514-backup`，旧镜像标签为
`dsapi:before-mimo-20261022-232514`。脱敏线上验证记录保存在本地
`data/diagnostics/mimo-online-verification-20261022.json`。
账号凭据、诊断记录与截图保存在 Git 忽略的目录中，不随源码提交。

## 尚未证明的能力

- 没有观察到刷新 serviceToken 的独立登录协议。只读探活可校验状态和保存实际
  轮换的 Cookie，不能保证无限延长会话。
- 上游会在 `enableThinking=false` 时仍返回 `reasoningContent`；本实现保留
  实际返回的数据，不保证开关能强制改变模型行为。
- 三个模型当前前端配置均关闭联网搜索能力。请求字段可透传，不能据此宣称
  上游一定执行搜索。
- 抓包包含一次图片示例，但项目公共请求层当前采用文本 prompt，本次未实现
  MiMo 图片上传或多模态请求。
- HAR 和静态资源不覆盖所有错误码、登录、退出或完整会话生命周期；未知错误
  仍会作为上游错误返回，不猜测其含义。
- `mimo-v2.6-pro-ultraspeed-studio` 的 fastchat 通道可能在尚未输出内容时
  返回“服务器繁忙”或中途断流。适配器默认自动降级到同账号的
  `mimo-v2.6-pro`（`MIMO_ULTRASPEED_FALLBACK_MODEL` 可覆盖）；一旦已经开始
  输出则不重试，避免重复正文或重复工具调用。
- 首次 fastchat 瞬态失败后会进入默认 300 秒冷却期，后续请求直接使用
  `mimo-v2.6-pro`，避免每个工具回合都先等待一次坏通道失败。冷却时间可通过
  `MIMO_FASTCHAT_COOLDOWN_SECS` 调整。
- Codex 请求只要携带工具，公开模型仍显示 UltraSpeed，但上游直接路由到
  `mimo-v2.6-pro`。UltraSpeed fastchat 仅用于不带工具的普通聊天，避免长文件
  写入参数在 fastchat 流中断。
