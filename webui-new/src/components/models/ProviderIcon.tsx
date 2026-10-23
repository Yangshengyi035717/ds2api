import { normalizeProvider, type AccountProvider } from '@/lib/providers'
import { cn } from '@/lib/utils'

type IconProvider = AccountProvider | 'unknown'

interface ProviderIconProps {
  provider?: string | null
  size?: number
  className?: string
  title?: string
}

const ICON_META: Record<
  IconProvider,
  { label: string; background: string; foreground: string }
> = {
  deepseek: { label: 'DeepSeek', background: '#4D6BFE', foreground: '#FFFFFF' },
  zai: { label: 'z.ai', background: '#18181B', foreground: '#FFFFFF' },
  kimi: { label: 'Kimi', background: '#F97316', foreground: '#FFFFFF' },
  qianwen: { label: '千问', background: '#615CED', foreground: '#FFFFFF' },
  hunyuan: { label: '腾讯混元', background: '#0EA5E9', foreground: '#FFFFFF' },
  wenxin: { label: '百度文心', background: '#2563EB', foreground: '#FFFFFF' },
  doubao: { label: '豆包', background: '#3B82F6', foreground: '#FFFFFF' },
  mimo: { label: '小米 MiMo', background: '#FF6900', foreground: '#FFFFFF' },
  unknown: { label: '模型', background: '#64748B', foreground: '#FFFFFF' },
}

function IconGlyph({ provider }: { provider: IconProvider }) {
  switch (provider) {
    case 'deepseek':
      return (
        <>
          <path
            fill="currentColor"
            d="M4.2 9.1h4.6c2.8 0 5.3 1.2 7 3.5l1.1 1.4c.7.9 1.6 1.5 2.7 1.8-1 .5-2.1.7-3.2.7 1.1.9 2.4 1.3 3.8 1.3-1.2 1.2-2.7 1.8-4.5 1.8-2.9 0-5.4-1.3-7.5-3.8l-1.6-2h3.7c.6 0 .9-.3.9-.9 0-.5-.3-.8-.9-.8H3.1c.1-.8.5-1.4 1.1-2Z"
          />
          <circle cx="16.2" cy="7.6" r="1.1" fill="currentColor" />
          <path
            fill="currentColor"
            d="M18 6.6c1.2.4 2.2 1 3 1.7-1.5.2-2.9.7-4.1 1.5.5-1.2.9-2.3 1.1-3.2Z"
          />
        </>
      )
    case 'zai':
      return (
        <path
          fill="currentColor"
          d="M6.6 6.7h10.8l-6.7 7.8h6.5v2.8H6.2l6.7-7.8H6.6V6.7Z"
        />
      )
    case 'kimi':
      return (
        <path
          fill="currentColor"
          d="M6.4 5.5h3v5.2l4.7-5.2h3.7l-5.2 5.5 5.5 7.5h-3.7L10.3 13l-.9 1v4.5h-3v-13Z"
        />
      )
    case 'qianwen':
      return (
        <path
          fill="currentColor"
          fillRule="evenodd"
          d="M11.6 4.6a6.8 6.8 0 1 0 3.9 12.3l2.4 1.9 1.8-2.2-2.2-1.8A6.8 6.8 0 0 0 11.6 4.6Zm0 2.8a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z"
          clipRule="evenodd"
        />
      )
    case 'hunyuan':
      return (
        <path
          fill="currentColor"
          fillRule="evenodd"
          d="M8.5 5.5a6.1 6.1 0 1 0 3.5 11.1 6.1 6.1 0 1 0 3.5-11.1 6.1 6.1 0 0 0-3.5 1.1 6.1 6.1 0 0 0-3.5-1.1Zm0 2.6a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7Zm7 0a3.5 3.5 0 1 1 0 7 3.5 3.5 0 0 1 0-7Z"
          clipRule="evenodd"
        />
      )
    case 'wenxin':
      return (
        <path
          fill="currentColor"
          d="M4.5 6.4h2.9l1.5 6.2.9-3.4h2.7l.9 3.4 1.5-6.2h2.8l-2.7 11.1h-3.1l-.8-3.5-.8 3.5H7.2L4.5 6.4Z"
        />
      )
    case 'doubao':
      return (
        <path
          fill="currentColor"
          d="M6.2 8.1c2.5-2.5 8.2-2.6 10.9-.1 1.8 1.7 1.7 4.9-.4 7-2.5 2.5-7.4 2.5-9.9.2-2.2-2-2.5-5.1-.6-7.1Zm3.2 2.2c-.7.7-.6 1.9.2 2.6.9.7 2.1.6 2.8-.1.7-.7.6-1.8-.2-2.5-.9-.7-2.1-.7-2.8 0Z"
        />
      )
    case 'mimo':
      return (
        <path
          fill="currentColor"
          d="M4.5 18V6h3.3l4.2 5.5L16.2 6h3.3v12h-3.1V11l-4.4 5.7L7.6 11v7H4.5Z"
        />
      )
    default:
      return (
        <path
          fill="currentColor"
          d="m12 4.6 1.4 4.3 4.5 1.5-4.5 1.5-1.4 4.3-1.4-4.3-4.5-1.5 4.5-1.5L12 4.6Zm6.1 10.1.7 2.1 2.1.7-2.1.7-.7 2.1-.7-2.1-2.1-.7 2.1-.7.7-2.1Z"
        />
      )
  }
}

export function ProviderIcon({
  provider,
  size = 18,
  className,
  title,
}: ProviderIconProps) {
  const key = normalizeProvider(provider) ?? 'unknown'
  const meta = ICON_META[key]

  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center justify-center rounded-[28%] shadow-sm ring-1 ring-black/10',
        className,
      )}
      style={{
        width: size,
        height: size,
        backgroundColor: meta.background,
        color: meta.foreground,
      }}
      role="img"
      aria-label={`${meta.label} 模型`}
      title={title ?? meta.label}
    >
      <svg
        viewBox="0 0 24 24"
        width={size * 0.72}
        height={size * 0.72}
        aria-hidden="true"
      >
        <IconGlyph provider={key} />
      </svg>
    </span>
  )
}
