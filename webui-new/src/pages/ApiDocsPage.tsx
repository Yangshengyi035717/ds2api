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
import { Badge } from '@/components/ui/badge'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'

const ORIGIN = window.location.origin
const API_BASE = `${ORIGIN}/v1`

const OPENAI_PYTHON = `from openai import OpenAI

client = OpenAI(
    api_key="YOUR_API_KEY",
    base_url="${API_BASE}",
)

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "你好，介绍一下你自己"},
    ],
)

print(response.choices[0].message.content)`

const OPENAI_STREAM = `curl -N "${ORIGIN}/v1/chat/completions" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "hello"}],
    "stream": true,
    "thinking_mode": false,
    "search_enabled": false
  }'`

const OPENAI_REQUEST = `{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "你好"}
  ],
  "stream": false,
  "thinking_mode": false,
  "search_enabled": false
}`

const ANTHROPIC_PYTHON = `from anthropic import Anthropic

client = Anthropic(
    api_key="YOUR_API_KEY",
    base_url="${ORIGIN}",
)

response = client.messages.create(
    model="deepseek-chat",
    max_tokens=1024,
    messages=[
        {"role": "user", "content": "你好，介绍一下你自己"},
    ],
)

print(response.content)`

const ANTHROPIC_CURL = `curl -N "${ORIGIN}/v1/messages" \\
  -H "x-api-key: YOUR_API_KEY" \\
  -H "anthropic-version: 2023-06-01" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "deepseek-chat",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "hello"}],
    "stream": true
  }'`

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
  return (
    <div className="space-y-4 animate-fade-in">
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

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(420px,560px)]">
        <div className="min-w-0 space-y-4">
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
                    推荐模型名
                  </div>
                  <code className="font-mono text-[11px] text-muted-foreground">
                    deepseek-chat
                  </code>
                </div>
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
            <TabsList className="grid h-auto w-full grid-cols-2 sm:grid-cols-4">
              <TabsTrigger value="quick">快速开始</TabsTrigger>
              <TabsTrigger value="openai">OpenAI</TabsTrigger>
              <TabsTrigger value="anthropic">Anthropic</TabsTrigger>
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
                  <CodeBlock code={OPENAI_PYTHON} title="Python" language="python" />
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
                  <FieldRow name="模型" description="deepseek-chat" />
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
                  <CodeBlock code={OPENAI_REQUEST} title="请求体" language="json" />
                  <div>
                    <FieldRow name="model" description="推荐固定为 deepseek-chat；旧模型名仅作为别名。" />
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
                  <CodeBlock code={OPENAI_STREAM} title="PowerShell / curl.exe" language="bash" />
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
                    <FieldRow name="model / messages / max_tokens" description="使用 Anthropic Messages 格式；模型推荐 deepseek-chat。" />
                    <FieldRow name="thinking" description='传 {"type":"enabled"} 请求思考内容。' />
                  </div>
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-base">Anthropic SDK</CardTitle>
                </CardHeader>
                <CardContent className="space-y-3">
                  <CodeBlock code={ANTHROPIC_PYTHON} title="Python" language="python" />
                  <CodeBlock code={ANTHROPIC_CURL} title="curl" language="bash" />
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
                  <p>2. 账号池中至少有一个 idle 状态的 DeepSeek 账号。</p>
                  <p>3. 使用 WebUI 创建的客户端 API Key，不要在客户端保存管理密码。</p>
                  <p>4. 默认只监听 127.0.0.1；跨机器调用需要修改 HOST 并配置反向代理与强密码。</p>
                  <p>5. 修改 .env 后必须重启服务才能生效。</p>
                </CardContent>
              </Card>
            </TabsContent>
          </Tabs>
        </div>

        <div className="min-w-0 xl:self-start">
          <ApiDebugger />
        </div>
      </div>
    </div>
  )
}
