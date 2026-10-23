import { useEffect, useState } from 'react'
import {
  BookOpen,
  Braces,
  CircleAlert,
  KeyRound,
  ListChecks,
  ShieldCheck,
  Sparkles,
  Terminal,
} from 'lucide-react'
import { ApiDebugger } from '@/components/api-docs/ApiDebugger'
import { CodeBlock } from '@/components/api-docs/CodeBlock'
import { PageHeader } from '@/components/layout/PageHeader'
import { ModelLabel } from '@/components/models/ModelLabel'
import { ProviderIcon } from '@/components/models/ProviderIcon'
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Select, SelectContent, SelectItem, SelectTrigger } from '@/components/ui/select'
import { get } from '@/lib/api'
import type { ModelsResponse, RuntimeConfigResponse } from '@/lib/types'
import { MIMO_REQUIRED_COOKIES } from '@/lib/providers'

const ORIGIN = window.location.origin
const API_BASE = `${ORIGIN}/v1`

function openAiPython(modelName: string) {
  return `from openai import OpenAI

client = OpenAI(
    api_key="YOUR_API_KEY",
    base_url="${API_BASE}",
)

response = client.chat.completions.create(
    model="${modelName}",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "你好，介绍一下你自己"},
    ],
)

print(response.choices[0].message.content)`
}

function openAiStream(modelName: string) {
  return `curl -N "${ORIGIN}/v1/chat/completions" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${modelName}",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": true,
    "thinking_mode": false,
    "search_enabled": false
  }'`
}

function openAiRequest(modelName: string) {
  return `{
  "model": "${modelName}",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "你好"}
  ],
  "stream": false,
  "thinking_mode": false,
  "search_enabled": false
}`
}

function responsesRequest(modelName: string) {
  return `curl -N "${ORIGIN}/v1/responses" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${modelName}",
    "input": "你好，介绍一下你自己",
    "stream": true
  }'`
}

function anthropicPython(modelName: string) {
  return `from anthropic import Anthropic

client = Anthropic(
    api_key="YOUR_API_KEY",
    base_url="${ORIGIN}",
)

response = client.messages.create(
    model="${modelName}",
    max_tokens=1024,
    messages=[
        {"role": "user", "content": "你好，介绍一下你自己"},
    ],
)

print(response.content)`
}

function anthropicCurl(modelName: string) {
  return `curl -N "${ORIGIN}/v1/messages" \\
  -H "x-api-key: YOUR_API_KEY" \\
  -H "anthropic-version: 2023-06-01" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${modelName}",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "hello"}],
    "stream": true
  }'`
}

function sub2apiAccount(modelName: string) {
  return `{
  "name": "DS2API",
  "platform": "deepseek",
  "type": "apikey",
  "credentials": {
    "api_key": "YOUR_API_KEY",
    "base_url": "${ORIGIN}",
    "account_mode": "payg",
    "api_protocol": "chat_completions",
    "model_mapping": {
      "deepseek-chat": "${modelName}",
      "${modelName}": "${modelName}"
    }
  },
  "rate_multiplier": 1.0,
  "concurrency": 2,
  "priority": 10
}`
}

function MethodPath({ method, path }: { method: 'GET' | 'POST'; path: string }) {
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/30 px-3 py-2">
      <Badge variant={method === 'GET' ? 'secondary' : 'default'}>{method}</Badge>
      <code className="break-all font-mono text-sm">{path}</code>
    </div>
  )
}

function FieldRow({ name, description }: { name: string; description: string }) {
  return (
    <div className="grid gap-1 border-b py-2 last:border-b-0 sm:grid-cols-[160px,1fr] sm:gap-4">
      <code className="font-mono text-xs text-primary">{name}</code>
      <span className="text-xs leading-relaxed text-muted-foreground">{description}</span>
    </div>
  )
}

function ErrorRow({
  status,
  title,
  description,
}: {
  status: string
  title: string
  description: string
}) {
  return (
    <div className="grid gap-2 rounded-md border p-3 sm:grid-cols-[80px,1fr]">
      <Badge variant="warning" className="h-fit w-fit font-mono">
        {status}
      </Badge>
      <div className="space-y-1">
        <div className="text-sm font-medium">{title}</div>
        <div className="text-xs leading-relaxed text-muted-foreground">{description}</div>
      </div>
    </div>
  )
}

export default function ApiDocsPage() {
  const [modelName, setModelName] = useState('deepseek-chat')
  const [models, setModels] = useState<ModelsResponse['data']>([])

  useEffect(() => {
    let cancelled = false
    Promise.all([
      get<RuntimeConfigResponse>('/admin/api/runtime-config'),
      get<ModelsResponse>('/v1/models'),
    ])
      .then(([config, modelList]) => {
        if (cancelled) return
        setModels(modelList.data)
        const configured = modelList.data.find((model) => model.id === config.model_name)
        setModelName(configured?.id ?? modelList.data[0]?.id ?? config.model_name)
      })
      .catch(() => {
        // Keep the documented default if runtime config cannot be loaded.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const zaiModel = models.find((model) => model.provider === 'zai')?.id
    ?? models.find((model) => model.id.startsWith('glm-'))?.id
    ?? 'glm-5.3-flash'
  const kimiModel = models.find((model) => model.provider === 'kimi')?.id
    ?? 'kimi-k2.6'
  const hunyuanModel = models.find((model) => model.provider === 'hunyuan')?.id
    ?? 'hunyuan-hy4-preview'
  const wenxinModel = models.find((model) => model.provider === 'wenxin')?.id
    ?? 'wenxin-smart-mode'
  const doubaoModel = models.find((model) => model.provider === 'doubao')?.id
    ?? 'doubao-seed'
  const mimoModel = models.find((model) => model.provider === 'mimo' && model.id === 'mimo-v2.6-pro')?.id
    ?? models.find((model) => model.provider === 'mimo')?.id
    ?? 'mimo-v2.6-pro'
  const exampleModels: ModelsResponse['data'] = models.length > 0
    ? models
    : [{ id: modelName }]
  const selectedExample = exampleModels.find((model) => model.id === modelName)

  return (
    <div className="w-full space-y-4 animate-fade-in">
      <PageHeader
        title="API 文档"
        description="OpenAI / Anthropic 兼容接口、客户端接入示例与内嵌在线调试"
        actions={
          <Badge variant="outline" className="h-7 gap-1.5">
            <BookOpen className="h-3.5 w-3.5" />
            本机服务
          </Badge>
        }
      />

      <Tabs defaultValue="docs" className="space-y-4">
        <TabsList className="grid h-auto w-full grid-cols-2">
          <TabsTrigger value="docs">接入文档</TabsTrigger>
          <TabsTrigger value="debug">在线调试</TabsTrigger>
        </TabsList>

        <TabsContent value="docs" className="mx-auto w-full max-w-[1180px] space-y-4">
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="flex items-center gap-2 text-base">
                <Braces className="h-4 w-4" />
                连接信息
              </CardTitle>
              <CardDescription>
                所有外部客户端都通过当前 HTTP 服务访问；API Key 只在请求头中传输。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <CodeBlock code={API_BASE} title="OpenAI Base URL" language="url" />

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="rounded-md border p-3">
                  <div className="mb-1 flex items-center gap-1.5 text-xs font-medium">
                    <KeyRound className="h-3.5 w-3.5" />
                    认证方式
                  </div>
                  <code className="break-all font-mono text-[11px] text-muted-foreground">
                    Authorization: Bearer YOUR_API_KEY
                  </code>
                </div>
                <div className="rounded-md border p-3">
                  <div className="mb-1 flex items-center gap-1.5 text-xs font-medium">
                    <Sparkles className="h-3.5 w-3.5" />
                    示例模型
                  </div>
                  <Select value={modelName} onValueChange={setModelName}>
                    <SelectTrigger className="h-8">
                      <div className="min-w-0 flex-1">
                        {selectedExample ? (
                          <ModelLabel
                            modelId={selectedExample.id}
                            provider={selectedExample.provider}
                            ownedBy={selectedExample.owned_by}
                            displayName={selectedExample.display_name}
                            iconSize={15}
                          />
                        ) : (
                          <ModelLabel modelId={modelName} iconSize={15} />
                        )}
                      </div>
                    </SelectTrigger>
                    <SelectContent>
                      {exampleModels.map((model) => (
                        <SelectItem key={model.id} value={model.id}>
                          <ModelLabel
                            modelId={model.id}
                            provider={model.provider}
                            ownedBy={model.owned_by}
                            displayName={model.display_name}
                          />
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="flex flex-wrap gap-2">
                {models.map((model) => (
                  <Badge
                    key={model.id}
                    variant={model.provider === 'zai' ? 'outline' : 'secondary'}
                    className="gap-1.5"
                  >
                    <ProviderIcon provider={model.provider ?? model.owned_by} size={14} />
                    {model.id}
                    <span className="opacity-60">{model.provider || model.owned_by}</span>
                  </Badge>
                ))}
              </div>

              <div className="flex items-start gap-2 rounded-md border border-primary/20 bg-primary/5 p-3 text-xs leading-relaxed text-muted-foreground">
                <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-primary" />
                <span>
                  WebUI 调试器留空 API Key 时会复用当前管理会话；第三方应用必须使用
                  「设置 → 客户端 API Key」创建的 Key。
                </span>
              </div>
            </CardContent>
          </Card>

          <Tabs defaultValue="quick">
            <TabsList className="flex h-auto w-full flex-wrap justify-start gap-1">
              <TabsTrigger value="quick">快速开始</TabsTrigger>
              <TabsTrigger value="openai">OpenAI</TabsTrigger>
              <TabsTrigger value="responses">Responses</TabsTrigger>
              <TabsTrigger value="anthropic">Anthropic</TabsTrigger>
              <TabsTrigger value="zai">z.ai</TabsTrigger>
              <TabsTrigger value="kimi">Kimi</TabsTrigger>
              <TabsTrigger value="hunyuan">混元</TabsTrigger>
              <TabsTrigger value="wenxin">文心</TabsTrigger>
              <TabsTrigger value="doubao">豆包</TabsTrigger>
              <TabsTrigger value="mimo">小米 MiMo</TabsTrigger>
              <TabsTrigger value="sub2api">Sub2API</TabsTrigger>
              <TabsTrigger value="errors">错误与限制</TabsTrigger>
            </TabsList>

            <TabsContent value="quick" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-base">
                    <Terminal className="h-4 w-4" />
                    Python OpenAI SDK
                  </CardTitle>
                  <CardDescription>
                    适用于 OpenAI、LangChain OpenAI-compatible、Cherry Studio 等客户端。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <CodeBlock code={openAiPython(modelName)} title="Python" language="python" />
                  <CodeBlock
                    code=".\.venv\Scripts\python.exe -m pip install openai"
                    title="安装 SDK（项目虚拟环境）"
                    language="powershell"
                  />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">通用客户端配置</CardTitle>
                </CardHeader>
                <CardContent className="space-y-1">
                  <FieldRow name="接口类型" description="OpenAI Compatible / OpenAI 兼容" />
                  <FieldRow name="Base URL" description={API_BASE} />
                  <FieldRow name="API Key" description="WebUI 设置页生成的客户端 API Key" />
                  <FieldRow name="模型" description={modelName} />
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="openai" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Chat Completions</CardTitle>
                  <CardDescription>OpenAI 兼容主接口，支持流式、思考与联网搜索。</CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/chat/completions" />
                  <CodeBlock code={openAiRequest(modelName)} title="请求体" language="json" />
                  <div>
                    <FieldRow name="model" description={`当前公开模型名为 ${modelName}；可在「设置」中修改。`} />
                    <FieldRow name="messages" description="OpenAI 消息数组，支持 system/user/assistant/tool。" />
                    <FieldRow name="stream" description="true 返回 SSE；false 返回完整 JSON。" />
                    <FieldRow name="thinking_mode" description="true 请求推理内容，是否生效仍受服务端 THINKING 配置影响。" />
                    <FieldRow name="search_enabled" description="true 允许模型联网搜索，受服务端 SEARCH 配置影响。" />
                    <FieldRow name="temperature / top_p / max_tokens" description="当前后端读取后忽略，不参与上游透传。" />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">流式 cURL</CardTitle>
                  <CardDescription>使用 -N 关闭客户端缓冲，实时查看 SSE。</CardDescription>
                </CardHeader>
                <CardContent>
                  <CodeBlock code={openAiStream(modelName)} title="PowerShell / curl.exe" language="bash" />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">多轮会话</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 text-sm text-muted-foreground">
                  <p>
                    由客户端在 <code className="font-mono">messages</code> 中重发完整历史，
                    服务端以客户端上下文为准。
                  </p>
                  <p>
                    如需复用上游会话，可传请求头
                    <code className="mx-1 font-mono">X-Conversation-Id: your-session-id</code>
                    或 OpenAI 顶层 <code className="font-mono">user</code> 字段。
                  </p>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="responses" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">OpenAI Responses</CardTitle>
                  <CardDescription>
                    兼容 Codex / Responses API；内部转换为 Chat Completions 内核，并按模型路由到对应提供商账号池。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/responses" />
                  <CodeBlock
                    code={responsesRequest(modelName)}
                    title="流式 Responses"
                    language="bash"
                  />
                  <div>
                    <FieldRow name="input" description="支持字符串或 Responses input item 数组。" />
                    <FieldRow name="instructions" description="转换为 system 指令。" />
                    <FieldRow name="stream" description="true 返回 Responses SSE 事件；false 返回 response 对象。" />
                    <FieldRow name="tools" description="支持 function 工具；内置 web_search 会映射为 DS2API 搜索开关，视觉工具不支持。" />
                    <FieldRow name="reasoning.effort" description="非 none 时请求 DS2API 思考模式。" />
                    <FieldRow name="/responses" description="同一接口的无 /v1 别名，兼容 Sub2API DeepSeek 平台 ResponsesPath。" />
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="anthropic" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Anthropic Messages</CardTitle>
                  <CardDescription>
                    面向 Anthropic SDK / Claude Code 的兼容端点，复用同一模型和账号池。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/messages" />
                  <div>
                    <FieldRow name="Base URL" description={ORIGIN} />
                    <FieldRow name="认证头" description="x-api-key: YOUR_API_KEY；也接受 Authorization: Bearer。" />
                    <FieldRow name="anthropic-version" description="Anthropic SDK 会发送；服务端不依赖其值。" />
                    <FieldRow name="model / messages / max_tokens" description={`使用 Anthropic Messages 格式；当前模型名为 ${modelName}。`} />
                    <FieldRow name="thinking" description='传 {"type":"enabled"} 请求思考内容。' />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Anthropic SDK</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <CodeBlock code={anthropicPython(modelName)} title="Python" language="python" />
                  <CodeBlock code={anthropicCurl(modelName)} title="curl" language="bash" />
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="zai" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">z.ai / GLM-5.3 Flash</CardTitle>
                  <CardDescription>
                    z.ai 与 DeepSeek 共用 OpenAI / Responses / Anthropic 接口，
                    通过请求中的 model 字段自动选择 z.ai 账号池。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/chat/completions" />
                  <CodeBlock
                    code={openAiRequest(zaiModel)}
                    title="OpenAI Chat Completions"
                    language="json"
                  />
                  <CodeBlock
                    code={openAiStream(zaiModel)}
                    title="流式 cURL"
                    language="bash"
                  />
                  <div>
                    <FieldRow name="公开模型名" description={`${zaiModel}；请求会自动路由到 provider=zai 账号。`} />
                    <FieldRow name="内部模型映射" description="默认 glm-5.3-flash → x-preview-l；由 ZAI_UPSTREAM_MODEL 控制。" />
                    <FieldRow name="账号要求" description="z.ai 账号必须包含有效 token、user_id 和稳定 device_id。" />
                    <FieldRow name="thinking_mode" description="映射 z.ai enable_thinking / reasoning_effort。" />
                    <FieldRow name="search_enabled" description="映射 z.ai web search 能力。" />
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="kimi" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Kimi / Moonshot</CardTitle>
                  <CardDescription>
                    Kimi 使用 Connect JSON 流式协议；服务端把它转换为统一的
                    OpenAI / Anthropic SSE 响应。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/chat/completions" />
                  <CodeBlock
                    code={openAiRequest(kimiModel)}
                    title="OpenAI Chat Completions"
                    language="json"
                  />
                  <div>
                    <FieldRow name="公开模型名" description={`${kimiModel}；请求自动路由到 provider=kimi 账号。`} />
                    <FieldRow name="默认上游模型" description="kimi-k2.6 → k2d6-chat；可选 kimi-k3 / kimi-k2.8。" />
                    <FieldRow name="认证" description="access_token 约 15 分钟有效，服务端使用 refresh_token 自动续期。" />
                    <FieldRow name="协议" description="application/connect+json，按 Connect 帧解析 thinking/answer/done 事件。" />
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="hunyuan" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">腾讯混元 / Hunyuan HY4 Preview</CardTitle>
                  <CardDescription>
                    混元使用 AI Studio Web SSE 协议；服务端会创建上游会话并转换为统一的
                    OpenAI / Anthropic 流式响应。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/chat/completions" />
                  <CodeBlock
                    code={openAiRequest(hunyuanModel)}
                    title="OpenAI Chat Completions"
                    language="json"
                  />
                  <CodeBlock
                    code={openAiStream(hunyuanModel)}
                    title="流式 cURL"
                    language="bash"
                  />
                  <div>
                    <FieldRow name="公开模型名" description={`${hunyuanModel}；请求自动路由到 provider=hunyuan 账号。`} />
                    <FieldRow name="默认上游模型" description="hunyuan-hy4-preview → hy4-preview；由 HUNYUAN_UPSTREAM_MODEL 控制。" />
                    <FieldRow name="认证" description="需要包含 hunyuan_user 和 hunyuan_token 的 HttpOnly Cookie；退出登录后需重新导入。" />
                    <FieldRow name="协议" description="text/event-stream；解析 speech_type、text、meta 与 error 事件。" />
                    <FieldRow name="thinking_mode" description="映射 effort=high / no_think。" />
                    <FieldRow name="search_enabled" description="映射 supportInternetSearch。" />
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="wenxin" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">百度文心 / Wenxin Smart Mode</CardTitle>
                  <CardDescription>
                    文心使用 chat.baidu.com Web SSE 协议；服务端把生成事件转换为统一的
                    OpenAI / Anthropic 流式响应。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/chat/completions" />
                  <CodeBlock
                    code={openAiRequest(wenxinModel)}
                    title="OpenAI Chat Completions"
                    language="json"
                  />
                  <CodeBlock
                    code={openAiStream(wenxinModel)}
                    title="流式 cURL"
                    language="bash"
                  />
                  <div>
                    <FieldRow name="公开模型名" description={`${wenxinModel}；请求自动路由到 provider=wenxin 账号。`} />
                    <FieldRow name="默认上游模型" description="wenxin-smart-mode → smartMode；由 WENXIN_UPSTREAM_MODEL 控制。" />
                    <FieldRow name="认证" description="需要页面下发的 token 与 lid；可在账号池粘贴 conversation HAR 自动提取。" />
                    <FieldRow name="协议" description="text/event-stream；解析 basedata、message 与 generator 增量。" />
                    <FieldRow name="thinking_mode" description="映射 usedModel.modelFunction.thinkMode。" />
                    <FieldRow name="search_enabled" description="映射 usedModel.modelFunction.deepSearch。" />
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="doubao" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">豆包 / Doubao Seed</CardTitle>
                  <CardDescription>
                    豆包使用 chat/completion SSE 协议；服务端把浏览器生成事件转换为统一的
                    OpenAI / Anthropic 流式响应。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/chat/completions" />
                  <CodeBlock
                    code={openAiRequest(doubaoModel)}
                    title="OpenAI Chat Completions"
                    language="json"
                  />
                  <CodeBlock
                    code={openAiStream(doubaoModel)}
                    title="流式 cURL"
                    language="bash"
                  />
                  <div>
                    <FieldRow name="公开模型名" description={`${doubaoModel}；请求自动路由到 provider=doubao 账号。`} />
                    <FieldRow name="默认上游模型" description="doubao-seed → model_item_key=0；由 DOUBAO_UPSTREAM_MODEL 控制。" />
                    <FieldRow name="认证" description="需要包含 sessionid、ttwid、msToken 的登录 Cookie；退出登录后需重新导入。" />
                    <FieldRow name="协议" description="text/event-stream；解析 SSE_ACK、CHUNK_DELTA、STREAM_CHUNK 与 SSE_REPLY_END。" />
                    <FieldRow name="thinking_mode" description="映射 need_deep_think / reasoning_effort。" />
                  </div>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="mimo" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">小米 MiMo</CardTitle>
                  <CardDescription>
                    使用公开模型名选择 MiMo 账号池；客户端继续使用 DS2API API Key，
                    MiMo 登录 Cookie 在账号池中管理。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/v1/chat/completions" />
                  <CodeBlock
                    code={openAiRequest(mimoModel)}
                    title="OpenAI Chat Completions"
                    language="json"
                  />
                  <CodeBlock
                    code={openAiStream(mimoModel)}
                    title="流式 cURL"
                    language="bash"
                  />
                  <CodeBlock
                    code={openAiRequest('mimo-v2.6-flash')}
                    title="MiMo V2.6 Flash"
                    language="json"
                  />
                  <CodeBlock
                    code={openAiRequest('mimo-v2.6-pro-ultraspeed-studio')}
                    title="MiMo V2.6 Pro UltraSpeed Studio"
                    language="json"
                  />
                  <div>
                    <FieldRow name="公开模型名" description={`默认 mimo-v2.6-pro；可选 mimo-v2.6-flash 和 mimo-v2.6-pro-ultraspeed-studio。示例使用 ${mimoModel}，请求路由到 provider=mimo 账号。`} />
                    <FieldRow name="UltraSpeed 协议" description="使用 /fastchat/open-apis/bot/chat；正文中的 <think> 标签会实时拆分为 reasoning_content。" />
                    <FieldRow name="认证" description={`Cookie-only；必须包含 ${MIMO_REQUIRED_COOKIES.join('、')}，值中的双引号需要保留。`} />
                    <FieldRow name="账号字段" description="无需填写 Token；User ID 从 Cookie 的 userId 提取，不需要 Device ID、Session ID 或 Shield。" />
                    <FieldRow name="导入" description="在「账号池 → 添加账号 → 小米 MiMo 导入」粘贴完整 Cookie 或包含 Cookie 的 HAR；分别返回 source=mimo-cookie / har:mimo-cookie。" />
                    <FieldRow name="保活" description="只验证登录态并回写轮换 Cookie；Cookie 过期后需重新登录并导入。" />
                  </div>
                </CardContent>
              </Card>
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">MiMo Cookie 导入示例</CardTitle>
                  <CardDescription>
                    管理接口使用当前管理员会话，payload 也可传入完整 HAR 文本。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <MethodPath method="POST" path="/admin/api/accounts/import" />
                  <CodeBlock
                    code={JSON.stringify({
                      provider: 'mimo',
                      payload: 'userId=YOUR_USER_ID; xiaomichatbot_serviceToken="YOUR_SERVICE_TOKEN"; xiaomichatbot_ph="YOUR_PH"',
                    }, null, 2)}
                    title="请求体（占位示例）"
                    language="json"
                  />
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="sub2api" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Sub2API DeepSeek 平台</CardTitle>
                  <CardDescription>
                    推荐作为 API Key 上游接入，并将上游协议固定为 Chat Completions。
                  </CardDescription>
                </CardHeader>
                <CardContent className="space-y-3">
                  <CodeBlock
                    code={sub2apiAccount(modelName)}
                    title="账号配置"
                    language="json"
                  />
                  <div>
                    <FieldRow name="platform" description="deepseek；使用 DeepSeek 平台分类与调度。" />
                    <FieldRow name="type" description="apikey。Sub2API 的上游模型同步依赖该类型。" />
                    <FieldRow name="base_url" description={`${ORIGIN}；Sub2API 会追加 /v1/models 和 /v1/chat/completions。`} />
                    <FieldRow name="api_protocol" description="推荐 chat_completions；如需 Codex/Responses，可改为 responses。DS2API 同时提供 /v1/responses 和 /responses。" />
                    <FieldRow name="model_mapping" description={`将 Sub2API 客户端模型名映射到当前 DS2API 模型 ${modelName}。`} />
                    <FieldRow name="rate_multiplier" description="这是 Sub2API 本地账号倍率，不会从 DS2API 自动读取。" />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">可用发现接口</CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 text-sm text-muted-foreground">
                  <p><code className="font-mono">GET /v1/models</code>：模型 ID、显示名、能力与支持端点。</p>
                  <p><code className="font-mono">GET /v1/models/&#123;model&#125;</code>：单模型查询。</p>
                  <p><code className="font-mono">GET /v1/provider-manifest</code>：供外部同步器读取协议和模型能力。</p>
                  <p><code className="font-mono">POST /v1/responses</code>：Responses API 与 Codex 兼容入口。</p>
                  <p>可直接运行 <code className="font-mono">scripts/sync_sub2api.py</code> 同步账号、模型、价格与倍率。</p>
                </CardContent>
              </Card>
            </TabsContent>

            <TabsContent value="errors" className="space-y-4">
              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-base">
                    <CircleAlert className="h-4 w-4" />
                    常见错误
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  <ErrorRow
                    status="401"
                    title="API Key 无效"
                    description="检查 Authorization 是否为 Bearer 格式，或确认 Key 未被撤销。"
                  />
                  <ErrorRow
                    status="429"
                    title="请求限流"
                    description="可能触发客户端 RPM 限制或 DeepSeek 上游限流；降低并发后重试。"
                  />
                  <ErrorRow
                    status="502"
                    title="上游协议错误或空响应"
                    description="检查账号凭证、上游状态与项目日志；可尝试关闭思考或联网后重试。"
                  />
                  <ErrorRow
                    status="503"
                    title="服务未完成配置"
                    description="可能是尚未创建客户端 API Key、账号池为空，或所有账号均忙。"
                  />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2 text-base">
                    <ListChecks className="h-4 w-4" />
                    接入检查清单
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-2 text-sm text-muted-foreground">
                  <p>1. 当前服务地址必须能从客户端所在机器访问。</p>
                  <p>2. 账号池中至少有一个与请求模型所属 provider 对应的 idle 账号。</p>
                  <p>3. 使用 WebUI 创建的客户端 API Key，不要在客户端保存管理密码。</p>
                  <p>4. 默认只监听 127.0.0.1；跨机器调用需要修改 HOST 并配置反向代理与强密码。</p>
                  <p>5. 修改 .env 后必须重启服务才能生效。</p>
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </TabsContent>

        <TabsContent value="debug" className="w-full space-y-4">
          <ApiDebugger defaultModel={modelName} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
