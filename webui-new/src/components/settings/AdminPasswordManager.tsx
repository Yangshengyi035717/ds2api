import { useCallback, useEffect, useState } from 'react'
import { KeyRound, Loader2, ShieldCheck } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
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
import { ApiCallError, get, put } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import { useAuthStore } from '@/stores/auth'

interface PasswordStatus {
  set: boolean
  source: 'webui' | 'env' | string
  weak: boolean
  updated_at: number
}

interface ChangeResponse extends PasswordStatus {
  ok: boolean
  relogin_required: boolean
}

export function AdminPasswordManager() {
  const navigate = useNavigate()
  const logout = useAuthStore((state) => state.logout)
  const [status, setStatus] = useState<PasswordStatus | null>(null)
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setStatus(await get<PasswordStatus>('/admin/api/admin-password'))
      setError(null)
    } catch (e) {
      const message = e instanceof ApiCallError ? e.message : String(e)
      setError(message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const changePassword = async () => {
    if (saving) return
    if (!currentPassword) {
      toast({ title: '请输入当前密码', variant: 'destructive' })
      return
    }
    if (newPassword.length < 8) {
      toast({ title: '新密码至少需要 8 个字符', variant: 'destructive' })
      return
    }
    if (newPassword !== confirmPassword) {
      toast({ title: '两次输入的新密码不一致', variant: 'destructive' })
      return
    }

    setSaving(true)
    setError(null)
    try {
      const result = await put<ChangeResponse>('/admin/api/admin-password', {
        current_password: currentPassword,
        new_password: newPassword,
      })
      setStatus(result)
      setCurrentPassword('')
      setNewPassword('')
      setConfirmPassword('')
      toast({
        title: '管理员密码已更新',
        description: '所有管理会话已失效，请使用新密码重新登录。',
        variant: 'success',
      })
      logout()
      navigate('/login', { replace: true })
    } catch (e) {
      const message = e instanceof ApiCallError ? e.message : String(e)
      setError(message)
      toast({ title: '修改密码失败', description: message, variant: 'destructive' })
    } finally {
      setSaving(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2 text-base">
              <ShieldCheck className="h-4 w-4" />
              管理员密码
            </CardTitle>
            <CardDescription>
              修改 WebUI 登录密码。服务端只保存 PBKDF2-SHA256 verifier，不保存明文。
            </CardDescription>
          </div>
          {status && (
            <Badge variant={status.source === 'webui' ? 'success' : 'secondary'}>
              {status.source === 'webui' ? 'WebUI 持久化' : '.env 初始值'}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {loading && !status ? (
          <p className="text-sm text-muted-foreground">正在读取密码状态…</p>
        ) : (
          <>
            <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-2">
              <div className="rounded-md border px-3 py-2">
                状态：
                <span className={status?.weak ? 'text-destructive' : 'text-foreground'}>
                  {status?.weak ? '弱密码' : '已设置'}
                </span>
              </div>
              <div className="rounded-md border px-3 py-2">
                最后修改：
                <span className="text-foreground">
                  {status?.updated_at
                    ? new Date(status.updated_at * 1000).toLocaleString()
                    : '尚未从 WebUI 修改'}
                </span>
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5 sm:col-span-2">
                <Label htmlFor="admin-current-password">当前密码</Label>
                <Input
                  id="admin-current-password"
                  type="password"
                  value={currentPassword}
                  onChange={(event) => setCurrentPassword(event.target.value)}
                  autoComplete="current-password"
                  disabled={saving}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="admin-new-password">新密码</Label>
                <Input
                  id="admin-new-password"
                  type="password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  autoComplete="new-password"
                  minLength={8}
                  maxLength={256}
                  disabled={saving}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="admin-confirm-password">确认新密码</Label>
                <Input
                  id="admin-confirm-password"
                  type="password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  autoComplete="new-password"
                  minLength={8}
                  maxLength={256}
                  disabled={saving}
                />
              </div>
            </div>

            <p className="rounded-md border border-dashed p-3 text-[11px] leading-relaxed text-muted-foreground">
              保存后所有管理员会话立即失效，需要用新密码重新登录。删除
              <code className="mx-1 font-mono">data/admin_auth.json</code>
              可恢复使用 <code className="font-mono">.env</code> 中的初始密码。
            </p>

            {error && <p className="text-sm text-destructive">{error}</p>}

            <Button
              onClick={() => void changePassword()}
              disabled={
                saving ||
                !currentPassword ||
                newPassword.length < 8 ||
                newPassword !== confirmPassword
              }
            >
              {saving ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <KeyRound className="h-4 w-4" />
              )}
              {saving ? '正在修改…' : '修改密码并退出所有会话'}
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  )
}
