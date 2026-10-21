import { useEffect, useState } from 'react'
import { Loader2, Plus, Save } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { post, put, get, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import type { Account, ProxiesResponse, ProxyConfig } from '@/lib/types'

interface Props {
  /** When set, the form is in "edit" mode. */
  account?: Account | null
  onSaved: (saved: Account) => void
  onCancel?: () => void
}

function zaiUserIdFromToken(token: string): string {
  try {
    const payload = token.trim().split('.')[1]
    if (!payload) return ''
    const normalized = payload.replace(/-/g, '+').replace(/_/g, '/')
    const parsed = JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')))
    return typeof parsed.id === 'string' ? parsed.id.trim() : ''
  } catch {
    return ''
  }
}

export function AccountForm({ account, onSaved, onCancel }: Props) {
  const isEdit = !!account
  const [email, setEmail] = useState(account?.email ?? '')
  const [provider, setProvider] = useState(account?.provider ?? 'deepseek')
  const [userId, setUserId] = useState('')
  const [token, setToken] = useState('')
  const [cookies, setCookies] = useState('')
  const [deviceId, setDeviceId] = useState('')
  const [proxyId, setProxyId] = useState(account?.proxy_id ?? '')
  const [proxies, setProxies] = useState<ProxyConfig[]>([])
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let cancelled = false
    get<ProxiesResponse>('/admin/api/proxies')
      .then((result) => {
        if (!cancelled) setProxies(result.proxies)
      })
      .catch((e) => {
        if (cancelled) return
        const msg = e instanceof ApiCallError ? e.message : String(e)
        toast({ title: '加载代理池失败', description: msg, variant: 'destructive' })
      })
    return () => {
      cancelled = true
    }
  }, [])

  const handleSave = async () => {
    if (busy) return
    const effectiveUserId = userId.trim() || zaiUserIdFromToken(token)
    if (provider === 'zai' && !effectiveUserId && !(isEdit && account?.has_user_id)) {
      toast({ title: 'z.ai 账号需要 User ID', variant: 'destructive' })
      return
    }
    setBusy(true)
    try {
      if (isEdit && account) {
        const body: Record<string, string> = {
          email,
          proxy_id: proxyId,
          provider,
        }
        if (token) body.token = token
        if (cookies) body.cookies = cookies
        if (deviceId) body.device_id = deviceId
        if (effectiveUserId) body.user_id = effectiveUserId
        await put(`/admin/api/accounts/${encodeURIComponent(account.id)}`, body)
        toast({ title: '账号已保存', variant: 'success' })
        // Refetch the account to get the fresh data.
        const saved: Account = { ...account, email: email || account.email }
        onSaved(saved)
      } else {
        if (!token) {
          toast({ title: 'Token 不能为空', variant: 'destructive' })
          setBusy(false)
          return
        }
        const res = await post<{ ok: boolean; account: Account }>('/admin/api/accounts', {
          token,
          cookies,
          email: email || '',
          proxy_id: proxyId,
          device_id: deviceId,
          provider,
          user_id: effectiveUserId,
        })
        toast({ title: '账号添加成功', variant: 'success' })
        onSaved(res.account)
        setEmail('')
        setProvider('deepseek')
        setUserId('')
        setToken('')
        setCookies('')
        setDeviceId('')
        setProxyId('')
      }
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '保存失败', description: msg, variant: 'destructive' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <Label>账号类型</Label>
        <Select value={provider} onValueChange={setProvider}>
          <SelectTrigger>
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="deepseek">DeepSeek</SelectItem>
            <SelectItem value="zai">z.ai</SelectItem>
          </SelectContent>
        </Select>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="acct-email">标识（邮箱 / 备注）</Label>
        <Input
          id="acct-email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="例如 user@example.com"
          autoComplete="off"
        />
      </div>
      {provider === 'zai' && (
        <div className="space-y-1.5">
          <Label htmlFor="acct-user-id">Z.ai User ID</Label>
          <Input
            id="acct-user-id"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder={
              isEdit && account?.user_id_preview
                ? `留空保持不变（当前 ${account.user_id_preview}）`
                : '可留空，自动从 token JWT 中提取'
            }
            autoComplete="off"
            className="font-mono text-xs"
          />
          <p className="text-[11px] text-muted-foreground">
            Z.ai 的 X-Signature 签名需要该 User ID；留空时会尝试解析 token JWT。
          </p>
        </div>
      )}
      <div className="space-y-1.5">
        <Label htmlFor="acct-token">Token</Label>
        <Input
          id="acct-token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          placeholder={isEdit ? '留空则不修改 Token' : 'Authorization Bearer token（不要带 Bearer）'}
          autoComplete="off"
          className="font-mono text-xs"
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="acct-cookies">Cookies</Label>
        <Input
          id="acct-cookies"
          value={cookies}
          onChange={(e) => setCookies(e.target.value)}
          placeholder={isEdit ? '留空则不修改 Cookies' : 'cf_clearance=...; session=...'}
          autoComplete="off"
          className="font-mono text-xs"
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="acct-device-id">Device ID（可选）</Label>
        <Input
          id="acct-device-id"
          value={deviceId}
          onChange={(e) => setDeviceId(e.target.value)}
          placeholder={
            isEdit
              ? `留空则不修改${account?.device_id_preview ? `（当前 ${account.device_id_preview}）` : ''}`
              : '登录浏览器的 x-device-id'
          }
          autoComplete="off"
          className="font-mono text-xs"
        />
        <p className="text-[11px] text-muted-foreground">
          登录 token 与浏览器设备绑定。通过 HAR / 浏览器导出导入时会自动带上。
        </p>
      </div>
      <div className="space-y-1.5">
        <Label>出口代理</Label>
        <Select value={proxyId || '__none__'} onValueChange={(value) => setProxyId(value === '__none__' ? '' : value)}>
          <SelectTrigger>
            <SelectValue placeholder="选择代理…" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="__none__">不使用代理</SelectItem>
            {proxies.map((proxy) => {
              const boundByOther = proxy.bound_count > 0 && proxy.id !== account?.proxy_id
              return (
                <SelectItem key={proxy.id} value={proxy.id} disabled={!proxy.enabled || boundByOther}>
                  {proxy.label} · {proxy.url_preview}
                  {!proxy.enabled ? '（停用）' : boundByOther ? '（已绑定）' : ''}
                </SelectItem>
              )
            })}
          </SelectContent>
        </Select>
        <p className="text-[11px] text-muted-foreground">
          每个代理默认只能绑定一个账号，避免多个账号共用同一出口 IP。代理在「代理池」菜单中维护。
        </p>
      </div>
      <div className="flex items-center gap-2 pt-2">
        <Button onClick={handleSave} disabled={busy}>
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : isEdit ? <Save className="h-4 w-4" /> : <Plus className="h-4 w-4" />}
          {isEdit ? '保存' : '添加'}
        </Button>
        {isEdit && onCancel && (
          <Button variant="outline" onClick={onCancel} disabled={busy}>
            取消
          </Button>
        )}
      </div>
      {!isEdit && (
        <p className="text-xs text-muted-foreground">
          新增账号会持久化到 data/accounts.json。
        </p>
      )}
    </div>
  )
}
