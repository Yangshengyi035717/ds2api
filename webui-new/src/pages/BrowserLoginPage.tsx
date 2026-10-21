import { useCallback, useEffect, useRef, useState } from 'react'
import { ExternalLink, Loader2, MonitorPlay, RefreshCw, Square } from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { get, post, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import type { BrowserLoginStatus } from '@/lib/types'

const STATUS_LABELS: Record<string, string> = {
  idle: '未启动',
  starting: '启动中',
  waiting_login: '等待登录',
  importing: '导入中',
  completed: '已完成',
  stopped: '已停止',
  error: '出错',
  disabled: '未配置',
  unavailable: '服务不可用',
}

export default function BrowserLoginPage() {
  const [status, setStatus] = useState<BrowserLoginStatus | null>(null)
  const [firstLoad, setFirstLoad] = useState(true)
  const [busy, setBusy] = useState(false)
  const [frameReady, setFrameReady] = useState(false)
  const [frameSrc, setFrameSrc] = useState('')
  const completedRef = useRef(false)
  const frameMountedRef = useRef(false)

  const refresh = useCallback(async () => {
    try {
      const next = await get<BrowserLoginStatus>('/admin/api/browser-login/status')
      setStatus(next)
      setFirstLoad(false)
      if (next.status === 'completed' && !completedRef.current) {
        completedRef.current = true
        toast({
          title: '登录完成',
          description: '凭据已导入账号池，浏览器即将关闭',
          variant: 'success',
        })
      }
    } catch (e) {
      const message = e instanceof ApiCallError ? e.message : String(e)
      setStatus({ enabled: false, status: 'unavailable', message })
      setFirstLoad(false)
    }
  }, [])

  useEffect(() => {
    refresh()
    const timer = setInterval(refresh, 3000)
    return () => clearInterval(timer)
  }, [refresh])

  useEffect(() => {
    if (!status || frameReady) return
    if (frameMountedRef.current) return
    if (!['starting', 'waiting_login', 'importing'].includes(status.status)) return
    let cancelled = false
    const restore = async () => {
      try {
        const result = await get<BrowserLoginStatus>('/admin/api/browser-login/view-token')
        if (cancelled || !result.view_token) return
        const exchange = await fetch(
          `/browser-login/view?token=${encodeURIComponent(result.view_token)}`,
          { credentials: 'same-origin' },
        )
        if (!exchange.ok) return
        setFrameSrc(
          '/browser-login/vnc.html?autoconnect=1&resize=scale&reconnect=1&path=/browser-login/websockify',
        )
        setFrameReady(true)
        frameMountedRef.current = true
      } catch {
        // The sidecar may still be launching; the next poll retries.
      }
    }
    restore()
    return () => {
      cancelled = true
    }
  }, [status, frameReady])

  const start = async (resetProfile: boolean) => {
    if (busy) return
    setBusy(true)
    completedRef.current = false
    setFrameReady(false)
    try {
      const result = await post<BrowserLoginStatus>('/admin/api/browser-login/start', {
        reset_profile: resetProfile,
      })
      setStatus(result)
      if (result.status === 'error' || result.status === 'unavailable') {
        throw new Error(result.message || '浏览器启动失败')
      }
      const viewToken = result.view_token
      if (!viewToken) {
        throw new Error('服务端没有返回 noVNC 访问令牌')
      }
      const exchange = await fetch(
        `/browser-login/view?token=${encodeURIComponent(viewToken)}`,
        { credentials: 'same-origin' },
      )
      if (!exchange.ok) {
        throw new Error(`noVNC 令牌交换失败（HTTP ${exchange.status}）`)
      }
      // Give Xvfb + Chromium a moment, then mount the frame.
      const iframePath =
        '/browser-login/vnc.html?autoconnect=1&resize=scale&reconnect=1&path=/browser-login/websockify'
      setTimeout(() => {
        setFrameSrc(iframePath)
        setFrameReady(true)
        frameMountedRef.current = true
      }, 1500)
      toast({ title: '浏览器正在启动', description: '首次启动约需 10–30 秒' })
    } catch (e) {
      const message = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '启动失败', description: message, variant: 'destructive' })
    } finally {
      setBusy(false)
    }
  }

  const stop = async () => {
    if (busy) return
    setBusy(true)
    try {
      const result = await post<BrowserLoginStatus>('/admin/api/browser-login/stop')
      setStatus(result)
      setFrameReady(false)
      frameMountedRef.current = false
      setFrameSrc('')
      toast({ title: '浏览器已停止' })
    } catch (e) {
      const message = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '停止失败', description: message, variant: 'destructive' })
    } finally {
      setBusy(false)
    }
  }

  const current = status?.status ?? 'idle'
  const active = ['starting', 'waiting_login', 'importing'].includes(current)

  return (
    <div className="space-y-4 animate-fade-in">
      <PageHeader
        title="浏览器登录"
        description="在服务器浏览器中完成 DeepSeek 登录，凭据自动导入账号池"
        actions={
          <>
            <Button variant="outline" onClick={refresh}>
              <RefreshCw className="h-4 w-4" />
              刷新状态
            </Button>
            {active ? (
              <Button variant="destructive" onClick={stop} disabled={busy}>
                <Square className="h-4 w-4" />
                停止
              </Button>
            ) : (
              <Button onClick={() => start(true)} disabled={busy || firstLoad || status?.enabled === false}>
                {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <MonitorPlay className="h-4 w-4" />}
                启动登录
              </Button>
            )}
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border bg-card px-4 py-3">
        <span className="rounded-full border bg-background px-2.5 py-1 text-xs font-medium">
          {STATUS_LABELS[current] ?? current}
        </span>
        <p className="min-w-0 flex-1 text-sm text-muted-foreground">
          {status?.message || '读取中…'}
        </p>
        {status?.account_id && (
          <code className="rounded bg-muted px-2 py-1 font-mono text-[11px]">
            {status.account_id}
          </code>
        )}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <Card className="order-1 overflow-hidden">
          <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
            <CardTitle className="text-base">远程浏览器</CardTitle>
            <Button variant="outline" size="sm" asChild>
              <a
                href={frameSrc || '/browser-login/vnc.html?autoconnect=1&resize=scale'}
                target="_blank"
                rel="noreferrer"
                className={!frameReady ? 'pointer-events-none opacity-50' : undefined}
                onClick={(event) => {
                  if (!frameReady) event.preventDefault()
                }}
              >
                <ExternalLink className="h-3.5 w-3.5" />
                新窗口
              </a>
            </Button>
          </CardHeader>
          <CardContent className="p-0">
            {frameReady ? (
              <iframe
                title="DeepSeek 浏览器登录"
                src={frameSrc}
                className="h-[calc(100vh-270px)] min-h-[420px] max-h-[780px] w-full border-0 bg-black"
                allow="clipboard-read; clipboard-write"
              />
            ) : (
              <div className="flex h-[calc(100vh-270px)] min-h-[420px] max-h-[780px] items-center justify-center bg-muted/30 text-sm text-muted-foreground">
                {active ? (
                  <span className="flex items-center gap-2">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    浏览器启动后这里会显示远程画面
                  </span>
                ) : (
                  '点击右上角“启动登录”开始'
                )}
              </div>
            )}
          </CardContent>
        </Card>

        <div className="order-2 space-y-4 xl:sticky xl:top-4 xl:self-start">
          <Card>
            <CardHeader className="pb-2">
              <CardTitle className="text-base">会话说明</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3 text-xs leading-relaxed text-muted-foreground">
              <p>启动服务器 Chromium，在远程画面完成账号密码和验证码。</p>
              <p>检测到有效登录后，token、Cookie 与 device_id 会自动进入账号池。</p>
              <p>浏览器完成后自动关闭，后续由协议保活维持登录态。</p>
            </CardContent>
          </Card>

          {status?.enabled === false && (
            <Card className="border-warning/40 bg-warning/5">
              <CardContent className="p-4 text-xs leading-relaxed text-muted-foreground">
                浏览器登录服务尚未部署。请启动 browser-login 容器并配置
                <code className="mx-1 font-mono">BROWSER_LOGIN_URL</code>
                与 <code className="font-mono">BROWSER_LOGIN_SHARED_SECRET</code>。
              </CardContent>
            </Card>
          )}

          {current === 'waiting_login' && (
            <Card className="border-warning/40 bg-warning/5">
              <CardContent className="p-4 text-xs leading-relaxed text-muted-foreground">
                如果画面显示 <code className="font-mono">403 Request blocked</code>，
                说明服务器出口 IP 被上游 CDN 拦截。请配置
                <code className="mx-1 font-mono">BROWSER_LOGIN_PROXY</code>
                后重启 browser-login 容器。
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}
