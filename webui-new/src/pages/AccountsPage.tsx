import { useState } from 'react'
import { Plus, RefreshCw, Users } from 'lucide-react'
import { PageHeader } from '@/components/layout/PageHeader'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
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
import { useApi } from '@/hooks/useApi'
import { del, post, put, ApiCallError } from '@/lib/api'
import { toast } from '@/hooks/use-toast'
import { AccountForm } from '@/components/accounts/AccountForm'
import { CredentialImportPanel } from '@/components/accounts/CredentialImportPanel'
import { AccountTable } from '@/components/accounts/AccountTable'
import { AccountDetailDrawer } from '@/components/accounts/AccountDetailDrawer'
import { KeepalivePanel } from '@/components/accounts/KeepalivePanel'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import type {
  Account,
  AccountsResponse,
  KeepaliveJobsResponse,
} from '@/lib/types'

export default function AccountsPage() {
  const { data, error, loading, refresh } = useApi<AccountsResponse>('/admin/api/accounts', {
    pollMs: 10000,
  })
  const {
    data: keepaliveData,
    refresh: refreshKeepalive,
  } = useApi<KeepaliveJobsResponse>('/admin/api/keepalive/jobs', {
    pollMs: 10000,
  })
  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<Account | null>(null)
  const [viewing, setViewing] = useState<Account | null>(null)
  const [deleting, setDeleting] = useState<Account | null>(null)
  const [providerFilter, setProviderFilter] = useState<'all' | 'deepseek' | 'zai' | 'kimi' | 'qianwen' | 'hunyuan' | 'wenxin' | 'doubao'>('all')
  const [keepaliveBusy, setKeepaliveBusy] = useState(false)

  const allAccounts = data?.accounts ?? []
  const accounts = providerFilter === 'all'
    ? allAccounts
    : allAccounts.filter((account) => account.provider === providerFilter)
  const providerCounts = {
    all: allAccounts.length,
    deepseek: allAccounts.filter((account) => account.provider === 'deepseek').length,
    zai: allAccounts.filter((account) => account.provider === 'zai').length,
    kimi: allAccounts.filter((account) => account.provider === 'kimi').length,
    qianwen: allAccounts.filter((account) => account.provider === 'qianwen').length,
    hunyuan: allAccounts.filter((account) => account.provider === 'hunyuan').length,
    wenxin: allAccounts.filter((account) => account.provider === 'wenxin').length,
    doubao: allAccounts.filter((account) => account.provider === 'doubao').length,
  }
  const keepaliveJobs = Object.fromEntries(
    (keepaliveData?.jobs ?? []).map((job) => [job.account_id, job]),
  )

  const refreshAll = () => {
    refresh()
    refreshKeepalive()
  }

  const handleKeepaliveBulk = async (action: 'enable' | 'disable' | 'probe') => {
    setKeepaliveBusy(true)
    try {
      await post('/admin/api/keepalive/actions/bulk', {
        action,
        provider: providerFilter === 'all' ? undefined : providerFilter,
      })
      toast({ title: '保活操作已提交', variant: 'success' })
      refreshKeepalive()
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '保活操作失败', description: msg, variant: 'destructive' })
    } finally {
      setKeepaliveBusy(false)
    }
  }

  const handleKeepaliveToggle = async (account: Account, enabled: boolean) => {
    setKeepaliveBusy(true)
    try {
      await put(`/admin/api/keepalive/jobs/${encodeURIComponent(account.id)}`, {
        enabled,
      })
      refreshKeepalive()
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '保活状态修改失败', description: msg, variant: 'destructive' })
    } finally {
      setKeepaliveBusy(false)
    }
  }

  const handleKeepaliveProbe = async (account: Account) => {
    setKeepaliveBusy(true)
    try {
      await post(`/admin/api/keepalive/jobs/${encodeURIComponent(account.id)}/probe`, {})
      toast({ title: '已加入立即检测队列', variant: 'success' })
      refreshKeepalive()
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '立即检测失败', description: msg, variant: 'destructive' })
    } finally {
      setKeepaliveBusy(false)
    }
  }

  const handleDelete = async () => {
    if (!deleting) return
    try {
      await del(`/admin/api/accounts/${encodeURIComponent(deleting.id)}`)
      toast({ title: '账号已删除', variant: 'success' })
      setDeleting(null)
      refreshAll()
    } catch (e) {
      const msg = e instanceof ApiCallError ? e.message : String(e)
      toast({ title: '删除失败', description: msg, variant: 'destructive' })
    }
  }

  return (
    <div className="space-y-6 animate-fade-in">
      <PageHeader
        title="账号池"
        description={`按提供商管理账号 · DeepSeek ${providerCounts.deepseek} · z.ai ${providerCounts.zai} · Kimi ${providerCounts.kimi} · 千问 ${providerCounts.qianwen} · 混元 ${providerCounts.hunyuan} · 文心 ${providerCounts.wenxin} · 豆包 ${providerCounts.doubao}`}
        actions={
          <>
            <Button variant="outline" onClick={refresh} disabled={loading}>
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              刷新
            </Button>
            <Button onClick={() => setAddOpen(true)}>
              <Plus className="h-4 w-4" />
              添加账号
            </Button>
          </>
        }
      />

      <Tabs
        value={providerFilter}
        onValueChange={(value) => setProviderFilter(value as 'all' | 'deepseek' | 'zai' | 'kimi' | 'qianwen' | 'hunyuan' | 'wenxin' | 'doubao')}
      >
        <TabsList className="grid w-full grid-cols-8">
          <TabsTrigger value="all">全部 {providerCounts.all}</TabsTrigger>
          <TabsTrigger value="deepseek">DeepSeek {providerCounts.deepseek}</TabsTrigger>
          <TabsTrigger value="zai">z.ai {providerCounts.zai}</TabsTrigger>
          <TabsTrigger value="kimi">Kimi {providerCounts.kimi}</TabsTrigger>
          <TabsTrigger value="qianwen">千问 {providerCounts.qianwen}</TabsTrigger>
          <TabsTrigger value="hunyuan">混元 {providerCounts.hunyuan}</TabsTrigger>
          <TabsTrigger value="wenxin">文心 {providerCounts.wenxin}</TabsTrigger>
          <TabsTrigger value="doubao">豆包 {providerCounts.doubao}</TabsTrigger>
        </TabsList>
      </Tabs>

      <KeepalivePanel
        status={keepaliveData?.status}
        providerFilter={providerFilter}
        busy={keepaliveBusy}
        onBulk={handleKeepaliveBulk}
      />

      <Card>
        <CardContent className="p-0">
          {loading && !data ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 5 }).map((_, i) => (
                <Skeleton key={i} className="h-10 w-full" />
              ))}
            </div>
          ) : error ? (
            <div className="p-8 text-center text-sm text-destructive">
              加载失败：{error.message}
            </div>
          ) : accounts.length === 0 ? (
            <div className="p-12 text-center">
              <Users className="mx-auto h-10 w-10 text-muted-foreground/40" />
              <h3 className="mt-3 text-sm font-medium">暂无账号</h3>
              <p className="mt-1 text-xs text-muted-foreground">
                点击右上角“添加账号”，也可以将账号写入 data/accounts.json。
              </p>
            </div>
          ) : (
            <AccountTable
              accounts={accounts}
              onEdit={(a) => setEditing(a)}
              onDelete={(a) => setDeleting(a)}
              onSelect={(a) => setViewing(a)}
              keepaliveJobs={keepaliveJobs}
              onKeepaliveToggle={handleKeepaliveToggle}
              onKeepaliveProbe={handleKeepaliveProbe}
            />
          )}
        </CardContent>
      </Card>

      {/* Add dialog */}
      <Dialog open={addOpen} onOpenChange={setAddOpen}>
        <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>添加账号</DialogTitle>
            <DialogDescription>
              从已登录浏览器导入凭据，或手动填写 Token、Cookies 与账号专属字段
            </DialogDescription>
          </DialogHeader>
          <Tabs defaultValue="import">
            <TabsList className="grid w-full grid-cols-7">
              <TabsTrigger value="import">DeepSeek 导入</TabsTrigger>
              <TabsTrigger value="zai">z.ai 导入</TabsTrigger>
              <TabsTrigger value="kimi">Kimi 导入</TabsTrigger>
              <TabsTrigger value="hunyuan">混元导入</TabsTrigger>
              <TabsTrigger value="wenxin">文心导入</TabsTrigger>
              <TabsTrigger value="doubao">豆包导入</TabsTrigger>
              <TabsTrigger value="manual">手动添加</TabsTrigger>
            </TabsList>
            <TabsContent value="import">
              <CredentialImportPanel
                onImported={() => {
                  setAddOpen(false)
                  refreshAll()
                }}
              />
            </TabsContent>
            <TabsContent value="zai">
              <CredentialImportPanel
                provider="zai"
                onImported={() => {
                  setAddOpen(false)
                  refreshAll()
                }}
              />
            </TabsContent>
            <TabsContent value="kimi">
              <CredentialImportPanel
                provider="kimi"
                onImported={() => {
                  setAddOpen(false)
                  refreshAll()
                }}
              />
            </TabsContent>
            <TabsContent value="hunyuan">
              <CredentialImportPanel
                provider="hunyuan"
                onImported={() => {
                  setAddOpen(false)
                  refreshAll()
                }}
              />
            </TabsContent>
            <TabsContent value="wenxin">
              <CredentialImportPanel
                provider="wenxin"
                onImported={() => {
                  setAddOpen(false)
                  refreshAll()
                }}
              />
            </TabsContent>
            <TabsContent value="doubao">
              <CredentialImportPanel
                provider="doubao"
                onImported={() => {
                  setAddOpen(false)
                  refreshAll()
                }}
              />
            </TabsContent>
            <TabsContent value="manual">
              <AccountForm
                onSaved={() => {
                  setAddOpen(false)
                  refreshAll()
                }}
                onCancel={() => setAddOpen(false)}
              />
            </TabsContent>
          </Tabs>
        </DialogContent>
      </Dialog>

      {/* Edit dialog */}
      <Dialog open={!!editing} onOpenChange={(o) => !o && setEditing(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>编辑账号</DialogTitle>
            <DialogDescription>
              留空 Token / Cookies 表示不修改对应字段
            </DialogDescription>
          </DialogHeader>
          {editing && (
            <AccountForm
              account={editing}
              onSaved={() => {
                setEditing(null)
                refreshAll()
              }}
              onCancel={() => setEditing(null)}
            />
          )}
        </DialogContent>
      </Dialog>

      {/* Delete confirm */}
      <AlertDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除账号？</AlertDialogTitle>
            <AlertDialogDescription>
              将删除账号 <code className="font-mono text-xs">{deleting?.id}</code>。
              此操作不可撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>取消</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete} className="bg-destructive text-destructive-foreground hover:bg-destructive/90">
              确认删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* Detail drawer */}
      <AccountDetailDrawer
        account={viewing}
        onClose={() => setViewing(null)}
        onRelogin={refreshAll}
      />
    </div>
  )
}
