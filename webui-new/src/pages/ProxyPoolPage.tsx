import { useState } from 'react'
import {
  Activity,
  Network,
  Pencil,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { ProxyForm } from '@/components/proxies/ProxyForm'
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
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { useApi } from '@/hooks/useApi'
import { useToast } from '@/hooks/use-toast'
import { del, post, ApiCallError } from '@/lib/api'
import { formatLatency } from '@/lib/utils'
import type { ProxiesResponse, ProxyConfig } from '@/lib/types'

export default function ProxyPoolPage() {
  const { data, error, loading, refresh } = useApi<ProxiesResponse>('/admin/api/proxies')
  const { toast } = useToast()
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<ProxyConfig | null>(null)
  const [deleting, setDeleting] = useState<ProxyConfig | null>(null)
  const [testing, setTesting] = useState<string | null>(null)

  const proxies = data?.proxies ?? []

  const handleTest = async (proxy: ProxyConfig) => {
    setTesting(proxy.id)
    try {
      const result = await post<{ ok: boolean; status: number | null; latency_ms: number; error: string }>(
        `/admin/api/proxies/${encodeURIComponent(proxy.id)}/test`,
      )
      toast({
        title: result.ok ? '代理连通性正常' : '代理检测失败',
        description: result.ok
          ? `HTTP ${result.status ?? '—'} · ${formatLatency(result.latency_ms)}`
          : result.error || `HTTP ${result.status ?? '—'}`,
        variant: result.ok ? 'success' : 'destructive',
      })
      refresh()
    } catch (e) {
      const message = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '代理检测失败', description: message, variant: 'destructive' })
    } finally {
      setTesting(null)
    }
  }

  const handleDelete = async () => {
    if (!deleting) return
    try {
      await del(`/admin/api/proxies/${encodeURIComponent(deleting.id)}`)
      toast({ title: '代理已删除', variant: 'success' })
      setDeleting(null)
      refresh()
    } catch (e) {
      const message = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '删除代理失败', description: message, variant: 'destructive' })
    }
  }

  return (
    <div className="space-y-5 animate-fade-in">
      <PageHeader
        title="代理池"
        description={`按账号绑定稳定出口 · 共 ${data?.total ?? '—'} 个，可用 ${data?.available ?? '—'} 个`}
        actions={
          <>
            <Button variant="outline" onClick={refresh} disabled={loading}>
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              刷新
            </Button>
            <Button onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" />
              添加代理
            </Button>
          </>
        }
      />

      <Card>
        <CardContent className="flex flex-col gap-3 p-4 text-sm text-muted-foreground sm:flex-row sm:items-start">
          <ShieldCheck className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
          <div className="space-y-1">
            <p className="font-medium text-foreground">一号一代理，不做请求级轮换</p>
            <p className="text-xs leading-relaxed">
              每个代理默认只能绑定一个账号。保持同一账号长期使用同一边出口，比每条请求随机换 IP
              更稳定，也能减少同一 IP 关联多个账号的概率。
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {error ? (
            <div className="p-8 text-center text-sm text-destructive">加载失败：{error.message}</div>
          ) : proxies.length === 0 && !loading ? (
            <div className="p-12 text-center">
              <Network className="mx-auto h-10 w-10 text-muted-foreground/40" />
              <h3 className="mt-3 text-sm font-medium">代理池为空</h3>
              <p className="mt-1 text-xs text-muted-foreground">添加代理后，可在账号编辑页分配给账号。</p>
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>地址</TableHead>
                  <TableHead>绑定账号</TableHead>
                  <TableHead>检测状态</TableHead>
                  <TableHead>启用</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {proxies.map((proxy) => (
                  <TableRow key={proxy.id}>
                    <TableCell>
                      <div className="max-w-[200px] truncate font-medium">{proxy.label}</div>
                      <div className="font-mono text-[10px] text-muted-foreground">{proxy.id}</div>
                    </TableCell>
                    <TableCell>
                      <code className="font-mono text-xs text-muted-foreground">{proxy.url_preview}</code>
                    </TableCell>
                    <TableCell>
                      {proxy.bound_count > 0 ? (
                        <div className="flex flex-wrap gap-1">
                          {proxy.bound_accounts.map((name) => (
                            <Badge key={name} variant="secondary">{name}</Badge>
                          ))}
                        </div>
                      ) : (
                        <span className="text-xs text-muted-foreground">未绑定</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {proxy.last_check_at === 0 ? (
                        <Badge variant="outline">未检测</Badge>
                      ) : proxy.last_check_ok ? (
                        <div className="space-y-1">
                          <Badge variant="success">正常</Badge>
                          <div className="text-[10px] text-muted-foreground">
                            HTTP {proxy.last_check_status ?? '—'} · {formatLatency(proxy.last_check_latency_ms)}
                          </div>
                        </div>
                      ) : (
                        <div className="space-y-1">
                          <Badge variant="destructive">失败</Badge>
                          <div className="max-w-[180px] truncate text-[10px] text-muted-foreground" title={proxy.last_check_error}>
                            {proxy.last_check_error || `HTTP ${proxy.last_check_status ?? '—'}`}
                          </div>
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={proxy.enabled ? 'success' : 'secondary'}>
                        {proxy.enabled ? '启用' : '停用'}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void handleTest(proxy)}
                          disabled={testing === proxy.id || !proxy.enabled}
                        >
                          <Activity className={`h-3.5 w-3.5 ${testing === proxy.id ? 'animate-pulse' : ''}`} />
                          检测
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7" onClick={() => setEditing(proxy)} aria-label="编辑代理">
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-muted-foreground hover:text-destructive"
                          onClick={() => setDeleting(proxy)}
                          aria-label="删除代理"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>添加代理</DialogTitle>
            <DialogDescription>代理地址只在服务端和脱敏预览中展示。</DialogDescription>
          </DialogHeader>
          <ProxyForm
            onSaved={() => {
              setAddOpen(false)
              refresh()
            }}
            onCancel={() => setAddOpen(false)}
          />
        </DialogContent>
      </Dialog>

      <Dialog open={!!editing} onOpenChange={(open) => !open && setEditing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>编辑代理</DialogTitle>
            <DialogDescription>修改地址后，绑定账号的缓存连接会自动重建。</DialogDescription>
          </DialogHeader>
          {editing && (
            <ProxyForm
              proxy={editing}
              onSaved={() => {
                setEditing(null)
                refresh()
              }}
              onCancel={() => setEditing(null)}
            />
          )}
        </DialogContent>
      </Dialog>

      <AlertDialog open={!!deleting} onOpenChange={(open) => !open && setDeleting(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除代理？</AlertDialogTitle>
            <AlertDialogDescription>
              {deleting?.bound_count
                ? `该代理仍绑定 ${deleting.bound_count} 个账号，请先在账号编辑页解除绑定。`
                : '删除后不可恢复。'}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => void handleDelete()}
              disabled={Boolean(deleting?.bound_count)}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
