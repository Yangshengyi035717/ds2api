import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Save, Sparkles } from 'lucide-react'
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
import { useToast } from '@/hooks/use-toast'
import { get, put } from '@/lib/api'
import type { RuntimeConfigResponse } from '@/lib/types'

const SOURCE_LABELS: Record<string, string> = {
  webui: 'WebUI 配置',
  env: '.env 初始值',
  default: '程序默认值',
}

export function RuntimeConfigManager() {
  const [config, setConfig] = useState<RuntimeConfigResponse | null>(null)
  const [modelName, setModelName] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { toast } = useToast()

  const applyConfig = (next: RuntimeConfigResponse) => {
    setConfig(next)
    setModelName(next.model_name)
  }

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      applyConfig(await get<RuntimeConfigResponse>('/admin/api/runtime-config'))
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setError(message)
      toast({ title: '加载模型配置失败', description: message, variant: 'destructive' })
    } finally {
      setLoading(false)
    }
  }, [toast])

  useEffect(() => {
    void load()
  }, [load])

  const save = async () => {
    const value = modelName.trim()
    if (!value) {
      toast({ title: '模型名称不能为空', variant: 'destructive' })
      return
    }

    setSaving(true)
    setError(null)
    try {
      const next = await put<RuntimeConfigResponse>('/admin/api/runtime-config', {
        model_name: value,
      })
      applyConfig(next)
      toast({
        title: '模型名称已更新',
        description: `之后的 API 响应将使用 ${next.model_name}，无需重启服务。`,
      })
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setError(message)
      toast({ title: '保存模型名称失败', description: message, variant: 'destructive' })
    } finally {
      setSaving(false)
    }
  }

  const changed = Boolean(config && modelName.trim() !== config.model_name)

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2 text-base">
              <Sparkles className="h-4 w-4" />
              模型名称
            </CardTitle>
            <CardDescription>
              配置 OpenAI / Anthropic 客户端看到的公开模型名；不会切换 DeepSeek 上游模型。
            </CardDescription>
          </div>
          {config && (
            <Badge variant={config.source === 'webui' ? 'success' : 'secondary'}>
              {SOURCE_LABELS[config.source] ?? config.source}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading && !config ? (
          <p className="text-sm text-muted-foreground">正在加载模型配置…</p>
        ) : (
          <>
            <div className="space-y-1.5">
              <Label htmlFor="runtime-model-name">公开模型名称</Label>
              <div className="flex flex-col gap-2 sm:flex-row">
                <Input
                  id="runtime-model-name"
                  value={modelName}
                  onChange={(event) => setModelName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter' && changed && !saving) void save()
                  }}
                  placeholder="deepseek-chat"
                  maxLength={128}
                  disabled={saving}
                />
                <Button onClick={() => void save()} disabled={saving || !changed || !modelName.trim()}>
                  <Save className="h-4 w-4" />
                  {saving ? '保存中…' : '保存并生效'}
                </Button>
              </div>
              <p className="text-[11px] leading-relaxed text-muted-foreground">
                保存后会写入 <code className="font-mono">data/runtime_config.json</code>，
                并立即更新 <code className="font-mono">/v1/models</code> 和响应中的 model 字段。
              </p>
            </div>

            {config && (
              <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
                <div className="rounded-md border px-3 py-2">
                  当前名称：<code className="font-mono text-foreground">{config.model_name}</code>
                </div>
                <div className="rounded-md border px-3 py-2">
                  默认名称：<code className="font-mono text-foreground">{config.default_model_name}</code>
                </div>
              </div>
            )}

            {config?.model_routes_configured && (
              <div className="rounded-md border border-warning/30 bg-warning/5 p-3 text-xs leading-relaxed text-muted-foreground">
                当前配置了 <code className="font-mono">MODEL_ROUTES</code>，
                <code className="mx-1 font-mono">/v1/models</code>
                会列出路由键；这里的公开名称仍用于响应和默认调试。
              </div>
            )}

            {error && <p className="text-sm text-destructive">{error}</p>}

            <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading || saving}>
              <RefreshCw className="h-4 w-4" />
              重新读取
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  )
}
