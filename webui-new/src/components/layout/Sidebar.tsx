import { NavLink, useLocation } from 'react-router-dom'
import {
  Activity,
  BookOpenText,
  ChevronsLeft,
  ChevronsRight,
  FlaskConical,
  LogIn,
  Network,
  Settings,
  Users,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { useState } from 'react'

interface NavItem {
  to: string
  label: string
  icon: React.ComponentType<{ className?: string }>
  end?: boolean
}

const NAV_GROUPS: Array<{ label: string; items: NavItem[] }> = [
  {
    label: '运行',
    items: [
      { to: '/', label: '概览', icon: Activity, end: true },
      { to: '/accounts', label: '账号池', icon: Users },
      { to: '/browser-login', label: '浏览器登录', icon: LogIn },
      { to: '/proxies', label: '代理池', icon: Network },
    ],
  },
  {
    label: '开发',
    items: [
      { to: '/api-docs', label: 'API 文档', icon: BookOpenText },
      { to: '/playground', label: 'Playground', icon: FlaskConical },
    ],
  },
  {
    label: '管理',
    items: [{ to: '/settings', label: '设置', icon: Settings }],
  },
]

export function Sidebar() {
  // The Codex side panel is often 700-900px wide. Start as an icon rail so
  // the actual page gets enough horizontal room; users can still expand it.
  const [collapsed, setCollapsed] = useState(
    () => typeof window !== 'undefined' && window.matchMedia('(max-width: 1100px)').matches,
  )
  const location = useLocation()

  return (
    <aside
      className={cn(
        'flex flex-col border-r bg-card transition-[width] duration-200',
        collapsed ? 'w-16' : 'w-56',
      )}
    >
      {/* Brand */}
      <div className="flex h-14 items-center border-b px-4">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-gradient-to-br from-primary to-primary/60 text-primary-foreground font-bold text-sm">
          D
        </div>
        {!collapsed && (
          <div className="ml-2.5 flex-1 overflow-hidden">
            <div className="truncate text-sm font-semibold">DS2API</div>
            <div className="truncate text-[10px] text-muted-foreground tracking-wider">
              管理面板
            </div>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 space-y-4 overflow-y-auto p-2 scrollbar-thin">
        {NAV_GROUPS.map((group) => (
          <div key={group.label} className="space-y-1">
            {!collapsed ? (
              <div className="px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-[0.16em] text-muted-foreground/70">
                {group.label}
              </div>
            ) : (
              <div className="mx-auto my-2 h-px w-6 bg-border" aria-hidden />
            )}
            {group.items.map((item) => {
              const Icon = item.icon
              const active = item.end
                ? location.pathname === item.to
                : location.pathname.startsWith(item.to)
              return (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end}
                  className={cn(
                    'relative flex h-10 items-center gap-2.5 rounded-md px-2.5 text-sm font-medium transition-colors',
                    collapsed && 'justify-center px-0',
                    active
                      ? 'bg-primary/15 text-primary'
                      : 'text-muted-foreground hover:bg-secondary hover:text-foreground',
                  )}
                  title={collapsed ? item.label : undefined}
                >
                  {active && (
                    <span className="absolute inset-y-2 left-0 w-0.5 rounded-full bg-primary" />
                  )}
                  <Icon className="h-4 w-4 shrink-0" />
                  {!collapsed && <span className="truncate">{item.label}</span>}
                </NavLink>
              )
            })}
          </div>
        ))}
      </nav>

      {/* Footer */}
      <div className="border-t p-2">
        <Button
          variant="ghost"
          size="sm"
          className="w-full justify-center"
          onClick={() => setCollapsed(!collapsed)}
          aria-label={collapsed ? '展开侧边栏' : '折叠侧边栏'}
        >
          {collapsed ? (
            <ChevronsRight className="h-4 w-4" />
          ) : (
            <>
              <ChevronsLeft className="h-4 w-4" />
              <span>折叠</span>
            </>
          )}
        </Button>
      </div>
    </aside>
  )
}
