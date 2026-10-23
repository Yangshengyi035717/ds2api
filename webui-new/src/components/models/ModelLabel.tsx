import { ProviderIcon } from '@/components/models/ProviderIcon'
import { resolveModelProvider } from '@/lib/providers'
import { cn } from '@/lib/utils'

interface ModelLabelProps {
  modelId: string
  provider?: string | null
  ownedBy?: string | null
  displayName?: string | null
  label?: string
  iconSize?: number
  className?: string
  textClassName?: string
}

export function modelDisplayText({
  modelId,
  displayName,
  label,
}: Pick<ModelLabelProps, 'modelId' | 'displayName' | 'label'>) {
  return label || displayName || modelId
}

export function ModelLabel({
  modelId,
  provider,
  ownedBy,
  displayName,
  label,
  iconSize = 16,
  className,
  textClassName,
}: ModelLabelProps) {
  const resolvedProvider = resolveModelProvider({ provider, ownedBy, modelId })

  return (
    <span className={cn('flex min-w-0 items-center gap-2', className)}>
      <ProviderIcon provider={resolvedProvider ?? provider} size={iconSize} />
      <span className={cn('min-w-0 truncate', textClassName)}>
        {modelDisplayText({ modelId, displayName, label })}
      </span>
    </span>
  )
}
