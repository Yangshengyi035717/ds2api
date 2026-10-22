import { useState, useRef, useEffect } from 'react'
import { Send, Square, Trash2, FlaskConical, Info } from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { useToast } from '@/hooks/use-toast'
import { useAuthStore } from '@/stores/auth'
import { ApiCallError } from '@/lib/api'
import { consumeSse } from '@/lib/sse'
import { ModelSelector } from '@/components/playground/ModelSelector'
import { MessageList, type ChatMessage, type StreamingState } from '@/components/playground/MessageList'

/** Messages sent to the backend (OpenAI-compatible subset we support). */
interface ApiMessage {
  role: 'system' | 'user' | 'assistant'
  content: string
}

/** Display labels for provider-hosted models returned by /v1/models. */
const MODEL_LABELS: Record<string, string> = {
  'deepseek-chat': 'DeepSeek Chat · deepseek',
  'glm-5.3-flash': 'GLM-5.3 Flash · z.ai',
  'kimi-k2.6': 'Kimi K2.6 · kimi',
  'kimi-k3': 'Kimi K3 · kimi',
  'kimi-k2.8': 'Kimi K2.8 · kimi',
  'qwen3.7-max': 'Qwen3.7 Max · qianwen',
  'hunyuan-hy4-preview': 'Hunyuan HY4 Preview · hunyuan',
  'wenxin-smart-mode': 'Wenxin Smart Mode · wenxin',
  'doubao-seed': 'Doubao Seed · doubao',
  'deepseek-reasoner': 'DeepSeek Reasoner（旧别名）',
}

export default function PlaygroundPage() {
  const token = useAuthStore((s) => s.token)
  const { toast } = useToast()

  // Request parameters are forwarded by provider:
  //   model          -> DeepSeek model or z.ai public alias
  //   thinking_mode  -> provider thinking/reasoning switch
  //   search_enabled -> provider web-search switch
  // temperature / top_p / max_tokens are ignored by the backend, so they
  // are intentionally not offered here.
  const [model, setModel] = useState('')
  const [systemPrompt, setSystemPrompt] = useState('')
  const [thinking, setThinking] = useState(false)
  const [search, setSearch] = useState(false)

  const [input, setInput] = useState('')
  const [history, setHistory] = useState<ChatMessage[]>([])
  const [streaming, setStreaming] = useState<StreamingState | null>(null)
  const [busy, setBusy] = useState(false)
  const [clearOpen, setClearOpen] = useState(false)
  const acRef = useRef<AbortController | null>(null)
  // Tracks the latest accumulated stream so stop/abort handlers read the
  // current value instead of a stale render closure.
  const accRef = useRef<StreamingState>({ reasoning: '', content: '' })

  useEffect(() => () => acRef.current?.abort(), [])

  const handleSend = async () => {
    if (!input.trim() || !model || busy) return
    const userMsg: ChatMessage = { role: 'user', content: input }
    const nextHistory = [...history, userMsg]
    setHistory(nextHistory)
    setInput('')
    setBusy(true)
    setStreaming({ reasoning: '', content: '' })

    const apiMessages: ApiMessage[] = []
    if (systemPrompt.trim()) {
      apiMessages.push({ role: 'system', content: systemPrompt })
    }
    for (const m of nextHistory) apiMessages.push({ role: m.role, content: m.content })

    const ac = new AbortController()
    acRef.current = ac
    const acc: StreamingState = { reasoning: '', content: '' }
    accRef.current = acc

    try {
      const res = await fetch('/v1/chat/completions', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${token}`,
        },
        body: JSON.stringify({
          model,
          messages: apiMessages,
          stream: true,
          thinking_mode: thinking,
          search_enabled: search,
        }),
        signal: ac.signal,
      })

      if (!res.ok || !res.body) {
        const body = await res.text()
        throw new ApiCallError(res.status, `HTTP ${res.status}: ${body.slice(0, 300)}`)
      }

      // Consume one "data: <json>" payload and accumulate its text.
      const handleEvent = (payload: string) => {
        if (payload === '[DONE]') return
        try {
          const obj = JSON.parse(payload)
          const choice = obj.choices?.[0]
          if (!choice) return
          const delta = choice.delta ?? {}
          if (typeof delta.reasoning_content === 'string') {
            acc.reasoning += delta.reasoning_content
          } else if (typeof delta.content === 'string') {
            acc.content += delta.content
          }
          accRef.current = { reasoning: acc.reasoning, content: acc.content }
          setStreaming({ reasoning: acc.reasoning, content: acc.content })
        } catch {
          // ignore malformed chunk
        }
      }

      await consumeSse(res, ({ data }) => handleEvent(data))

      setHistory([
        ...nextHistory,
        { role: 'assistant', content: acc.content, reasoning: acc.reasoning },
      ])
      setStreaming(null)
    } catch (e) {
      if (ac.signal.aborted) {
        const cur = accRef.current
        if (cur.content.trim() || cur.reasoning.trim()) {
          setHistory([
            ...nextHistory,
            { role: 'assistant', content: cur.content, reasoning: cur.reasoning },
          ])
        } else {
          setHistory(nextHistory)
        }
      } else {
        const msg = e instanceof ApiCallError ? e.message : String(e)
        toast({ title: '请求失败', description: msg, variant: 'destructive' })
      }
      setStreaming(null)
    } finally {
      setBusy(false)
      acRef.current = null
    }
  }

  const handleStop = () => {
    acRef.current?.abort()
  }

  const handleClear = () => {
    setHistory([])
    setStreaming(null)
    setClearOpen(false)
  }

  return (
    <div className="space-y-4 animate-fade-in h-full flex flex-col">
      <PageHeader
        title="Playground"
        description="在线测试 /v1/chat/completions"
        actions={
          <Button
            variant="outline"
            onClick={() => setClearOpen(true)}
            disabled={history.length === 0 || busy}
          >
            <Trash2 className="h-4 w-4" />
            清空
          </Button>
        }
      />

      <div className="grid min-h-0 flex-1 gap-4 sm:grid-cols-[220px,minmax(0,1fr)] xl:grid-cols-[280px,minmax(0,1fr)]">
        {/* Left column: request parameters */}
        <Card className="self-start overflow-y-auto scrollbar-thin">
          <CardHeader className="pb-2">
            <CardTitle className="text-base flex items-center gap-2">
              <FlaskConical className="h-4 w-4" />
              参数
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label>模型</Label>
              <ModelSelector value={model} onChange={setModel} labels={MODEL_LABELS} />
              <p className="text-[11px] text-muted-foreground">
                模型列表来自 <code className="font-mono">/v1/models</code>。
                DeepSeek、z.ai 与 Kimi 会自动路由到对应账号池，是否思考由下面的开关
                （或服务端 <code className="font-mono">THINKING</code>）决定。
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="system">System 提示</Label>
              <textarea
                id="system"
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
                placeholder="可选，例如 'You are a helpful assistant.'"
                className="flex min-h-[80px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                rows={3}
              />
            </div>

            <div className="flex items-center justify-between">
              <div>
                <Label htmlFor="thinking">思考模式</Label>
                <p className="text-[11px] text-muted-foreground">
                  对应 DeepSeek <code className="font-mono">thinking_enabled</code>
                  / z.ai <code className="font-mono">enable_thinking</code>
                </p>
              </div>
              <Switch
                id="thinking"
                checked={thinking}
                onCheckedChange={setThinking}
              />
            </div>

            <div className="flex items-center justify-between">
              <div>
                <Label htmlFor="search">联网搜索</Label>
                <p className="text-[11px] text-muted-foreground">
                  对应 DeepSeek <code className="font-mono">search_enabled</code>
                  / z.ai web search
                </p>
              </div>
              <Switch id="search" checked={search} onCheckedChange={setSearch} />
            </div>

            <details className="rounded-md border border-dashed">
              <summary className="flex cursor-pointer list-none items-center gap-1.5 px-3 py-2 text-[11px] font-medium text-foreground/80">
                <Info className="h-3.5 w-3.5" />
                参数生效规则
              </summary>
              <div className="space-y-1 border-t px-3 py-2 text-[11px] leading-relaxed text-muted-foreground">
                <p>· 服务端 THINKING / SEARCH 优先级高于此处设置</p>
                <p>· 思考能力由开关和 THINKING 决定，不是换模型名</p>
                <p>· 旧的 MODE=expert / quick 仍兼容</p>
                <p>· temperature / top_p / max_tokens 后端暂不生效</p>
              </div>
            </details>
          </CardContent>
        </Card>

        {/* Right column: conversation */}
        <Card className="flex min-h-[460px] flex-col">
          <CardContent className="flex-1 min-h-0 overflow-y-auto scrollbar-thin p-4">
            <MessageList messages={history} streaming={streaming} />
          </CardContent>
          <div className="border-t p-3 flex items-end gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  handleSend()
                }
              }}
              placeholder="输入消息，回车发送，Shift+回车换行…"
              disabled={busy || !model}
              className="flex-1"
            />
            {busy ? (
              <Button variant="destructive" onClick={handleStop} aria-label="停止">
                <Square className="h-4 w-4" />
              </Button>
            ) : (
              <Button onClick={handleSend} disabled={!input.trim() || !model}>
                <Send className="h-4 w-4" />
                发送
              </Button>
            )}
          </div>
          <div className="border-t px-3 py-1.5 text-[11px] text-muted-foreground">
            Enter 发送 · Shift+Enter 换行 · 模型与参数见左侧
          </div>
        </Card>
      </div>

      {/* Clear-confirm dialog */}
      <AlertDialog open={clearOpen} onOpenChange={setClearOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>清空所有对话？</AlertDialogTitle>
            <AlertDialogDescription>此操作将删除当前会话中的所有消息，不可恢复。</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={handleClear} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
              确认清空
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
