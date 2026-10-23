import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Save, Sparkles } from 'lucide-react'
import { ModelLabel } from '@/components/models/ModelLabel'
import { ProviderIcon } from '@/components/models/ProviderIcon'
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
import { useToast } from '@/hooks/use-toast'
import { get, put } from '@/lib/api'
import { resolveModelProvider } from '@/lib/providers'
import type { RuntimeConfigResponse } from '@/lib/types'

const SOURCE_LABELS: Record<string, string> = {
  webui: 'WebUI 配置',
  env: '.env 初始值',
  default: '程序默认值',
}

export function RuntimeConfigManager() {
  const [config, setConfig] = useState<RuntimeConfigResponse | null>(null)
  const [modelName, setModelName] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [currency, setCurrency] = useState('USD')
  const [billingMode, setBillingMode] = useState('token')
  const [inputPrice, setInputPrice] = useState('')
  const [outputPrice, setOutputPrice] = useState('')
  const [cacheReadPrice, setCacheReadPrice] = useState('')
  const [cacheWritePrice, setCacheWritePrice] = useState('')
  const [perRequestPrice, setPerRequestPrice] = useState('')
  const [rateMultiplier, setRateMultiplier] = useState('1')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { toast } = useToast()

  const applyConfig = (next: RuntimeConfigResponse) => {
    setConfig(next)
    setModelName(next.model_name)
    setDisplayName(next.display_name)
    setCurrency(next.pricing.currency)
    setBillingMode(next.pricing.billing_mode)
    setInputPrice(next.pricing.input_price === null ? '' : String(next.pricing.input_price))
    setOutputPrice(next.pricing.output_price === null ? '' : String(next.pricing.output_price))
    setCacheReadPrice(next.pricing.cache_read_price === null ? '' : String(next.pricing.cache_read_price))
    setCacheWritePrice(next.pricing.cache_write_price === null ? '' : String(next.pricing.cache_write_price))
    setPerRequestPrice(next.pricing.per_request_price === null ? '' : String(next.pricing.per_request_price))
    setRateMultiplier(String(next.pricing.rate_multiplier))
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
    if (!displayName.trim()) {
      toast({ title: '显示名称不能为空', variant: 'destructive' })
      return
    }

    const optionalNumber = (raw: string): number | null => {
      if (!raw.trim()) return null
      const parsed = Number(raw)
      return Number.isFinite(parsed) ? parsed : null
    }
    const multiplier = Number(rateMultiplier)
    if (!Number.isFinite(multiplier) || multiplier < 0) {
      toast({ title: '倍率必须是大于等于 0 的数字', variant: 'destructive' })
      return
    }

    setSaving(true)
    setError(null)
    try {
      const next = await put<RuntimeConfigResponse>('/admin/api/runtime-config', {
        model_name: value,
        display_name: displayName.trim(),
        pricing: {
          currency: currency.trim().toUpperCase(),
          billing_mode: billingMode,
          input_price: optionalNumber(inputPrice),
          output_price: optionalNumber(outputPrice),
          cache_read_price: optionalNumber(cacheReadPrice),
          cache_write_price: optionalNumber(cacheWritePrice),
          per_request_price: optionalNumber(perRequestPrice),
          rate_multiplier: multiplier,
        },
      })
      applyConfig(next)
      toast({
        title: '模型与定价配置已更新',
        description: `API 模型名称为 ${next.model_name}，Sub2API 可随后同步。`,
      })
    } catch (e) {
      const message = e instanceof Error ? e.message : String(e)
      setError(message)
      toast({ title: '保存运行配置失败', description: message, variant: 'destructive' })
    } finally {
      setSaving(false)
    }
  }

  const changed = Boolean(
    config &&
    (
      modelName.trim() !== config.model_name ||
      displayName.trim() !== config.display_name ||
      currency.trim().toUpperCase() !== config.pricing.currency ||
      billingMode !== config.pricing.billing_mode ||
      inputPrice !== (config.pricing.input_price === null ? '' : String(config.pricing.input_price)) ||
      outputPrice !== (config.pricing.output_price === null ? '' : String(config.pricing.output_price)) ||
      cacheReadPrice !== (config.pricing.cache_read_price === null ? '' : String(config.pricing.cache_read_price)) ||
      cacheWritePrice !== (config.pricing.cache_write_price === null ? '' : String(config.pricing.cache_write_price)) ||
      perRequestPrice !== (config.pricing.per_request_price === null ? '' : String(config.pricing.per_request_price)) ||
      rateMultiplier !== String(config.pricing.rate_multiplier)
    )
  )

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2 text-base">
              <Sparkles className="h-4 w-4" />
              模型与 Sub2API 定价
            </CardTitle>
            <CardDescription>
              配置公开模型名、显示名和供 Sub2API 同步器读取的价格/倍率；最终计费仍由 Sub2API 管理。
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
              <div className="relative">
                <ProviderIcon
                  provider={resolveModelProvider({ modelId: modelName })}
                  size={16}
                  className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2"
                />
                <Input
                  id="runtime-model-name"
                  value={modelName}
                  onChange={(event) => setModelName(event.target.value)}
                  placeholder="deepseek-chat"
                  className="pl-9"
                  maxLength={128}
                  disabled={saving}
                />
              </div>
              <p className="text-[11px] leading-relaxed text-muted-foreground">
                保存后会写入 <code className="font-mono">data/runtime_config.json</code>，
                并立即更新 <code className="font-mono">/v1/models</code> 和响应中的 model 字段。
              </p>
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="runtime-display-name">模型显示名称</Label>
              <Input
                id="runtime-display-name"
                value={displayName}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder="DeepSeek Chat"
                maxLength={128}
                disabled={saving}
              />
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label>计费模式</Label>
                <Select value={billingMode} onValueChange={setBillingMode} disabled={saving}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="token">按 Token</SelectItem>
                    <SelectItem value="per_request">按请求</SelectItem>
                    <SelectItem value="image">按图片</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="runtime-currency">币种</Label>
                <Input id="runtime-currency" value={currency} onChange={(event) => setCurrency(event.target.value)} maxLength={3} disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="runtime-rate-multiplier">账号倍率</Label>
                <Input id="runtime-rate-multiplier" type="number" min="0" step="0.000001" value={rateMultiplier} onChange={(event) => setRateMultiplier(event.target.value)} disabled={saving} />
              </div>
            </div>

            <details className="group rounded-md border">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 text-sm font-medium">
                <span>高级定价</span>
                <span className="text-[11px] font-normal text-muted-foreground">
                  输入 / 输出 / 缓存 / 按请求
                </span>
              </summary>
              <div className="grid gap-3 border-t p-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="runtime-input-price">输入价格 / 百万 Token</Label>
                <Input id="runtime-input-price" type="number" min="0" step="0.000001" value={inputPrice} onChange={(event) => setInputPrice(event.target.value)} placeholder="留空" disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="runtime-output-price">输出价格 / 百万 Token</Label>
                <Input id="runtime-output-price" type="number" min="0" step="0.000001" value={outputPrice} onChange={(event) => setOutputPrice(event.target.value)} placeholder="留空" disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="runtime-cache-read-price">缓存读取价格</Label>
                <Input id="runtime-cache-read-price" type="number" min="0" step="0.000001" value={cacheReadPrice} onChange={(event) => setCacheReadPrice(event.target.value)} placeholder="留空" disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="runtime-cache-write-price">缓存写入价格</Label>
                <Input id="runtime-cache-write-price" type="number" min="0" step="0.000001" value={cacheWritePrice} onChange={(event) => setCacheWritePrice(event.target.value)} placeholder="留空" disabled={saving} />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="runtime-request-price">按请求价格</Label>
                <Input id="runtime-request-price" type="number" min="0" step="0.000001" value={perRequestPrice} onChange={(event) => setPerRequestPrice(event.target.value)} placeholder="留空" disabled={saving} />
              </div>
              </div>
            </details>

            <p className="rounded-md border border-dashed p-3 text-[11px] leading-relaxed text-muted-foreground">
              价格用于 <code className="font-mono">/v1/provider-manifest</code> 和 Sub2API 同步脚本。
              Sub2API 的渠道模型价格、账号倍率与分组高峰倍率仍是最终计费依据。
            </p>

            {config && (
              <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
                <div className="flex items-center gap-2 rounded-md border px-3 py-2">
                  当前名称：
                  <ModelLabel
                    modelId={config.model_name}
                    iconSize={14}
                    textClassName="font-mono text-foreground"
                  />
                </div>
                <div className="flex items-center gap-2 rounded-md border px-3 py-2">
                  默认名称：
                  <ModelLabel
                    modelId={config.default_model_name}
                    iconSize={14}
                    textClassName="font-mono text-foreground"
                  />
                </div>
              </div>
            )}

            <Button onClick={() => void save()} disabled={saving || !changed || !modelName.trim()}>
              <Save className="h-4 w-4" />
              {saving ? '保存中…' : '保存并生效'}
            </Button>

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
