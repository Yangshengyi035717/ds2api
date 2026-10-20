import { useEffect, useMemo, useRef, useState } from 'react'
import {
  CircleAlert,
  Eye,
  EyeOff,
  Play,
  Square,
  Terminal,
  Trash2,
} from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { CodeBlock } from '@/components/api-docs/CodeBlock'
import { useToast } from '@/hooks/use-toast'
import { consumeSse } from '@/lib/sse'
import { formatLatency } from '@/lib/utils'
import { useAuthStore } from '@/stores/auth'

type DebugEndpoint = 'models' | 'chat' | 'messages'
type JsonObject = Record<string, unknown>

interface DebugResult {
  status: number | null
  elapsedMs: number | null
  content: string
  reasoning: string
  finishReason: string
  raw: string
  error: string
}

interface StreamExtraction {
  content: string
  reasoning: string
  finishReason: string
}

const EMPTY_RESULT: DebugResult = {
  status: null,
  elapsedMs: null,
  content: '',
  reasoning: '',
  finishReason: '',
  raw: '',
  error: '',
}

const ENDPOINTS: Record<
  DebugEndpoint,
  { label: string; method: 'GET' | 'POST'; path: string }
> = {
  models: { label: 'GET /v1/models', method: 'GET', path: '/v1/models' },
  chat: {
    label: 'POST /v1/chat/completions',
    method: 'POST',
    path: '/v1/chat/completions',
  },
  messages: { label: 'POST /v1/messages', method: 'POST', path: '/v1/messages' },
}

function asObject(value: unknown): JsonObject | null {
  return typeof value === 'object' && value !== null ? (value as JsonObject) : null
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function formatResponseBody(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2)
  } catch {
    return text
  }
}

function apiErrorMessage(text: string): string {
  try {
    const root = asObject(JSON.parse(text))
    const detail = asString(root?.detail)
    if (detail) return detail
    const error = asObject(root?.error)
    const nested = asString(error?.message)
    if (nested) return nested
    const message = asString(root?.message)
    if (message) return message
  } catch {
    // Non-JSON error body.
  }
  return text.trim() || '请求失败'
}

function extractStreamPayload(payload: unknown): StreamExtraction {
  const root = asObject(payload)
  if (!root) return { content: '', reasoning: '', finishReason: '' }

  const choice = asObject(asArray(root.choices)[0])
  const delta = asObject(choice?.delta)
  if (delta) {
    return {
      content: asString(delta.content),
      reasoning: asString(delta.reasoning_content),
      finishReason: asString(choice?.finish_reason),
    }
  }

  const type = asString(root.type)
  const eventDelta = asObject(root.delta)
  if (type === 'content_block_delta') {
    const deltaType = asString(eventDelta?.type)
    return {
      content: deltaType === 'text_delta' ? asString(eventDelta?.text) : '',
      reasoning:
        deltaType === 'thinking_delta' ? asString(eventDelta?.thinking) : '',
      finishReason: '',
    }
  }

  if (type === 'content_block_start') {
    const block = asObject(root.content_block)
    return {
      content: asString(block?.type) === 'text' ? asString(block?.text) : '',
      reasoning:
        asString(block?.type) === 'thinking' ? asString(block?.thinking) : '',
      finishReason: '',
    }
  }

  if (type === 'message_delta') {
    return {
      content: '',
      reasoning: '',
      finishReason: asString(eventDelta?.stop_reason),
    }
  }

  return { content: '', reasoning: '', finishReason: '' }
}

function extractNonstream(
  endpoint: DebugEndpoint,
  payload: unknown,
): StreamExtraction {
  const root = asObject(payload)
  if (!root) return { content: '', reasoning: '', finishReason: '' }

  if (endpoint === 'models') {
    const ids = asArray(root.data)
      .map((item) => asString(asObject(item)?.id))
      .filter(Boolean)
    return { content: ids.join('\n'), reasoning: '', finishReason: '' }
  }

  if (endpoint === 'chat') {
    const choice = asObject(asArray(root.choices)[0])
    const message = asObject(choice?.message)
    return {
      content: asString(message?.content),
      reasoning: asString(message?.reasoning_content),
      finishReason: asString(choice?.finish_reason),
    }
  }

  let content = ''
  let reasoning = ''
  for (const rawBlock of asArray(root.content)) {
    const block = asObject(rawBlock)
    const type = asString(block?.type)
    if (type === 'text') content += asString(block?.text)
    if (type === 'thinking') reasoning += asString(block?.thinking)
    if (type === 'tool_use') {
      content += `[tool_use] ${asString(block?.name)} ${JSON.stringify(block?.input ?? {})}`
    }
  }
  return {
    content,
    reasoning,
    finishReason: asString(root.stop_reason),
  }
}

export function ApiDebugger() {
  const adminToken = useAuthStore((state) => state.token)
  const { toast } = useToast()
  const controllerRef = useRef<AbortController | null>(null)

  const [endpoint, setEndpoint] = useState<DebugEndpoint>('chat')
  const [apiKey, setApiKey] = useState('')
  const [showApiKey, setShowApiKey] = useState(false)
  const [model, setModel] = useState('deepseek-chat')
  const [systemPrompt, setSystemPrompt] = useState('')
  const [userPrompt, setUserPrompt] = useState('请用一句话介绍你自己。')
  const [stream, setStream] = useState(true)
  const [thinking, setThinking] = useState(false)
  const [search, setSearch] = useState(false)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<DebugResult>(EMPTY_RESULT)

  useEffect(() => () => controllerRef.current?.abort(), [])

  const requestBody = useMemo(() => {
    if (endpoint === 'models') return null

    const messages = [{ role: 'user', content: userPrompt.trim() }]
    if (endpoint === 'chat') {
      return {
        model,
        system: undefined,
        messages: [
          ...(systemPrompt.trim()
            ? [{ role: 'system', content: systemPrompt.trim() }]
            : []),
          ...messages,
        ],
        stream,
        thinking_mode: thinking,
        search_enabled: search,
      }
    }

    return {
      model,
      max_tokens: 1024,
      ...(systemPrompt.trim() ? { system: systemPrompt.trim() } : {}),
      messages,
      stream,
      ...(thinking ? { thinking: { type: 'enabled' } } : {}),
    }
  }, [endpoint, model, search, stream, systemPrompt, thinking, userPrompt])

  const requestPreview = requestBody
    ? JSON.stringify(requestBody, null, 2)
    : null
  const effectiveKey = apiKey.trim() || adminToken || ''

  const handleRun = async () => {
    if (!effectiveKey) {
      toast({
        title: '缺少认证信息',
        description: '请填写客户端 API Key，或重新登录管理面板。',
        variant: 'destructive',
      })
      return
    }
    if (endpoint !== 'models' && !userPrompt.trim()) {
      toast({ title: '请输入调试消息', variant: 'destructive' })
      return
    }

    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const started = performance.now()
    let statusCode: number | null = null
    let content = ''
    let reasoning = ''
    let finishReason = ''
    let raw = ''

    setBusy(true)
    setResult(EMPTY_RESULT)

    try {
      const spec = ENDPOINTS[endpoint]
      const response = await fetch(spec.path, {
        method: spec.method,
        headers: {
          Authorization: `Bearer ${effectiveKey}`,
          ...(requestBody ? { 'Content-Type': 'application/json' } : {}),
        },
        body: requestBody ? JSON.stringify(requestBody) : undefined,
        signal: controller.signal,
      })
      statusCode = response.status

      if (!response.ok) {
        const text = await response.text()
        const elapsedMs = Math.round(performance.now() - started)
        setResult({
          status: statusCode,
          elapsedMs,
          content: '',
          reasoning: '',
          finishReason: '',
          raw: formatResponseBody(text),
          error: `HTTP ${statusCode}: ${apiErrorMessage(text)}`,
        })
        return
      }

      if (spec.method === 'GET') {
        const text = await response.text()
        let payload: unknown = text
        try {
          payload = JSON.parse(text)
        } catch {
          // Keep the raw text for non-JSON responses.
        }
        const extracted = extractNonstream(endpoint, payload)
        setResult({
          status: statusCode,
          elapsedMs: Math.round(performance.now() - started),
          content: extracted.content,
          reasoning: extracted.reasoning,
          finishReason: extracted.finishReason,
          raw: formatResponseBody(text),
          error: '',
        })
        return
      }

      if (stream) {
        await consumeSse(response, ({ data, raw: rawEvent }) => {
          raw = `${raw}${raw ? '\n\n' : ''}${rawEvent}`.slice(-200_000)

          if (data !== '[DONE]') {
            try {
              const extracted = extractStreamPayload(JSON.parse(data))
              content += extracted.content
              reasoning += extracted.reasoning
              finishReason = extracted.finishReason || finishReason
            } catch {
              // Keep malformed events visible in the raw stream view.
            }
          }

          setResult({
            status: statusCode,
            elapsedMs: Math.round(performance.now() - started),
            content,
            reasoning,
            finishReason,
            raw,
            error: '',
          })
        })
      } else {
        const text = await response.text()
        let payload: unknown = text
        try {
          payload = JSON.parse(text)
        } catch {
          // Keep raw text for non-JSON responses.
        }
        const extracted = extractNonstream(endpoint, payload)
        setResult({
          status: statusCode,
          elapsedMs: Math.round(performance.now() - started),
          content: extracted.content,
          reasoning: extracted.reasoning,
          finishReason: extracted.finishReason,
          raw: formatResponseBody(text),
          error: '',
        })
      }
    } catch (error) {
      if (controller.signal.aborted) {
        setResult((current) => ({ ...current, error: '请求已停止' }))
      } else {
        const message = error instanceof Error ? error.message : String(error)
        setResult((current) => ({
          ...current,
          status: statusCode,
          elapsedMs: Math.round(performance.now() - started),
          error: message,
        }))
      }
    } finally {
      setBusy(false)
      controllerRef.current = null
    }
  }

  const handleStop = () => {
    controllerRef.current?.abort()
  }

  const clearResult = () => {
    controllerRef.current?.abort()
    setResult(EMPTY_RESULT)
  }

  const statusBadge = result.status ? (
    <Badge variant={result.error ? 'destructive' : 'success'}>
      HTTP {result.status}
    </Badge>
  ) : null

  return (
    <Card className="overflow-hidden">
      <CardHeader className="border-b bg-muted/20 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2 text-base">
              <Terminal className="h-4 w-4" />
              在线调试
            </CardTitle>
            <CardDescription>
              直接请求当前服务的真实接口。API Key 留空时使用当前管理会话。
            </CardDescription>
          </div>
          <Badge variant={busy ? 'busy' : 'idle'}>
            {busy ? '请求中' : '就绪'}
          </Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-4 p-4">
        <div className="space-y-1.5">
          <Label>接口</Label>
          <Select
            value={endpoint}
            onValueChange={(value) => {
              setEndpoint(value as DebugEndpoint)
              setResult(EMPTY_RESULT)
            }}
            disabled={busy}
          >
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {Object.entries(ENDPOINTS).map(([value, spec]) => (
                <SelectItem key={value} value={value}>
                  {spec.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <div className="space-y-1.5">
          <div className="flex items-center justify-between gap-2">
            <Label htmlFor="debug-api-key">客户端 API Key</Label>
            {adminToken && <Badge variant="env">当前管理会话可用</Badge>}
          </div>
          <div className="relative">
            <Input
              id="debug-api-key"
              type={showApiKey ? 'text' : 'password'}
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder="留空则使用当前管理会话"
              className="pr-10 font-mono text-xs"
              autoComplete="off"
              disabled={busy}
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="absolute right-0 top-0 h-9 w-9"
              onClick={() => setShowApiKey((current) => !current)}
              aria-label={showApiKey ? '隐藏 API Key' : '显示 API Key'}
              disabled={busy}
            >
              {showApiKey ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </Button>
          </div>
          <p className="text-[11px] text-muted-foreground">
            输入值只保存在当前页面内存；WebUI 调试默认使用管理会话，外部客户端仍需客户端 API Key。
          </p>
        </div>

        <div className="space-y-1.5">
          <Label htmlFor="debug-model">模型</Label>
          <Input
            id="debug-model"
            value={model}
            onChange={(event) => setModel(event.target.value)}
            placeholder="deepseek-chat"
            disabled={busy}
          />
        </div>

        {endpoint !== 'models' && (
          <>
            <div className="space-y-1.5">
              <Label htmlFor="debug-system">System 提示（可选）</Label>
              <Textarea
                id="debug-system"
                value={systemPrompt}
                onChange={(event) => setSystemPrompt(event.target.value)}
                placeholder="例如：You are a helpful assistant."
                rows={2}
                disabled={busy}
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="debug-prompt">User 消息</Label>
              <Textarea
                id="debug-prompt"
                value={userPrompt}
                onChange={(event) => setUserPrompt(event.target.value)}
                rows={4}
                disabled={busy}
              />
            </div>

            <div className="grid gap-3 rounded-md border p-3 sm:grid-cols-3">
              <label className="flex items-center justify-between gap-3 text-sm">
                <span>流式输出</span>
                <Switch checked={stream} onCheckedChange={setStream} disabled={busy} />
              </label>
              <label className="flex items-center justify-between gap-3 text-sm">
                <span>思考模式</span>
                <Switch checked={thinking} onCheckedChange={setThinking} disabled={busy} />
              </label>
              {endpoint === 'chat' ? (
                <label className="flex items-center justify-between gap-3 text-sm">
                  <span>联网搜索</span>
                  <Switch checked={search} onCheckedChange={setSearch} disabled={busy} />
                </label>
              ) : (
                <span className="text-xs leading-5 text-muted-foreground">
                  Anthropic 端点不接收 search 开关
                </span>
              )}
            </div>
          </>
        )}

        {endpoint === 'models' && (
          <div className="rounded-md border border-dashed p-3 text-sm text-muted-foreground">
            GET 请求无需请求体，将返回当前服务公开的模型列表。
          </div>
        )}

        {requestPreview && (
          <details className="rounded-md border">
            <summary className="cursor-pointer px-3 py-2 text-xs font-medium">
              查看请求体
            </summary>
            <CodeBlock
              code={requestPreview}
              title="application/json"
              language="json"
              className="rounded-none border-x-0 border-b-0"
            />
          </details>
        )}

        <div className="flex gap-2">
          {busy ? (
            <Button variant="destructive" onClick={handleStop} className="flex-1">
              <Square className="h-4 w-4" />
              停止请求
            </Button>
          ) : (
            <Button onClick={() => void handleRun()} className="flex-1">
              <Play className="h-4 w-4" />
              发送请求
            </Button>
          )}
          <Button
            variant="outline"
            size="icon"
            onClick={clearResult}
            disabled={busy || !result.raw}
            aria-label="清空调试结果"
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>

        {result.status !== null && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {statusBadge}
            {result.elapsedMs !== null && (
              <span>耗时 {formatLatency(result.elapsedMs)}</span>
            )}
            {result.finishReason && <span>结束原因 {result.finishReason}</span>}
          </div>
        )}

        {result.error && (
          <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
            <CircleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            <span className="break-all">{result.error}</span>
          </div>
        )}

        {(busy ||
          result.status !== null ||
          result.raw ||
          result.content ||
          result.reasoning) && (
          <Tabs defaultValue="output">
            <TabsList className="grid h-auto w-full grid-cols-3">
              <TabsTrigger value="output">输出</TabsTrigger>
              <TabsTrigger value="reasoning">推理</TabsTrigger>
              <TabsTrigger value="raw">原始响应</TabsTrigger>
            </TabsList>
            <TabsContent value="output">
              <pre className="max-h-80 min-h-28 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/30 p-3 font-mono text-xs leading-relaxed scrollbar-thin">
                {result.content || (busy ? '等待响应…' : '暂无文本输出')}
              </pre>
            </TabsContent>
            <TabsContent value="reasoning">
              <pre className="max-h-80 min-h-28 overflow-auto whitespace-pre-wrap break-words rounded-md border bg-muted/30 p-3 font-mono text-xs leading-relaxed scrollbar-thin">
                {result.reasoning || '当前响应没有推理内容'}
              </pre>
            </TabsContent>
            <TabsContent value="raw">
              <CodeBlock
                code={result.raw || '等待响应…'}
                title={stream && endpoint !== 'models' ? 'SSE 原始事件' : 'HTTP 响应'}
                className="max-h-80"
              />
            </TabsContent>
          </Tabs>
        )}
      </CardContent>
    </Card>
  )
}
