import { useMemo, useState } from 'react'
import { ClipboardCheck, ClipboardCopy, FileUp, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { post, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import type { Account, CredentialImportResponse } from '@/lib/types'

const EXPORT_SCRIPT = `(() => {
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

interface Props {
  onImported: (account: Account) => void
}

export function CredentialImportPanel({ onImported }: Props) {
  const [payload, setPayload] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<CredentialImportResponse | null>(null)
  const charCount = payload.length

  const placeholder = useMemo(
    () =>
      '粘贴 DevTools 导出的 HAR，或在 chat.deepseek.com 控制台运行“复制导出脚本”后粘贴输出的 JSON。',
    [],
  )

  const copyScript = async () => {
    try {
      await navigator.clipboard.writeText(EXPORT_SCRIPT)
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
      })
      setResult(res)
      setPayload('')
      onImported(res.account)
      toast({
        title: res.action === 'updated' ? '账号凭据已更新' : '账号已导入',
        description: res.import.has_device_id
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
        <p>
          DeepSeek 登录使用设备绑定的 MFA 签名，服务器无法代替浏览器完成密码登录。
          推荐在已登录的 <code className="font-mono">chat.deepseek.com</code>{' '}
          页面导出凭据后粘贴到这里；退出登录会导致旧 token 立即失效。
        </p>
      </div>

      <div className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Label htmlFor="credential-import">凭据内容（HAR 或浏览器导出 JSON）</Label>
          <Button type="button" size="sm" variant="outline" onClick={copyScript}>
            <ClipboardCopy className="h-3.5 w-3.5" />
            复制浏览器导出脚本
          </Button>
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
          {charCount > 0 ? `${charCount.toLocaleString()} 字符` : '支持整份 HAR，服务端只提取 token / cookie / device_id'}
        </p>
      </div>

      <div className="rounded-md border p-3 text-xs text-muted-foreground">
        <p className="font-medium text-foreground">浏览器导出步骤</p>
        <ol className="mt-1 list-decimal space-y-1 pl-4">
          <li>在浏览器打开并登录 chat.deepseek.com。</li>
          <li>打开 DevTools → Console，运行上方脚本。</li>
          <li>脚本会把 JSON 复制到剪贴板，粘贴到上面即可。</li>
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
