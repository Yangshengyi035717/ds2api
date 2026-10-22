import { useMemo, useState } from 'react'
import { ClipboardCheck, ClipboardCopy, FileUp, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { post, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import type { Account, CredentialImportResponse } from '@/lib/types'

const DEEPSEEK_EXPORT_SCRIPT = `(() => {
  const read = (key) => localStorage.getItem(key)
  const parse = (raw) => {
    if (!raw) return null
    try { return JSON.parse(raw) } catch { return raw }
  }
  const tokenRaw = parse(read('userToken'))
  const token = typeof tokenRaw === 'string'
    ? tokenRaw
    : tokenRaw && (tokenRaw.value || tokenRaw.token) || ''
  const deviceId = read('deepseek-device-id:chat') || ''
  const cookies = Object.fromEntries(
    document.cookie.split(';').map((part) => {
      const i = part.indexOf('=')
      return i < 0 ? ['', ''] : [part.slice(0, i).trim(), part.slice(i + 1).trim()]
    }).filter(([k]) => k)
  )
  const out = { token, cookies, device_id: deviceId }
  const text = JSON.stringify(out, null, 2)
  navigator.clipboard?.writeText(text)
  console.log(text)
  return text
})()`

const ZAI_EXPORT_SCRIPT = `(async () => {
  const token = localStorage.getItem('token') || ''
  if (!token) throw new Error('未找到 z.ai token，请先登录 zchat.z.ai')
  const userIdFromToken = () => {
    try {
      const part = token.split('.')[1]
      if (!part) return ''
      const normalized = part.replace(/-/g, '+').replace(/_/g, '/')
      const parsed = JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')))
      return typeof parsed.id === 'string' ? parsed.id.trim() : ''
    } catch {
      return ''
    }
  }
  let user = {}
  try {
    const response = await fetch('/api/v1/auths/', {
      headers: { Authorization: 'Bearer ' + token, Accept: 'application/json' },
      credentials: 'include'
    })
    if (response.ok) {
      const payload = await response.json()
      user = payload?.data || payload || {}
    }
  } catch (error) {
    console.warn('读取 z.ai 用户信息失败，改用 token JWT', error)
  }
  const cookies = Object.fromEntries(
    document.cookie.split(';').map((part) => {
      const i = part.indexOf('=')
      return i < 0 ? ['', ''] : [part.slice(0, i).trim(), part.slice(i + 1).trim()]
    }).filter(([key]) => key)
  )
  const out = {
    provider: 'zai',
    token,
    user_id: user.id || userIdFromToken(),
    device_id: localStorage.getItem('_arms_uid') || '',
    cookies
  }
  if (!out.user_id) throw new Error('z.ai 用户信息中没有 id')
  const text = JSON.stringify(out, null, 2)
  navigator.clipboard?.writeText(text)
  console.log(text)
  return text
})()`

const KIMI_EXPORT_SCRIPT = `(() => {
  const accessToken = localStorage.getItem('access_token') || ''
  const refreshToken = localStorage.getItem('refresh_token') || ''
  if (!accessToken || !refreshToken) {
    throw new Error('未找到 Kimi access_token / refresh_token，请先登录')
  }
  const decode = (token) => {
    try {
      const part = token.split('.')[1]
      const normalized = part.replace(/-/g, '+').replace(/_/g, '/')
      return JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')))
    } catch {
      return {}
    }
  }
  const claims = decode(accessToken)
  const cookies = Object.fromEntries(
    document.cookie.split(';').map((part) => {
      const i = part.indexOf('=')
      return i < 0 ? ['', ''] : [part.slice(0, i).trim(), part.slice(i + 1).trim()]
    }).filter(([key]) => key)
  )
  const out = {
    provider: 'kimi',
    token: accessToken,
    refresh_token: refreshToken,
    user_id: localStorage.getItem('msh_user_id') || claims.sub || claims.abstract_user_id || '',
    device_id: claims.device_id || '',
    cookies
  }
  const text = JSON.stringify(out, null, 2)
  navigator.clipboard?.writeText(text)
  console.log(text)
  return text
})()`

interface Props {
  onImported: (account: Account) => void
  provider?: 'deepseek' | 'zai' | 'kimi' | 'hunyuan'
}

export function CredentialImportPanel({ onImported, provider = 'deepseek' }: Props) {
  const [payload, setPayload] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<CredentialImportResponse | null>(null)
  const charCount = payload.length
  const isZai = provider === 'zai'
  const isKimi = provider === 'kimi'
  const isHunyuan = provider === 'hunyuan'

  const placeholder = useMemo(
    () =>
      isZai
        ? '粘贴在 zchat.z.ai 控制台运行“复制导出脚本”后输出的 JSON。'
        : isKimi
          ? '粘贴在 www.kimi.com 控制台运行“复制导出脚本”后输出的 JSON。'
          : isHunyuan
            ? '粘贴 api.hunyuan.tencent.com 请求的完整 Cookie 请求头，或包含该请求的 HAR。'
            : '粘贴 DevTools 导出的 HAR，或在 chat.deepseek.com 控制台运行“复制导出脚本”后粘贴输出的 JSON。',
    [isHunyuan, isKimi, isZai],
  )

  const copyScript = async () => {
    if (isHunyuan) return
    try {
      await navigator.clipboard.writeText(
        isKimi
          ? KIMI_EXPORT_SCRIPT
          : isZai
            ? ZAI_EXPORT_SCRIPT
            : DEEPSEEK_EXPORT_SCRIPT,
      )
      toast({ title: '导出脚本已复制', variant: 'success' })
    } catch {
      toast({
        title: '复制失败',
        description: '请手动选中脚本内容复制',
        variant: 'destructive',
      })
    }
  }

  const handleImport = async () => {
    if (!payload.trim() || busy) return
    setBusy(true)
    setResult(null)
    try {
      const res = await post<CredentialImportResponse>('/admin/api/accounts/import', {
        payload,
        provider,
      })
      setResult(res)
      setPayload('')
      onImported(res.account)
      toast({
        title: res.action === 'updated' ? '账号凭据已更新' : '账号已导入',
        description: isHunyuan
          ? '已导入 Hunyuan Cookie'
          : res.import.has_device_id
            ? '已包含 Device ID'
            : '未包含 Device ID，建议重新导出',
        variant: 'success',
      })
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '导入失败', description: msg, variant: 'destructive' })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <div className="rounded-md border bg-muted/40 p-3 text-xs leading-relaxed text-muted-foreground">
        {isZai ? (
          <p>
            请在已登录的 <code className="font-mono">zchat.z.ai</code>{' '}
            页面运行导出脚本。脚本会读取 Token、用户 ID、Device ID 与 Cookie；
            退出登录或重新登录会轮换旧凭据。
          </p>
        ) : isKimi ? (
          <p>
            请在已登录的 <code className="font-mono">www.kimi.com</code>{' '}
            页面运行导出脚本。脚本读取 access_token、refresh_token、用户 ID、
            Device ID 与 Cookie；服务端会使用 refresh_token 自动续期。
          </p>
        ) : isHunyuan ? (
          <p>
            混元使用 HttpOnly Cookie，页面脚本无法读取完整登录态。请在已登录的{' '}
            <code className="font-mono">aistudio.tencent.com</code>{' '}
            DevTools → Network 中打开 <code className="font-mono">api.hunyuan.tencent.com</code>{' '}
            请求，复制完整 Cookie 请求头或导出 HAR。Cookie 必须包含{' '}
            <code className="font-mono">hunyuan_user</code> 与{' '}
            <code className="font-mono">hunyuan_token</code>。
          </p>
        ) : (
          <p>
            DeepSeek 登录使用设备绑定的 MFA 签名，服务器无法代替浏览器完成密码登录。
            推荐在已登录的 <code className="font-mono">chat.deepseek.com</code>{' '}
            页面导出凭据后粘贴到这里；退出登录会导致旧 token 立即失效。
          </p>
        )}
      </div>

      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Label htmlFor="credential-import">
            {isZai
              ? 'z.ai 浏览器导出 JSON'
              : isKimi
                ? 'Kimi 浏览器导出 JSON'
                : isHunyuan
                  ? '混元 Cookie 或 HAR'
                  : '凭据内容（HAR 或浏览器导出 JSON）'}
          </Label>
          {!isHunyuan && (
            <Button type="button" size="sm" variant="outline" onClick={copyScript}>
              <ClipboardCopy className="h-3.5 w-3.5" />
              复制{isZai ? ' z.ai ' : isKimi ? ' Kimi ' : '浏览器'}导出脚本
            </Button>
          )}
        </div>
        <Textarea
          id="credential-import"
          value={payload}
          onChange={(e) => setPayload(e.target.value)}
          placeholder={placeholder}
          className="min-h-[180px] font-mono text-xs"
          spellCheck={false}
        />
        <p className="text-[11px] text-muted-foreground">
          {charCount > 0
            ? `${charCount.toLocaleString()} 字符`
            : isZai
              ? '服务端只提取 token / user_id / device_id / cookie'
              : isKimi
                ? '服务端只提取 access_token / refresh_token / device_id / cookie'
                : isHunyuan
                  ? '支持 Cookie 字符串或整份 HAR，服务端只提取 hunyuan_user / hunyuan_token / Cookie'
                  : '支持整份 HAR，服务端只提取 token / cookie / device_id'}
        </p>
      </div>

      <div className="rounded-md border p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">浏览器导出步骤</p>
        <ol className="mt-1 list-decimal space-y-1 pl-4">
          <li>
            在浏览器打开并登录{' '}
            <code className="font-mono">
              {isZai
                ? 'zchat.z.ai'
                : isKimi
                  ? 'www.kimi.com'
                  : isHunyuan
                    ? 'aistudio.tencent.com'
                    : 'chat.deepseek.com'}
            </code>。
          </li>
          <li>
            {isHunyuan
              ? '打开 DevTools → Network，选择一个 api.hunyuan.tencent.com 请求。'
              : '打开 DevTools → Console，运行上方脚本。'}
          </li>
          <li>
            {isHunyuan
              ? '复制完整 Cookie 请求头，或导出 HAR 并粘贴到上面。'
              : '脚本会把 JSON 复制到剪贴板，粘贴到上面即可。'}
          </li>
        </ol>
      </div>

      {result && (
        <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3 text-xs">
          <p className="flex items-center gap-1.5 font-medium text-emerald-700 dark:text-emerald-400">
            <ClipboardCheck className="h-3.5 w-3.5" />
            导入完成（{result.action === 'updated' ? '更新已有账号' : '新增账号'}）
          </p>
          <ul className="mt-1 space-y-0.5 text-muted-foreground">
            <li>来源：{result.import.source}</li>
            <li>Token 长度：{result.import.token_length}</li>
            <li>Cookie：{result.import.cookie_names.join(', ') || '无'}</li>
            <li>Device ID：{result.import.device_id_preview || '未提供'}</li>
            {result.import.has_user_id && (
              <li>User ID：{result.import.user_id_preview}</li>
            )}
            {result.import.has_refresh_token && (
              <li>Refresh Token：已导入</li>
            )}
            {result.import.warnings.map((warning) => (
              <li key={warning} className="text-amber-600 dark:text-amber-400">
                {warning}
              </li>
            ))}
          </ul>
        </div>
      )}

      <Button onClick={handleImport} disabled={busy || !payload.trim()}>
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileUp className="h-4 w-4" />}
        导入并保存
      </Button>
    </div>
  )
}
