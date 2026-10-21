import { Moon, Sun, Monitor, LogOut, RefreshCw } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuthStore } from '@/stores/auth'
import { useThemeStore, applyTheme } from '@/stores/theme'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useTheme } from '@/hooks/useTheme'

interface TopBarProps {
  onRefresh?: () => void
  refreshing?: boolean
}

const PAGE_LABELS: Array<[string, string]> = [
  ['/accounts', '账号池'],
  ['/browser-login', '浏览器登录'],
  ['/proxies', '代理池'],
  ['/api-docs', 'API 文档'],
  ['/playground', 'Playground'],
  ['/settings', '设置'],
]

export function TopBar({ onRefresh, refreshing }: TopBarProps) {
  const theme = useThemeStore((s) => s.theme)
  const setTheme = useThemeStore((s) => s.setTheme)
  const logout = useAuthStore((s) => s.logout)
  const navigate = useNavigate()
  const location = useLocation()
  const { effective } = useTheme()

  const handleLogout = async () => {
    try {
      await fetch('/admin/api/logout', {
        method: 'POST',
        headers: { Authorization: `Bearer ${useAuthStore.getState().token}` },
      })
    } catch {
      // ignore — local logout still works
    }
    logout()
    navigate('/login', { replace: true })
  }

  const ThemeIcon = effective === 'dark' ? Moon : Sun
  const pageLabel =
    PAGE_LABELS.find(([prefix]) => location.pathname.startsWith(prefix))?.[1] ?? '概览'

  return (
    <header className="flex h-14 items-center justify-between gap-3 border-b bg-card/50 px-3 backdrop-blur sm:px-4">
      <div className="min-w-0">
        <div className="truncate text-sm font-medium">{pageLabel}</div>
        <div className="flex items-center gap-1.5 text-[10px] text-muted-foreground">
          <span className="h-1.5 w-1.5 rounded-full bg-success" />
          DS2API 管理端
        </div>
      </div>

      <div className="flex items-center gap-1.5">
        {onRefresh && (
          <Button variant="ghost" size="icon" onClick={onRefresh} disabled={refreshing} aria-label="刷新">
            <RefreshCw className={`h-4 w-4 ${refreshing ? 'animate-spin' : ''}`} />
          </Button>
        )}

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" aria-label="切换主题">
              <ThemeIcon className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-40">
            <DropdownMenuLabel>主题</DropdownMenuLabel>
            <DropdownMenuItem onClick={() => { setTheme('light'); applyTheme('light') }}>
              <Sun className="mr-2 h-4 w-4" /> 浅色
              {theme === 'light' && <span className="ml-auto text-primary">✓</span>}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => { setTheme('dark'); applyTheme('dark') }}>
              <Moon className="mr-2 h-4 w-4" /> 深色
              {theme === 'dark' && <span className="ml-auto text-primary">✓</span>}
            </DropdownMenuItem>
            <DropdownMenuItem onClick={() => { setTheme('system'); applyTheme('system') }}>
              <Monitor className="mr-2 h-4 w-4" /> 跟随系统
              {theme === 'system' && <span className="ml-auto text-primary">✓</span>}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>

        <Button variant="ghost" size="icon" onClick={handleLogout} aria-label="退出登录">
          <LogOut className="h-4 w-4" />
        </Button>
      </div>
    </header>
  )
}
