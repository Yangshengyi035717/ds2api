import { Activity, PauseCircle, PlayCircle, RotateCw } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import type { KeepaliveStatus } from '@/lib/types'
import { getProviderLabel, type AccountProviderFilter } from '@/lib/providers'

interface Props {
  status?: KeepaliveStatus
  providerFilter: AccountProviderFilter
  busy: boolean
  onBulk: (action: 'enable' | 'disable' | 'probe') => void
}

export function KeepalivePanel({ status, providerFilter, busy, onBulk }: Props) {
  const provider = providerFilter === 'all' ? undefined : getProviderLabel(providerFilter)
  return (
    <Card className="border-dashed">
      <CardContent className="flex flex-col gap-3 p-4 xl:flex-row xl:items-center xl:justify-between">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="flex items-center gap-1.5 font-medium">
            <Activity className="h-4 w-4" />
            多账号协议保活
          </span>
          <Badge variant={status?.running ? 'success' : 'secondary'}>
            {status?.running ? '调度中' : status?.enabled ? '启动中' : '已停用'}
          </Badge>
          <span className="text-xs text-muted-foreground">
            正常 {status?.active ?? 0} · 冷却 {status?.cooldown ?? 0} · 待重登{' '}
            {status?.relogin_required ?? 0} · 执行中 {status?.running_count ?? 0}
          </span>
          {!status?.enabled && (
            <span className="text-xs text-warning">
              需设置 KEEPALIVE_ENABLED=true 后重启
            </span>
          )}
          {(providerFilter === 'all' || providerFilter === 'mimo') && (
            <p className="w-full text-xs text-muted-foreground">
              小米 MiMo 保活只验证登录态并回写轮换 Cookie；Cookie 过期后需重新登录并导入。
            </p>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => onBulk('probe')}
          >
            <RotateCw className="h-3.5 w-3.5" />
            立即检测{provider ? ` ${provider}` : '全部'}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => onBulk('enable')}
          >
            <PlayCircle className="h-3.5 w-3.5" />
            启用{provider ? ` ${provider}` : '全部'}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => onBulk('disable')}
          >
            <PauseCircle className="h-3.5 w-3.5" />
            暂停{provider ? ` ${provider}` : '全部'}
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
