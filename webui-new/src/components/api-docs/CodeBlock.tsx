import { useState } from 'react'
import { Check, Copy } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

interface CodeBlockProps {
  code: string
  title?: string
  language?: string
  className?: string
}

export function CodeBlock({ code, title, language, className }: CodeBlockProps) {
  const [copied, setCopied] = useState(false)

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }

  return (
    <div className={cn('overflow-hidden rounded-lg border bg-muted/30', className)}>
      <div className="flex h-9 items-center justify-between border-b bg-muted/50 px-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-xs font-medium">{title ?? '示例'}</span>
          {language && (
            <span className="rounded bg-background/70 px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
              {language}
            </span>
          )}
        </div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-2"
          onClick={() => void copy()}
          aria-label="复制代码"
        >
          {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          {copied ? '已复制' : '复制'}
        </Button>
      </div>
      <pre className="max-h-[420px] overflow-auto p-3 text-xs leading-relaxed scrollbar-thin">
        <code className="font-mono">{code}</code>
      </pre>
    </div>
  )
}
