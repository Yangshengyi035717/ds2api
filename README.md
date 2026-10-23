# DeepSeek Chat API Proxy

> 将 DeepSeek Chat、z.ai、Kimi、千问、腾讯混元 AI Studio、百度文心与豆包的 Web Chat 私有 API
> 转换为统一的 OpenAI / Anthropic 兼容格式。

> **🤖 全 AI 生成声明**: 本项目没有一行人工手写代码。所有的 API 端点设计、协议逆向、PoW 求解、SSE 解析、格式映射、文档编写等等全部由 **DeepSeek v4 Flash 模型** + **Claude Code** 协作完成。

**免责声明**: 本项目仅限学习研究使用。非官方项目，与 DeepSeek 无关。使用需自行承担风险，不保证稳定性。

---

## 功能特性

- **OpenAI 兼容** — `/v1/chat/completions` 与 `/v1/models` 接口，支持 `stream` 模式
- **Responses 兼容** — `/v1/responses` / `/responses`，支持流式文本、函数工具和思考开关
- **Anthropic 兼容** — `/v1/messages` 接口，支持 Claude API 格式
- **API Key 鉴权** — `/v1/*` 支持 `Authorization: Bearer` / `x-api-key`，适合公网部署
- **思维链（Reasoning/Thinking）** — 开启 `thinking_mode` 后自动分离思维链 tokens 并通过 `reasoning_content` 字段输出
- **思考模式** — DeepSeek 已把「快速模式 / 专家模式」合并为同一个模型（见下方说明），原专家体验现由 `thinking_enabled` 提供，响应含 THINK→RESPONSE 双阶段
- **低延迟直答** — 关闭思考即直接回答，无推理链开销
- **联网搜索** — 通过 `search_enabled` 参数启用实时搜索增强
- **Function Calling** — 基于 DSML（DeepSeek Markup Language）提示词注入实现工具调用
- **流式筛分** — `StreamSieve` 引擎，逐字符检测 DSML 工具调用标签，从 SSE 流中实时分离正文与工具调用
- **PoW 鉴权** — 自动完成 WASM 工作量证明（DeepSeekHashV1）挑战
- **会话管理** — 自动创建和管理 DeepSeek Chat 会话；可选多轮会话复用
- **多账号池** — 轮询分配、idle/busy/error 状态、健康检查、自动恢复
- **多提供商账号池** — DeepSeek / z.ai / Kimi / 千问 / 混元 / 文心 / 豆包分类管理，按请求模型自动隔离路由
- **Kimi 兼容** — Connect JSON 流式协议与 access/refresh token 自动续期
- **混元兼容** — AI Studio `hy4-preview` SSE 协议、Cookie 账号池与协议保活
- **文心兼容** — chat.baidu.com `smartMode` SSE 协议、HAR 凭据导入与账号保活
- **豆包兼容** — doubao.com `chat/completion` SSE 协议与登录 Cookie 账号池
- **小米 MiMo 兼容** — MiMo Studio Cookie 账号池、Pro / Flash 对话与只读协议保活
- **代理池** — 一号一代理、出口绑定、连通性检测与加密存储
- **Sub2API 上游适配** — 富模型目录、Provider Manifest、价格/倍率配置和同步脚本
- **客户端限流** — 滑动窗口，按 API key 和 IP 独立计数；响应头 `X-RateLimit-*`
- **使用量统计** — 基于 tiktoken 估算的 prompt / completion / total tokens
- **管理面板** — 内置 Web 管理界面，支持请求统计、账号池管理（多账号轮询/持久化增删改查/重登录）

---

## 添加 DeepSeek 账号（浏览器导入）

DeepSeek 的密码登录不是普通 HTTP 表单：登录请求体经 RSA-OAEP 包装的 AES-CBC 加密，MFA challenge 由浏览器 IndexedDB 中**不可导出**的 ES256 私钥签名。因此服务端不能仅凭账号密码自动登录，也不应保存 DeepSeek 密码。

推荐流程（WebUI → 账号池 → 添加账号 → 浏览器导入）：

1. 在浏览器正常登录 `https://chat.deepseek.com`。
2. 打开 DevTools → Console，运行页面提供的“浏览器导出脚本”，脚本会复制如下 JSON：

   ```json
   {"token": "...", "cookies": {"ds_session_id": "..."}, "device_id": "..."}
   ```

3. 粘贴到 WebUI 的导入框并保存。也可以直接粘贴 DevTools 导出的整份 HAR；服务端会从 `mfa/verify_signature` 响应中提取 token，并尽量提取 Cookie 与 `x-device-id`。
4. 导入接口为 `POST /admin/api/accounts/import`，响应只返回脱敏摘要，不包含 token/cookie 明文。

注意：浏览器退出登录会吊销/轮换旧 token；退出后服务器保存的旧凭据会立即失效，需要重新从已登录浏览器导出。协议保活只能维持现有登录态，不能代替登录。

### 添加 z.ai 账号

在 WebUI → 账号池 → 添加账号 → **z.ai 导入** 中复制导出脚本，然后在已登录的
`https://zchat.z.ai` 页面 Console 运行。脚本会读取：

```json
{
  "provider": "zai",
  "token": "...",
  "user_id": "...",
  "device_id": "uid_...",
  "cookies": {"acw_tc": "..."}
}
```

`user_id` 通过同源接口 `GET /api/v1/auths/` 获取，是 z.ai 请求签名
`X-Signature` 的必需字段；`device_id` 来自 `localStorage._arms_uid`。
导入后账号池会显示 `z.ai` 分类，调用模型名 `glm-5.3-flash` 时只选择
`provider=zai` 的账号，不会与 DeepSeek 账号串用。

当前 z.ai 网页把 `GLM-5.3-Flash` 暴露为公开名称、但 completion API 的
模型 ID 是 `x-preview-l`。服务端会自动执行：

```text
公开模型：glm-5.3-flash
上游模型：x-preview-l
```

每次 z.ai 请求会先调用 `POST /api/v1/chats/new` 创建服务端会话，再用返回的
chat/message UUID 发起 `/api/v2/chat/completions`。

### 添加 Kimi 账号

在 WebUI → 账号池 → 添加账号 → **Kimi 导入** 中复制脚本，并在已登录的
`www.kimi.com` 页面 Console 运行。Kimi 需要 `access_token` 和
`refresh_token`；access token 约 15 分钟有效，服务端会自动调用
`AuthService/RefreshToken` 并持久化轮换后的两个 token。

公开模型映射：

```text
kimi-k2.6  → k2d6-chat
kimi-k3    → k3-agent
kimi-k2.8  → k28-agent-preview
```

### 添加混元账号

在 WebUI → 账号池 → 添加账号 → **混元导入** 中粘贴 Cookie 请求头或 HAR。
混元登录使用 HttpOnly Cookie，页面 Console 无法读取完整登录态；请在已登录的
`aistudio.tencent.com` 页面打开 DevTools → Network，选择任意
`api.hunyuan.tencent.com` 请求并复制完整 Cookie：

```text
hunyuan_source=web; hunyuan_user=...; hunyuan_token=...
```

公开模型与上游映射：

```text
hunyuan-hy4-preview → hy4-preview
```

服务端在每次会话开始时调用 `POST /api/new-portal/generate/id` 创建会话，再通过
`POST /api/new-portal/chat/{conversationId}` 获取 SSE 生成结果。

### 添加文心账号

在 WebUI → 账号池 → 添加账号 → **文心导入** 中粘贴已登录浏览器
`chat.baidu.com` 请求的完整 Cookie（必须包含 `BDUSS`），或导出包含 Cookie 的
HAR。服务端会访问 `wenxin.baidu.com` 自动获取页面 `token` 与 `lid`。

公开模型与上游映射：

```text
wenxin-smart-mode → smartMode
```

文心账号必须保留百度登录 Cookie；退出登录或 Cookie 过期后需要重新导入。
仅导入匿名 HAR 中的 `chat_token` 不足以在服务端复用。

### 添加豆包账号

在 WebUI → 账号池 → 添加账号 → **豆包导入** 中粘贴已登录浏览器
`www.doubao.com` 请求的完整 Cookie（必须包含 `sessionid`、`ttwid`），或导出包含
Cookie 的 HAR。服务端按会话 ID 自己生成请求参数，不需要浏览器 `a_bogus`。

公开模型与上游映射：

```text
doubao-seed → model_item_key=0
```

豆包的 `msToken` 随 Cookie 轮换，无需单独保存；Cookie 失效后重新导入即可。

### 添加小米 MiMo 账号

在 WebUI → 账号池 → 添加账号 → **小米 MiMo 导入** 中粘贴已登录
`aistudio.xiaomimimo.com` 请求的完整 Cookie，或导入包含 Cookie 的 HAR。
Cookie 必须包含 `userId`、`xiaomichatbot_serviceToken` 和 `xiaomichatbot_ph`，
不需要另填 Bearer Token。Cookie 值里的双引号、`+`、`/` 和 `=` 会正确解析。
浏览器导出的脱敏 HAR 可能不含 Cookie，此时需要另行复制完整 Cookie 请求头。

API 文档和 Playground 可选择 `mimo-v2.6-pro`（默认）、`mimo-v2.6-flash`
或 `mimo-v2.6-pro-ultraspeed-studio`。UltraSpeed 使用独立的 fastchat 端点，
正文中的 `<think>` 标签会转换为标准的 `reasoning_content`。
支持现有 OpenAI Chat Completions、Responses 和 Anthropic Messages 文本接口，
流式正文与 `reasoning_content` 分开返回，每轮发送客户端提供的完整历史。

保活仅调用只读会话列表并保存上游轮换的 Cookie，不创建聊天或消耗生成额度；
它不能恢复已经过期或被退出登录吊销的凭据，失效后需要重新登录并导入。
代理绑定沿用账号池配置。当前接入范围为文本对话，图片上传不在本次实现内。
协议证据、复现步骤及边界见 [MiMo 接入说明](docs/mimo-provider.md)。

### 服务器浏览器登录（noVNC）

如果不想手动导出，可启用 `browser-login` sidecar。WebUI 的「浏览器登录」
菜单会启动服务器上的 Chromium，并通过 noVNC 把画面嵌入页面：

```env
BROWSER_LOGIN_SHARED_SECRET=<random-secret>
```

```bash
docker compose up -d --build
```

然后按 `deploy/README.md` 安装 `nginx-browser-login.lua`、共享密钥文件和
`nginx-browser-login.conf`（宝塔 Nginx 未编译 `auth_request`，改用
`ngx_lua` 本地校验 HMAC），并确保存在 `$connection_upgrade` map。
浏览器登录完成后，服务端会直接读取 `localStorage.userToken`、
`deepseek-device-id:chat` 和 Cookie，并调用内部 ingest 接口写入账号池。
之后的保活仍由 `keepalive/` 负责。

### 协议保活（DeepSeek / z.ai / Kimi / 千问 / 混元 / 文心 / 豆包）

DS2API 内置多账号 `KeepaliveScheduler`，会为账号池中的每个账号创建独立
Job，并按 `provider` 自动分流：

- DeepSeek：周期调用 `GET /api/v0/users/current`
- z.ai：周期调用 `GET /api/v1/chats/?page=1&type=all&channel=zai-web`
- 混元：周期调用 `POST /api/new-portal/chat/conversation/list`
- 文心：周期提交轻量 `conversation` SSE 请求并校验 `basedata`
- 豆包：周期调用只读接口 `GET /samantha/notice/info` 校验登录态（不消耗生成额度）

探活只使用只读接口，不创建 chat、不发送 prompt，因此不消耗生成额度。
上游轮换的 WAF / session Cookie 会自动执行 `PUT /admin/api/accounts/{id}`
回写；z.ai 优先使用该账号绑定的代理出口。`401/403`（z.ai）或
`40003`（DeepSeek）判定为登录失效，并停止等待人工重新导入。

启用进程内调度器：

```env
KEEPALIVE_ENABLED=true
KEEPALIVE_TICK_SECS=30
KEEPALIVE_GLOBAL_CONCURRENCY=4
```

Job、租约、下次执行时间、退避和待重登状态保存在
`data/keepalive.sqlite3`。WebUI 账号池支持逐账号启停、立即检测，以及按
各 Provider 批量启用、暂停和检测。

`keepalive/` 下的单账号 Docker worker 保留为兼容模式。启用
`KEEPALIVE_ENABLED=true` 后应停止旧的 `dsapi-keepalive` 容器，避免两个
调度器重复探活。单账号 worker 的 `KEEPALIVE_SHARED_SECRET` 仍可作为
独立部署方案使用。

如果服务器机房 IP 被上游 CDN 拦截（页面显示 `403 Request blocked`），
在 `.env` 设置浏览器出口代理后重启 sidecar：

```env
BROWSER_LOGIN_PROXY=http://user:pass@host:port
```

```bash
docker compose up -d --force-recreate browser-login
```

## 快速开始

### 前置条件

- Python 3.10+
- 可以访问 `chat.deepseek.com` 的网络环境
- 有效的 DeepSeek 账号（免费注册）

### 安装

```bash
# 1. 克隆/下载本项目
git clone https://github.com/snake-aabb-wtf/deepseek-web2api-free.git
cd deepseek-web2api-free

# 2. 安装依赖（Windows 推荐直接用 start.bat 自动创建虚拟环境并安装；手动方式：）
pip install -r requirements.txt
```

### 获取凭证

你需要从浏览器开发者工具中提取你的 DeepSeek 凭证：

1. 用浏览器打开 [chat.deepseek.com](https://chat.deepseek.com) 并登录
2. 按 `F12` 打开开发者工具，切换到 **Network（网络）** 标签
3. 在页面中随便发一条消息
4. 在网络请求列表中点击任意一个请求（如 `chat/completion`）
5. 在请求头中找到以下两个值：

| 凭证 | 位置 | 示例 |
|------|------|------|
| Token | `Authorization` 请求头 `Bearer` 后的值 | 从请求头复制 |
| Cookies | `Cookie` 请求头的完整值 | 从请求头复制 |

> 将凭证添加到 WebUI「账号池」，或写入 `data/accounts.json`。账号凭证不再从 `.env` 读取。

### 配置

```bash
# 复制环境变量模板
cp .env.example .env
```

编辑 `.env` 文件：

```ini
# 客户端 API Key 在 WebUI「设置」页创建和撤销
# 完整 Key 仅在创建时显示；服务端仅保存哈希
ALLOW_UNAUTHENTICATED_API=false

# 必填：WebUI 管理面板密码（未设置默认 admin，公网部署前必须修改）
DEEPSEEK_ADMIN_PASSWORD=change-me
# WebUI 修改密码后保存 PBKDF2 verifier；此环境值保留为恢复回退
ADMIN_PASSWORD_STORE_PATH=data/admin_auth.json

# DeepSeek / z.ai 账号只通过 WebUI「账号池」或 data/accounts.json 配置。

# 公开模型名称优先在 WebUI「设置 → 模型名称」中配置，
# 保存到 data/runtime_config.json 并立即生效，无需重启。
# 下面的值仅作为首次启动的默认回退。
MODEL_NAME=deepseek-chat
# z.ai 默认模型；glm-* 请求会自动选择 provider=zai 账号
ZAI_MODEL_NAME=glm-5.3-flash

# 可选（高级）：DeepSeek 能力路由覆盖。默认不配置——/v1/models 会列出
# 当前 DeepSeek 模型与 z.ai 模型；老的 model 名（deepseek-reasoner 等）作为别名照常接受。
# MODEL_ROUTES={"my-model":"thinking"}

# 可选：监听地址/端口（默认 127.0.0.1:8080）
HOST=127.0.0.1
PORT=8080

# 可选：能力开关（auto=尊重客户端 / enabled / disabled）
THINKING=auto
SEARCH=auto

# 已废弃：仅兼容旧配置，expert ≡ THINKING=enabled，quick ≡ THINKING=disabled
# MODE=auto
```

完整变量说明见 `.env.example` 与 [AGENTS.md](AGENTS.md)。

### 启动

```bash
# 推荐：使用启动脚本 start.bat（Windows）
#   - 自动定位 Python 3.10+，检测/创建虚拟环境（.venv / venv / env）
#   - 校验依赖，不足则自动安装
#   - WebUI 产物缺失时自动 npm 构建（set WEBUI_REBUILD=1 强制重建）
#   - 只结束本项目的旧进程，绝不误杀占用端口的其他服务
#   - 启动前打印访问地址，默认 3 秒后自动打开浏览器（set WEBUI_OPEN=0 跳过）
start.bat

# 手动启动（已在虚拟环境中）：
#   .venv\Scripts\python -m uvicorn server:app --host 127.0.0.1 --port 8080
```

启动后终端会显示访问地址并自动打开浏览器；日志示例：
```
INFO:     Started server process [12345]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
```

### 验证

```bash
curl http://localhost:8080/health
# → {"status":"ok"}

curl http://localhost:8080/v1/models \
  -H "Authorization: Bearer sk-your-api-key"
# → {"object":"list","data":[
#      {"id":"deepseek-chat",...}   # 只有一个模型；开启思考请设 THINKING=enabled
#   ]}
```

### 管理面板

项目内置 Web 管理界面（v3.0.0 起为 React SPA），提供请求统计和账号池管理功能：

```
浏览器打开 http://localhost:8080/webui/
```

> start.bat 会自动构建 WebUI（源码在 `webui-new/`，产物 `webui-new/dist/`）；若手动部署，需先在 `webui-new/` 执行 `npm install && npm run build`。

初始密码为 `.env` 中设置的 `DEEPSEEK_ADMIN_PASSWORD`（未设置则为 `admin`）。
登录后可在「设置 → 管理员密码」中修改；新密码只以 PBKDF2-SHA256 verifier
保存到 `data/admin_auth.json`，并立即注销所有管理会话。删除该文件可回退到
`.env` 初始密码。

账号只从 `data/accounts.json` 加载。WebUI 账号池中的增删改操作会写入该文件；也可以直接维护该 JSON 文件。

WebUI 其他功能包括：

- **概览** — 实时请求统计（总量/成功/失败/延迟/运行时长）+ 账号池状态一览
- **代理池** — 维护 upstream 代理、连通性检测，并按账号绑定稳定出口
- **API 文档** — OpenAI / Anthropic 接入说明，内置 `/v1/models`、Chat Completions、Messages 在线调试器
- **Playground** — 在线测试对话（单个模型 + 「思考模式」/「联网搜索」两个开关，支持推理过程可视化）
- **设置** — 可视化配置模型/定价、管理 API Key、修改管理员密码，并查看当前运行时配置

---

## 非交互式账号池配置（代理 / 自动化工具）

> **给 SDK、脚本、curl、CI、代理用**，不经过 WebUI 面板。账号凭证统一存储在 `data/accounts.json`；`.env` 不读取账号凭证。非交互场景直接维护该文件即可。

**`data/accounts.json`（v1 明文）完整格式：**

```json
{
  "version": 1,
  "accounts": [
    {
      "email": "deepseek-001",
      "provider": "deepseek",
      "token": "<token>",
      "cookies": "<cookies>",
      "proxy_id": "proxy_xxx"
    },
    {
      "email": "zai-001",
      "provider": "zai",
      "user_id": "<z.ai user id>",
      "device_id": "uid_...",
      "token": "<token>",
      "cookies": "<cookies>",
      "proxy_id": "proxy_yyy"
    }
  ]
}
```

- DeepSeek：必填 `token` + `cookies`；`provider` 省略时默认 `deepseek`
- z.ai：必填 `token` + `user_id`；`provider` 必须为 `zai`，Cookie/Device ID 建议保留
- 可选 `email`/`id`/`proxy_id`/`created_at`/`updated_at`
- 文件 `chmod 600`、目录 `chmod 700`（含上游登录凭证，泄露=账号被滥用）
- 服务启动时一次性全量加载；改文件后需重启进程
- **加密**：设 `DEEPSEEK_ENCRYPTION_KEY`(Fernet) 后首次启动自动把明文迁移为 v2 加密，并留 `accounts.json.v1.bak`；此时勿再手写明文

**非交互配置步骤：**

```bash
# 1. 写账号池文件（可脚本生成）
cat > project_dir/data/accounts.json <<'JSON'
{"version":1,"accounts":[{"email":"acc-001","provider":"deepseek","token":"<token>","cookies":"<cookies>"}]}
JSON
chmod 600 project_dir/data/accounts.json && chmod 700 project_dir/data

# 2. 配 API 鉴权（非交互必需，否则 /v1/* → 503）
#    首次启动后登录 WebUI，在「设置」页创建 API Key

# 3. 启动
python -m uvicorn server:app --host 127.0.0.1 --port 8080
```

- **改存储路径**：`ACCOUNT_STORE_PATH` 环境变量。

### 代理池与出口绑定

WebUI「代理池」支持 HTTP / HTTPS / SOCKS4 / SOCKS5 / SOCKS5H 代理：

- 支持标准 URL、`host:port` 和 `host:port:username:password` 裸格式
- 代理池页面支持按行批量导入，重复项自动跳过
- 每个代理默认只能绑定一个账号，避免多个账号共用同一出口 IP
- 账号在「账号池 → 编辑账号」中选择代理，绑定关系持久化到 `data/accounts.json`
- 代理地址持久化到 `data/proxies.json`；开启 `DEEPSEEK_ENCRYPTION_KEY` 后代理 URL 会与账号凭证一起加密
- 删除已绑定代理会被拒绝，需先解除账号绑定
- 代理停用或失效时，绑定账号会报错而不会静默回退为直连
- 「检测」会通过代理访问 `PROXY_CHECK_URL`（默认 `https://chat.deepseek.com/`）

建议一个账号长期使用一个稳定、地域一致的专用出口，不要按请求轮换代理。

---

## Sub2API 上游接入

推荐将 DS2API 作为 Sub2API 的 `DeepSeek / API Key` 上游：

```json
{
  "platform": "deepseek",
  "type": "apikey",
  "credentials": {
    "api_key": "<DS2API 客户端 API Key>",
    "base_url": "https://ds2api.example.com",
    "account_mode": "payg",
    "api_protocol": "chat_completions",
    "model_mapping": {
      "deepseek-chat": "<DS2API 当前公开模型名>"
    }
  }
}
```

- `base_url` 填写站点根地址，Sub2API 会追加 `/v1/models` 与 `/v1/chat/completions`
- 使用 `type=apikey` 才能正常使用 Sub2API 的“同步上游模型”
- 推荐 `api_protocol=chat_completions`；Codex/Responses 客户端可改为 `responses`，DS2API 同时提供 `/v1/responses` 与 `/responses`
- 自定义模型名必须写入 `model_mapping`，否则可能被 DeepSeek 官方模型白名单拦截
- 账号倍率、渠道模型价格和分组高峰倍率仍由 Sub2API 本地配置，不从 DS2API 自动读取
- Sub2API 与 DS2API 分机部署时需要 HTTPS 域名或正确配置内网 URL 白名单

配置完成后可运行同步器：

```powershell
$env:DS2API_KEY = "<DS2API API Key>"
$env:SUB2API_ADMIN_TOKEN = "<Sub2API 管理员 Token>"
.\.venv\Scripts\python.exe scripts\sync_sub2api.py `
  --sub2api-url http://127.0.0.1:18080 `
  --group-id 1 `
  --apply
```

省略 `--apply` 时只打印账号与渠道载荷，不写入 Sub2API。

---

## API 文档

### `POST /v1/chat/completions`

OpenAI 兼容的聊天补全接口。

#### 请求体

```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Hello!"}
  ],
  "stream": false,
  "temperature": 0.7,
  "top_p": 0.95,
  "max_tokens": null,
  "thinking_mode": false,
  "search_enabled": false,
  "tools": null,
  "tool_choice": "auto"
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model` | `string` | `"deepseek-chat"` | 本代理只有一个模型，此字段不影响上游模型；老名字（`deepseek-reasoner` 等）作为别名照常接受。是否思考看 `thinking_mode` 或服务端 `THINKING` |
| `messages` | `array` | 必填 | 消息列表，支持 `system`/`user`/`assistant`/`tool` 角色 |
| `stream` | `boolean` | `false` | 是否流式输出 |
| `temperature` | `float` | `null` | 采样温度（传递给 DeepSeek 但效果取决于服务端） |
| `top_p` | `float` | `null` | Top-p 采样 |
| `max_tokens` | `int` | `null` | 最大生成 tokens |
| `thinking_mode` | `boolean` | `false` | 开启深度推理（上游 `thinking_enabled`，即原「专家模式」） |
| `search_enabled` | `boolean` | `false` | 开启联网搜索增强 |
| `stream_options` | `object` | `null` | 支持 `{"include_usage":true}`；流式末尾追加 usage-only 帧 |
| `user` | `string` | `null` | 多轮会话标识（与 `X-Conversation-Id` 二选一，见「多轮对话」） |
| `tools` | `array` | `null` | OpenAI 格式的工具定义 |
| `tool_choice` | `string\|dict` | `null` | 工具选择策略 |

### 模型与能力发现

- `GET /v1/models`：同时返回 `deepseek-chat` 与 `glm-5.3-flash`，并带
  `provider=deepseek|zai`、显示名、思考能力、输入模态和支持端点
- `GET /v1/models/{model}`：单模型查询
- `GET /v1/provider-manifest`：供 Sub2API 等外部网关同步账号/渠道的模型与协议能力清单
- `POST /v1/responses` / `POST /responses`：Responses API 兼容入口，支持流式、文本、函数工具和思考开关

`/v1/*` 错误统一使用 OpenAI `{"error":{"message","type","code"}}` 结构。流式 finish chunk 包含 `usage`；请求 `stream_options.include_usage=true` 时还会追加 `choices=[]` 的 usage-only chunk。

#### 非流式响应（`stream: false`）

```json
{
  "id": "chatcmpl-a1b2c3d4e5f6",
  "object": "chat.completion",
  "created": 1712345678,
  "model": "deepseek-chat",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "你好！有什么可以帮助你的吗？"
    },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": -1,
    "completion_tokens": -1,
    "total_tokens": -1
  }
}
```

> **注意**: `usage` 中的 tokens 数返回 `-1`，因为 DeepSeek Chat 不暴露 token 计数。这是已知限制。

#### 流式响应（`stream: true`）

标准 OpenAI SSE 格式，每个事件是一行 `data: {...}\n\n`，以 `data: [DONE]\n\n` 结尾：

```
data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"！"},"finish_reason":null}]}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

---

### `POST /v1/messages`

Anthropic Claude API 兼容的聊天补全接口。

#### 请求体

```json
{
  "model": "claude-3-5-sonnet-20241022",
  "max_tokens": 1024,
  "messages": [
    {"role": "user", "content": "Hello!"}
  ],
  "system": "You are a helpful assistant.",
  "stream": false,
  "thinking": {"type": "enabled", "budget_tokens": 16000},
  "tools": [
    {"name": "get_weather", "description": "获取天气", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}
  ]
}
```

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model` | `string` | `"claude-3-5-sonnet-20241022"` | 模型名称（不影响实际模型，仅用于标识） |
| `messages` | `array` | 必填 | 消息列表，支持 `user`/`assistant` 角色 |
| `system` | `string\|array` | `null` | 系统提示词 |
| `stream` | `boolean` | `false` | 是否流式输出 |
| `thinking` | `object` | `null` | `{"type": "enabled"}` 开启思考模式 |
| `tools` | `array` | `null` | Anthropic 格式的工具定义（`name`/`description`/`input_schema`） |
| `max_tokens` | `int` | `null` | 被忽略（DeepSeek 不支持） |
| `metadata` | `object` | `null` | 被忽略 |
| `stop_sequences` | `array` | `null` | 被忽略 |

#### 非流式响应（`stream: false`）

```json
{
  "id": "msg_xxxxxxxxxxxxxxxxxxxxxxxx",
  "type": "message",
  "role": "assistant",
  "content": [
    {"type": "text", "text": "你好！有什么可以帮助你的吗？"}
  ],
  "model": "deepseek-chat",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {"input_tokens": -1, "output_tokens": -1}
}
```

工具调用时：

```json
"content": [
  {"type": "tool_use", "id": "toolu_xxx", "name": "get_weather", "input": {"city": "北京"}}
],
"stop_reason": "tool_use"
```

#### 流式响应（`stream: true`）

Anthropic 原生 SSE 格式，包含 `message_start`、`content_block_start`、`content_block_delta`、`content_block_stop`、`message_delta`、`message_stop` 事件：

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","type":"message","role":"assistant","content":[],"model":"deepseek-chat","stop_reason":null,...}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"推理过程..."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: content_block_start
data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"回答内容"}}

event: content_block_stop
data: {"type":"content_block_stop","index":1}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":-1}}

event: message_stop
data: {}
```

> **注意**: Anthropic 端点与 OpenAI 端点共享相同的底层 adapter，MODE/THINKING/SEARCH 环境变量同时影响两个端点。

---

## THINKING / SEARCH 详解（含已废弃的 MODE）

> **v3.3.8 起：DeepSeek 已合并「快速模式 / 专家模式」，本项目也只服务一个模型。**
> 网页端 `client/settings?scope=model` 现在返回
> `{"model_type":"default","enabled":true,"switchable":true}`、
> `{"model_type":"expert","enabled":false,"switchable":false}`、
> `{"model_type":"vision","enabled":false,"switchable":false}`，
> 且上游会把请求体里的 `model_type` 一律改写回 `"default"`（可在 `event: ready` 帧看到）。
> 也就是说**只有一个对话模型**，原来的「专家体验」完全由 `thinking_enabled` 提供。
> 实测 `model_type=default` 与 `expert` 在同一 prompt 下输出逐字相同、时延无差异、工具调用表现一致。

因此现在的两个真正开关是：

| 环境变量 | 可选值 | 默认值 | 说明 |
|----------|--------|--------|------|
| `THINKING` | `auto` / `enabled` / `disabled` | `auto` | 控制 `thinking_enabled`：`true` / `false`，即原「专家模式」 |
| `SEARCH` | `auto` / `enabled` / `disabled` | `auto` | 控制 `search_enabled`（联网搜索）：`true` / `false` |
| `MODE`（已废弃） | `auto` / `quick` / `expert` | `auto` | **仅为兼容旧配置保留**：`expert` ≡ `THINKING=enabled`，`quick` ≡ `THINKING=disabled`，`auto` 不介入。新配置请直接用 `THINKING` |

`model_type` 仍然会随请求发送（保持与浏览器一致的请求形态），但它是**协议字段**而非能力选择：默认值 `"default"`，只在 `MODEL_ROUTES` 的字典形式里显式指定时才改变（留给上游将来重新放开 `vision` 之类的场景）。

优先级：环境变量 > 请求参数。当环境变量为 `auto` 时，由客户端请求中的对应字段决定行为。

### 组合示例

| THINKING | SEARCH | 行为 | 典型场景 |
|----------|--------|------|----------|
| `auto` | `auto` | 由客户端 `thinking_mode` / `search_enabled` 决定 | 完全由客户端灵活控制 |
| `disabled` | `auto` | 强制关闭思考，直接作答 | 追求低延迟、不需要深度推理 |
| `enabled` | `enabled` | 强制思维链 + 联网搜索 | 深度推理 + 实时信息 |
| `enabled` | `disabled` | 思维链但关闭联网 | 快速响应但附带推理过程 |

### 环境变量与请求参数互斥

```
THINKING=auto,    SEARCH=auto, 请求 thinking_mode=true  →  有 reasoning_content，搜索由客户端决定
THINKING=disabled,              请求 thinking_mode=true  →  无 reasoning_content（服务端强制关闭）
THINKING=enabled, SEARCH=disabled                        →  有 reasoning_content + 无联网搜索
MODE=expert（旧写法）                                    →  等价于 THINKING=enabled
```

---

## 思维链（Reasoning / Thinking）

当 `thinking_mode=true`（或 `THINKING=enabled`）时，流式和非流式响应中推理 tokens 通过 `reasoning_content` 字段暴露。

### 流式响应中的推理

开启思考后，SSE 流先输出推理 tokens，再输出正式回答：

```
data: {"id":"...","choices":[{"index":0,"delta":{"reasoning_content":""},"finish_reason":null}]}

data: {"id":"...","choices":[{"index":0,"delta":{"reasoning_content":"首先"},"finish_reason":null}]}

data: {"id":"...","choices":[{"index":0,"delta":{"reasoning_content":"需要"},"finish_reason":null}]}
...
data: {"id":"...","choices":[{"index":0,"delta":{"content":"您好"},"finish_reason":null}]}

data: {"id":"...","choices":[{"index":0,"delta":{"content":"！"},"finish_reason":null}]}
...
data: {"id":"...","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
data: [DONE]
```

客户端如 NextChat、Open WebUI 等支持识别 `reasoning_content` 字段并展示思维链。如果客户端不支持，可以设置 `THINKING=disabled` 强制不输出推理过程。

---

## Function Calling（工具调用）

本项目通过 **DSML（DeepSeek Markup Language）** 提示词注入实现工具调用，利用 DeepSeek Chat 对 XML 标签的理解能力。

### 工作原理

1. `tools` 参数中的函数定义被转换为 DSML 格式的系统提示词
2. 提示词指导模型以指定 XML 格式响应工具调用
3. `StreamSieve` 引擎实时从 SSE 流中检测 DSML 标签
4. 匹配的工具调用被转换为 OpenAI 格式的 `tool_calls` 返回

### 使用示例

```python
import openai

client = openai.Client(base_url="http://localhost:8080/v1", api_key="sk-your-api-key")

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "北京的天气怎么样？"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "获取指定城市的天气信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名称"}
                },
                "required": ["city"]
            }
        }
    }],
    tool_choice="auto",
)
```

### DSML 格式

DSML 使用类似 XML 的标签结构。当模型决定调用工具时，响应格式为：

```xml
<|DSML|tool_calls>
  <|DSML|invoke name="get_weather">
    <|DSML|parameter name="city"><![CDATA[北京]]></|DSML|parameter>
  </|DSML|invoke>
</|DSML|tool_calls>
```

`StreamSieve` 引擎在收到第一个 `<` 标签字符时就转至 "capture" 模式，积累全部 DSML 内容后一起解析。避免了模型"先输出正文再输工具调用"导致的文本污染。

---

## 多轮会话（v2.2.0）

DeepSeek Chat 协议本身支持 `parent_message_id` 串接同 `chat_session_id` 内的消息。v2.2.0 利用此能力为兼容端点提供轻量级多轮支持：

- **Anthropic 客户端**：通过请求体中的 `metadata.user_id` 字段作为会话粘性 key
- **OpenAI 客户端**：通过自定义 HTTP 头 `X-Conversation-Id` 或顶层 `user` 字段作为会话 key
- **隔离与账号绑定**：缓存同时绑定调用方和创建会话的 DeepSeek 账号；同一上游 session 不会跨账号提交。
- **无显式标识时**：不复用会话。不会再根据第一条消息推断 key，以避免不同用户因首句相同而串话。

会话缓存默认 TTL 为 1800 秒（`SESSION_CACHE_TTL`），过期后自动开始新会话。设置 `SESSION_CACHE_TTL=0` 完全禁用多轮行为。

```bash
# OpenAI 多轮示例（带 X-Conversation-Id 头）
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Conversation-Id: user-42-session-7" \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[
    {"role":"user","content":"我叫小明"},
    {"role":"user","content":"我叫什么？"}
  ]}'
# 第二条消息会看到第一条的上下文。
```

> **注意**：多轮会话依赖 DeepSeek 上游接受 `parent_message_id`；如果上游对历史消息有长度限制，长会话可能中途被截断。账号繁忙或不可用时，代理会安全地改用新会话，而不是用其他账号继续旧会话。

---

## 模型路由（v2.2.0；v3.3.8 起为可选的高级覆盖）

**默认不配置。** DeepSeek 侧只服务一个模型：`MODEL_NAME`（默认
`deepseek-chat`），「思考 / 不思考」由 `THINKING`（或请求里的
`thinking_mode`）决定，不是两个 DeepSeek 模型。`/v1/models` 另会列出
z.ai 的 `glm-5.3-flash`；`glm-*` 请求由 Provider 路由到 z.ai 账号。

写死老名字的客户端（`deepseek-reasoner` 等）不会 404：这些名字经 `model_router.LEGACY_MODEL_ALIASES` 归一到 `deepseek-chat`，照常服务，但**不改变行为**（旧版它们等于「开思考」，现在要思考请设 `THINKING=enabled`）。

确实需要按 model 名分流时才配置 `MODEL_ROUTES`；配置后 `/v1/models` 会改为列出它的键：

```bash
# 简单形式 — 字符串值（能力预设）
#   "default"  → 关闭思考（直答）
#   "thinking" → 开启思考（原「专家体验」）
#   "expert"   → thinking 的旧别名，兼容 3.3.8 之前的配置
#   "quick"    → default 的旧别名
MODEL_ROUTES={"my-model":"thinking"}

# 完整形式 — dict 值（可显式指定 model_type 协议字段与 search）
MODEL_ROUTES={"my-model":{"thinking":"enabled","search":"disabled"}}
```

未命中的 model 名按 `THINKING` / `SEARCH` env 变量处理（`MODE` 已废弃，仍兼容）。

---

## 客户端限流（v2.2.0）

v2.2.0 内置了基于滑动窗口的双维度限流器：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ENABLE_RATE_LIMIT` | `true` | 总开关；设为 `false` 完全禁用 |
| `CLIENT_RPM_PER_KEY` | `60` | 每 API key 每分钟请求数（0 = 不限） |
| `CLIENT_RPM_PER_IP` | `120` | 每 IP 每分钟请求数（0 = 不限） |

每个请求需要 **同时** 通过 key 和 IP 两个维度的检查。命中限流时返回 `429 Too Many Requests`，响应头含：

```
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 47
Retry-After: 47
```

> 限流状态保存在进程内；多 worker 部署（gunicorn -w N）实际限制为 N × 配置值。

---

## 使用量统计（v2.2.0）

`usage` 字段从 v2.1 的硬编码 `-1` 升级为真实 token 数。优先使用 `tiktoken` 的 `cl100k_base` 编码（与 DeepSeek 兼容），未安装时回退到字符启发式（CJK 1 token/字，ASCII 约 1 token/4 字符）。

服务端面板的 `/admin/api/stats` 现在还返回：

| 字段 | 说明 |
|------|------|
| `success_rate` | 0.0..1.0 |
| `p50_latency_ms` / `p95_latency_ms` / `p99_latency_ms` | 最近 1024 次请求的延迟百分位 |
| `latency_window_size` | 当前环形缓冲已用大小 |
| `total_prompt_tokens` / `total_completion_tokens` | 整个进程生命周期的累计 tokens |

---

## 安全建议（生产部署 checklist）

部署到公网前请按顺序检查：

1. **修改默认 admin 密码**：`.env` 中 `DEEPSEEK_ADMIN_PASSWORD=…`（16+ 字符随机）。默认密码 + 公网绑定时服务**会拒绝启动**。
2. **设置 Fernet 加密 key**：`DEEPSEEK_ENCRYPTION_KEY=…`，生成方式 `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`。已有明文 `data/accounts.json` 会在首次启动时自动迁移，原始文件保留为 `accounts.json.v1.bak`。
3. **设置 API key**：登录 WebUI，在「设置」页创建客户端 Key，并将创建时显示的完整值保存到调用方。Key 哈希保存在 `data/api_keys.sqlite3`；`ALLOW_UNAUTHENTICATED_API` 保持 `false`。
4. **配置 CORS 白名单**：`ALLOWED_ORIGINS=https://app.example.com`，而不是依赖默认同源策略。
5. **如果用反向代理**（nginx、Caddy、Traefik）：设置 `TRUSTED_PROXIES=10.0.0.0/8`（你的代理网段）让 admin 限流能正确识别客户端 IP。
6. **绑定地址**：默认 `127.0.0.1`。如果要走反向代理，保持 loopback；如果直连公网，设置 `HOST=0.0.0.0` 并确保前置有 TLS + WAF。
7. **使用 HTTPS**：限流响应头里包含 token 计数和 IP；admin 端点需要 HTTPS 保护。强烈建议配合 Cloudflare / Caddy 等前端使用。
8. **定期轮换**：token / cookie 90 天左右主动续期；`accounts.json` 是敏感文件，确保所在分区权限 `0o700` / NTFS ACL 限制。
9. **限流**：保留默认 `CLIENT_RPM_PER_KEY=60`、`CLIENT_RPM_PER_IP=120`，按你的实际流量调整。
10. **监控**：`GET /health` 适合做存活探针；`/admin/api/stats` 适合做业务指标抓取。

---

## 架构概览

```
┌──────────────────────────────────────────────────────────────────┐
│                          管理面板                                │
│  ┌──────────────────────┐  ┌──────────────────────────────────┐ │
│  │ 前端 SPA (webui/)    │  │ Admin API (admin.py)             │ │
│  │ · 概览面板           │◄─┤ · 密码认证 / 统计 / 账号管理     │ │
│  │ · 账号池管理         │  │ · 重登录触发                     │ │
│  └──────────────────────┘  └───────┬──────────────────────────┘ │
└────────────────────────────────────┼────────────────────────────┘
                                     │
┌──────────────┐     OpenAI 格式      ┌────────▼─────────────────┐
│  客户端应用   │ ◄───── SSE ────────► │   FastAPI Server          │
│ (NextChat,    │                      │   (server.py)             │
│  OpenWebUI,   │                      │                           │
│  custom)      │                      │  ┌─────────────────────┐  │
└──────────────┘                      │  │  AccountPool         │  │
                                      │  │  (account_pool.py)   │  │
                                      │  │  · 多账号轮询选择    │  │
                                      │  │  · 状态追踪          │  │
                                      │  │  · 健康检查          │  │
                                      │  └────────┬────────────┘  │
                                      │           │                │
                                      │  ┌────────▼────────────┐  │
                                      │  │  ChatAdapter         │  │
                                      │  │  (adapter.py)        │  │
                                      │  │  - PoW solving       │  │
                                      │  │  - Session mgmt      │  │
                                      │  │  - SSE parsing       │  │
                                      │  │  - Fragment stm      │  │
                                      │  └────────┬────────────┘  │
                                      │           │                │
                                      │  ┌────────▼────────────┐  │
                                      │  │ StreamSieve          │  │
                                      │  │ (tool_sieve.py)      │  │
                                      │  │ - DSML detection     │  │
                                      │  │ - Real-time sep      │  │
                                      │  └──────────────────────┘  │
                                      │                           │
                                      │  ┌──────────────────────┐  │
                                      │  │ DSML Parser          │  │
                                      │  │ (tool_dsml.py)       │  │
                                      │  │ - XML parsing        │  │
                                      │  │ - Format conv        │  │
                                      │  └──────────────────────┘  │
                                      └──────────┬────────────────┘
                                                 │
                                      DeepSeek 原生协议
                                      (PoW + SSE)
                                                 │
                                      ┌──────────▼────────────┐
                                      │  chat.deepseek.com    │
                                      │  (DeepSeek Chat API)  │
                                      └───────────────────────┘
```

### 核心组件

| 文件 | 职责 |
|------|------|
| `server.py` | FastAPI 服务器，Provider 路由，THINKING 控制，OpenAI SSE 格式化 |
| `adapter.py` | DeepSeek 协议适配器 — PoW 挑战求解，会话创建/管理，原生 SSE 解析，fragment 状态机 |
| `zai_adapter.py` | z.ai 协议适配器 — v2 completion、HMAC 签名、浏览器指纹 query、SSE 解析 |
| `provider_registry.py` | Provider 常量、模型到 Provider 的归一化与路由 |
| `anthropic_format.py` | Anthropic `/v1/messages` 格式转换 — 请求解析、响应组装、SSE 生成 |
| `tool_sieve.py` | StreamSieve 流式筛分引擎 — 逐字符检测 DSML 工具调用标签，实时分离正文与工具调用 |
| `tool_dsml.py` | DSML 解析器/生成器 — XML 格式的 DSML ↔ OpenAI tool_calls 双向转换 |
| `account_pool.py` | 多账号管理 — CRUD、状态追踪（idle/busy/error）、轮询分配、健康检查 |
| `admin.py` | 管理后台 API — 密码认证、请求统计、账号池增删查改、重登录触发 |
| `webui-new/` | React + TypeScript 管理面板（构建后由 `/webui/*` 提供） |
| `sha3_wasm_bg.wasm` | WASM 二进制，用于 DeepSeekHashV1 工作量证明求解 |

### 请求生命周期

1. **客户端请求** → `/v1/chat/completions` 收到 OpenAI 格式请求
2. **能力解析** → `server.py: resolve_model_controls()` 按 `MODEL_ROUTES` → `THINKING`/`SEARCH` → 请求参数的顺序确定 `thinking_enabled` / `search_enabled`（`model_type` 仅作协议字段，恒为 `"default"`）
3. **DSML 注入** → 如有 `tools`，`build_dsml_tool_prompt()` 生成 DSML 格式系统提示词
4. **PoW 求解** → `adapter.py` 请求并求解 DeepSeekHashV1 挑战
5. **会话创建** → 创建新的 DeepSeek Chat 会话（可选复用）
6. **请求发送** → 以原生格式发送到 `/api/v0/chat/completion`
7. **响应处理**:
   - 非流式：解析 SSE 收集全部内容 → 检测工具调用 → 返回 OpenAI 格式
   - 流式：逐 token 转发 → StreamSieve 实时筛分 → 格式化 OpenAI SSE

---

## 文件结构

```
├── server.py            # FastAPI 服务器主入口
├── adapter.py           # DeepSeek 协议适配器 (PoW, 会话, SSE)
├── zai_adapter.py       # z.ai 协议适配器 (v2, 签名, 指纹, SSE)
├── provider_registry.py # Provider / 模型路由
├── anthropic_format.py  # Anthropic /v1/messages 格式转换
├── responses_format.py  # OpenAI Responses 格式转换
├── tool_sieve.py        # StreamSieve 流式工具调用检测引擎
├── tool_dsml.py         # DSML 解析器/生成器
├── account_pool.py      # 多账号池管理 (轮询、状态追踪、健康检查)
├── proxy_pool.py        # 代理池与账号出口绑定
├── runtime_config.py    # 模型与定价可视化配置
├── admin.py             # 管理后台 API 端点
├── webui-new/           # React 管理面板源码
├── scripts/
│   └── sync_sub2api.py  # Sub2API 同步器
├── sha3_wasm_bg.wasm    # WASM PoW 求解器
├── requirements.txt     # Python 依赖
├── .env.example         # 环境变量模板
├── .env                 # 你的实际配置（已 .gitignore）
├── start.bat            # Windows 启动脚本
├── AGENTS.md            # AI Agent 参考文档
└── README.md            # 本文件
```

---

## 常见问题

### Q: 启动后请求返回 502 Bad Gateway

原因通常是凭证失效或网络问题：

1. 在 WebUI「账号池」检查账号凭证状态；非交互部署则检查 `data/accounts.json` 中的凭证是否仍然有效
2. 检查能否访问 `chat.deepseek.com`（可能需要代理）
3. 检查控制台日志中的具体错误信息

### Q: 流式输出中只有 reasoning_content 没有 content

在 `thinking_mode=true` 下，模型会先输出完整的推理过程再输出回答。如果你看到只有推理没有内容：

1. 等待模型完成推理（响应尚未结束）
2. 上游偶尔只回 THINK 片段就结束（实测可复现），此时 `content` 为空但 `finish_reason` 正常，属上游行为，重试即可
3. 如果真是 bug：确认服务端版本，检查 `finish_reason` 是否正常输出。已知旧版本可能缺少 `finish_reason: "stop"` 帧

### Q: 如何关闭思维链/推理展示？

设置环境变量 `THINKING=disabled`，即使请求中 `thinking_mode=true` 也不会输出 `reasoning_content`。

### Q: 一直转圈 / 响应极慢

- 开启思考（`thinking_mode=true`）本身就更慢，模型在做完整推理
- PoW 求解在低性能机器上可能需要数秒
- 检查网络到 `chat.deepseek.com` 的延迟

### Q: 多轮对话里模型「失忆」，不记得上一轮说的话

先确认调用方式，两种都支持，但要求不同：

| 调用风格 | 要求 | 行为 |
|---|---|---|
| 每轮重发完整 `messages`（OpenAI 规范） | 带上 `X-Conversation-Id` 或 `user` 才会复用上游会话 | 第二轮起只把**新增的**那部分发给上游，省 token |
| 每轮只发**最新一条** user 消息 | 必须带 `X-Conversation-Id` 或 `user` | 复用同一会话与父消息链，整段 prompt 重发一次以补齐上下文 |

排查清单：

1. 是否带了 `X-Conversation-Id`（或 OpenAI 顶层 `user` / Anthropic `metadata.user_id`）？不带就每轮新建会话，必然失忆
2. 两轮之间是否隔了超过 `SESSION_CACHE_TTL`（默认 1800 秒）？
3. 是否在流式/非流式之间来回切换？v3.3.8 起两种模式共用同一会话缓存，此前的版本会因此各建一个会话
4. 日志里出现 `session_prompt_diverged` 是正常提示，表示这一轮走了「保留会话 + 重发全文」的路径

### Q: Windows 下 curl 请求中文返回 422

Windows bash curl 默认编码为 GBK，发送 JSON 时中文字符可能被错误编码。解决方式：

```bash
# 方式一：将请求体写入 JSON 文件
curl -X POST http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d @body.json

# 方式二：用 PowerShell（推荐）
Invoke-RestMethod -Uri http://localhost:8080/v1/chat/completions `
  -Method POST `
  -ContentType "application/json" `
  -Body '{"model":"deepseek-chat","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### Q: `.env` 修改后没有生效

需要重启服务器进程。FastAPI 的 reload 模式不会重新加载环境变量：

```bash
# 先杀掉旧进程
taskkill /F /IM python.exe
# 再重新启动
python -m uvicorn server:app --host 0.0.0.0 --port 8080
```

### Q: 浏览器插件（如 YouTube 字幕翻译）请求失败，日志显示 405 Method Not Allowed

某些 Chrome 插件在使用 OpenAI 兼容接口前会先发送 `OPTIONS` 预检请求（CORS preflight）来检查服务器是否允许跨域访问。如果服务端未处理 `OPTIONS` 请求，会返回 `405 Method Not Allowed`，导致插件无法正常使用。

**解决方法：** 本项目已内置 CORS 中间件（`CORSMiddleware`），默认允许所有来源、方法和请求头。如果仍遇到跨域问题，请确认版本已包含 `server.py` 中的 CORS 配置。

### Q: 支持多轮对话吗？

不支持。每次请求都是独立的，服务器会创建新的 DeepSeek Chat 会话。多轮会话支持需要在应用层面（如 NextChat）维护上下文。

### Q: 如何查看 PoW 求解过程和调试信息？

适配器使用 `httpx` 发送请求，设置日志级别可查看详细请求/响应：

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

### Q: Token/凭证多久过期？

DeepSeek 的 Token 有效期不明确。如果遇到 `401` 或 `403` 响应，重新登录 chat.deepseek.com 并更新 `.env` 中的凭证。

---

## 环境变量参考

### 核心配置

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `HOST` | `127.0.0.1` | 否 | 监听地址。`0.0.0.0` 配合默认 admin 密码时拒绝启动 |
| `PORT` | `8080` | 否 | 服务器监听端口 |
| `ALLOW_UNAUTHENTICATED_API` | `false` | 否 | 是否允许 `/v1/*` 无鉴权访问；公网部署不要开启 |
| `ALLOW_INSECURE_PUBLIC_DEFAULTS` | `false` | 否 | 显式确认在公网上使用默认密码（不建议） |

客户端 API Key 在 WebUI「设置」页管理，SQLite 文件中只保存哈希。旧版 `.env` 的 `API_KEYS` / `DEEPSEEK_API_KEY` 仅在首次创建 Key 数据库时导入一次；确认客户端已可用后，可从 `.env` 删除旧配置。

### 账号池存储

| 变量 | 默认值 | 必填 | 说明 |
|------|--------|------|------|
| `ACCOUNT_STORE_PATH` | `data/accounts.json` | 否 | 面板持久化账号保存路径 |
| `PROXY_STORE_PATH` | `data/proxies.json` | 否 | 代理池持久化路径 |
| `PROXY_CHECK_URL` | `https://chat.deepseek.com/` | 否 | 代理池连通性检测目标 URL |

### 多账号保活

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `KEEPALIVE_ENABLED` | `false` | 启用进程内多账号调度器 |
| `KEEPALIVE_STORE_PATH` | `data/keepalive.sqlite3` | Job、租约和退避数据库 |
| `KEEPALIVE_TICK_SECS` | `30` | 调度扫描周期 |
| `KEEPALIVE_GLOBAL_CONCURRENCY` | `4` | 全局并发上限 |
| `KEEPALIVE_DEEPSEEK_CONCURRENCY` | `2` | DeepSeek 并发上限 |
| `KEEPALIVE_ZAI_CONCURRENCY` | `2` | z.ai 并发上限 |
| `KEEPALIVE_DEEPSEEK_INTERVAL` | `900` | DeepSeek 默认周期（秒） |
| `KEEPALIVE_ZAI_INTERVAL` | `900` | z.ai 默认周期（秒） |
| `KEEPALIVE_RETRY_BASE_SECS` | `60` | 临时失败初始退避 |
| `KEEPALIVE_RETRY_MAX_SECS` | `3600` | 最大退避 |
| `KEEPALIVE_SHARED_SECRET` | `""` | 独立单账号 worker 的共享密钥 |

### 模型行为

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `MODEL_NAME` | `"deepseek-chat"` | DeepSeek 公开模型名；WebUI 的运行时模型配置优先于此值 |
| `ZAI_MODEL_NAME` | `"glm-5.3-flash"` | z.ai 公开模型名；`glm-*` 请求自动路由到 z.ai 账号 |
| `ZAI_MODEL_DISPLAY_NAME` | `"GLM-5.3 Flash"` | `/v1/models` 中 z.ai 模型的显示名称 |
| `ZAI_UPSTREAM_MODEL` | `"x-preview-l"` | 公开 `glm-5.3-flash` 对应的 z.ai completion 模型 ID |
| `KIMI_MODEL_NAME` | `"kimi-k2.6"` | Kimi 默认公开模型名 |
| `KIMI_UPSTREAM_MODEL` | `"k2d6-chat"` | Kimi K2.6 对应的 Web Chat 模型 ID |
| `QWEN_MODEL_NAME` | `"qwen3.7-max"` | 千问默认公开模型名 |
| `QWEN_UPSTREAM_MODEL` | `"Qwen3.7-Max"` | 千问 Web Chat 上游模型 ID |
| `THINKING` | `"auto"` | `auto` / `enabled` / `disabled` — 控制 `thinking_enabled`（原「专家模式」开关，**这里**决定思考与否） |
| `SEARCH` | `"auto"` | `auto` / `enabled` / `disabled` — 控制 `search_enabled` |
| `MODEL_ROUTES` | `""` | 可选高级覆盖：JSON 路由表，把 DeepSeek model 名映射到 thinking/search 能力预设。配置后列出其键；不配置时列出 `MODEL_NAME`。z.ai 模型始终根据 `ZAI_MODEL_NAME` 追加 |
| `MODE` | `"auto"` | **已废弃**，仅兼容旧配置：`expert` ≡ `THINKING=enabled`，`quick` ≡ `THINKING=disabled` |

### 管理面板

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_ADMIN_PASSWORD` | `"admin"` | 初始/恢复用管理密码（公网部署必须修改） |
| `ADMIN_PASSWORD_STORE_PATH` | `data/admin_auth.json` | WebUI 修改后的 PBKDF2 密码 verifier 路径 |

从 WebUI 修改密码后，`data/admin_auth.json` 优先于 `.env`；所有旧管理会话会立即失效。

### 限流 & 会话

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ENABLE_RATE_LIMIT` | `true` | 客户端限流总开关 |
| `CLIENT_RPM_PER_KEY` | `60` | 每 API key 每分钟请求数 |
| `CLIENT_RPM_PER_IP` | `120` | 每 IP 每分钟请求数 |
| `SESSION_CACHE_TTL` | `600` | 多轮会话缓存 TTL（秒），0 = 禁用多轮 |

### 安全 & 加密

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ALLOWED_ORIGINS` | `""` | CORS 允许的 origin 列表（逗号分隔），空 = 同源 |
| `ALLOW_CORS_CREDENTIALS` | `false` | 是否允许带 credentials 的跨域（需配合 `ALLOWED_ORIGINS`） |
| `TRUSTED_PROXIES` | `""` | 信任的代理 IP/CIDR（设置后 `X-Forwarded-For` 才会被采纳） |
| `DEEPSEEK_ENCRYPTION_KEY` | `""` | Fernet key；设置后账号 token/cookies 与代理池 URL 自动加密 |

### 日志 & 反检测

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |
| `LOG_FORMAT` | `json` | `json`（生产）或 `text`（开发） |
| `DEEPSEEK_IMPERSONATE` | `chrome131` | curl_cffi TLS 指纹 profile |
| `DEEPSEEK_JITTER_SECS` | `0.0` | 调用间随机抖动（秒） |
| `DEEPSEEK_TRANSIENT_RETRY_AFTER` | `5` | 上游瞬时故障（`generation_err` / 「服务器暂时不可用」）返回的 `Retry-After` 秒数 |
| `DSML_MAX_BUFFER_BYTES` | `1048576` | StreamSieve 捕获缓冲上限 |
| `DISABLE_AUTO_RECOVER` | `false` | 设为 true 禁用账号自动恢复 |

---

## 依赖

| 包 | 最低版本 | 用途 |
|----|----------|------|
| `fastapi` | ≥0.100.0 | Web 框架 |
| `uvicorn` | ≥0.20.0 | ASGI 服务器 |
| `httpx` | ≥0.24.0 | HTTP 客户端（用于调用 DeepSeek API） |
| `wasmtime` | ≥14.0.0 | WASM 运行时（PoW 求解） |
| `python-dotenv` | ≥1.0.0 | `.env` 文件加载 |

---

## 许可

本项目使用 **Unlicense** 协议正式发布到公有领域（public domain）。

```
This is free and unencumbered software released into the public domain.
```

你可以自由地复制、修改、发布、使用、编译、出售或分发本软件，无论用于商业或非商业目的，无论以任何形式。

**不提供任何保证**，不保证服务的可用性、准确性、稳定性。使用本项目所产生的任何后果由使用者自行承担。

---

## 致谢

- [DeepSeek](https://deepseek.com) — 优秀的 AI 模型与平台
- OpenAI — API 标准格式参考
- [wasmtime-py](https://github.com/bytecodealliance/wasmtime-py) — WASM 运行时
- [@minhmc2007](https://github.com/minhmc2007) — 贡献多轮会话复用修复（PR #10，v3.3.0）
- [@Zhangkc](https://github.com/Zhangkc) — 贡献请求趋势图修复：stats 采样器读取不存在的 avg_latency_ms 属性（PR #14，v3.3.2）
