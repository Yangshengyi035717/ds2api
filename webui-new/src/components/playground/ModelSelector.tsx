import { useEffect, useState } from 'react'
import { ModelLabel } from '@/components/models/ModelLabel'
import { Select, SelectContent, SelectItem, SelectTrigger } from '@/components/ui/select'
import { useApi } from '@/hooks/useApi'
import { ApiCallError } from '@/lib/api'
import type { ModelsResponse } from '@/lib/types'

interface Props {
  value: string
  onChange: (v: string) => void
  /** Optional display-label map (e.g. annotate each name's capability preset). */
  labels?: Record<string, string>
}

export function ModelSelector({ value, onChange, labels }: Props) {
  const { data, error } = useApi<ModelsResponse>('/v1/models')
  const [firstRender, setFirstRender] = useState(true)

  // Pick the first model as default if nothing is set yet.
  useEffect(() => {
    if (firstRender && !value && data?.data && data.data.length > 0) {
      onChange(data.data[0].id)
      setFirstRender(false)
    }
  }, [data, value, onChange, firstRender])

  if (error) {
    return (
      <div className="text-xs text-destructive">
        无法加载模型列表：{' '}
        {error instanceof ApiCallError ? error.message : String(error)}
      </div>
    )
  }

  const models = data?.data ?? []
  const selected = models.find((model) => model.id === value)

  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger>
        <div className="min-w-0 flex-1">
          {selected ? (
            <ModelLabel
              modelId={selected.id}
              provider={selected.provider}
              ownedBy={selected.owned_by}
              displayName={selected.display_name}
              label={labels?.[selected.id]}
            />
          ) : (
            <span className="text-muted-foreground">选择模型…</span>
          )}
        </div>
      </SelectTrigger>
      <SelectContent>
        {models.map((m) => (
          <SelectItem key={m.id} value={m.id}>
            <ModelLabel
              modelId={m.id}
              provider={m.provider}
              ownedBy={m.owned_by}
              displayName={m.display_name}
              label={labels?.[m.id]}
            />
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
