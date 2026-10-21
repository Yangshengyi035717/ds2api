import { useState } from 'react'
import { Loader2, Plus, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { post, put, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import type { ProxyConfig } from '@/lib/types'

interface ProxyFormProps {
  proxy?: ProxyConfig | null
  onSaved: (proxy: ProxyConfig) => void
  onCancel?: () => void
}

export function ProxyForm({ proxy, onSaved, onCancel }: ProxyFormProps) {
  const isEdit = Boolean(proxy)
  const [label, setLabel] = useState(proxy?.label ?? '')
  const [url, setUrl] = useState('')
  const [enabled, setEnabled] = useState(proxy?.enabled ?? true)
  const [busy, setBusy] = useState(false)

  const save = async () => {
    if (busy) return
    if (!isEdit && !url.trim()) {
      toast({ title: '代理地址不能为空', variant: 'destructive' })
      return
    }

    setBusy(true)
    try {
      const result = isEdit && proxy
        ? await put<{ ok: boolean; proxy: ProxyConfig }>(
            `/admin/api/proxies/${encodeURIComponent(proxy.id)}`,
            {
              label,
              ...(url.trim() ? { url: url.trim() } : {}),
              enabled,
            },
          )
        : await post<{ ok: boolean; proxy: ProxyConfig }>('/admin/api/proxies', {
            label,
            url: url.trim(),
            enabled,
          })
      toast({ title: isEdit ? '代理已更新' : '代理已添加', variant: 'success' })
      onSaved(result.proxy)
      if (!isEdit) {
        setLabel('')
        setUrl('')
        setEnabled(true)
      }
    } catch (error) {
      const message = error instanceof ApiCallError ? error.message : String(error)
      toast({ title: '保存代理失败', description: message, variant: 'destructive' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="space-y-1.5">
        <Label htmlFor="proxy-label">名称 / 备注</Label>
        <Input
          id="proxy-label"
          value={label}
          onChange={(event) => setLabel(event.target.value)}
          placeholder="例如：账号 A - 上海住宅"
          maxLength={80}
        />
      </div>

      <div className="space-y-1.5">
        <Label htmlFor="proxy-url">代理地址</Label>
        <Input
          id="proxy-url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder={isEdit ? '留空则不修改当前地址' : 'host:port:username:password'}
          autoComplete="off"
          className="font-mono text-xs"
        />
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          支持 HTTP、HTTPS、SOCKS4、SOCKS5、SOCKS5H。地址可能包含代理账号密码，
          界面只显示脱敏预览；启用凭据加密时会同时加密代理地址。可填写标准 URL、
          <code className="mx-1 font-mono">host:port</code>
          或
          <code className="mx-1 font-mono">host:port:username:password</code>。
        </p>
      </div>

      <div className="flex items-center justify-between rounded-md border p-3">
        <div>
          <Label htmlFor="proxy-enabled">启用代理</Label>
          <p className="text-[11px] text-muted-foreground">停用后不能绑定新账号，已绑定账号会停止使用该出口。</p>
        </div>
        <Switch id="proxy-enabled" checked={enabled} onCheckedChange={setEnabled} />
      </div>

      <div className="flex items-center gap-2">
        <Button onClick={() => void save()} disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : isEdit ? <Save className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
          {isEdit ? '保存' : '添加代理'}
        </Button>
        {isEdit && onCancel && (
          <Button variant="outline" onClick={onCancel} disabled={busy}>取消</Button>
        )}
      </div>
    </div>
  )
}
