export const ACCOUNT_PROVIDERS = [
  { id: 'deepseek', label: 'DeepSeek' },
  { id: 'zai', label: 'z.ai' },
  { id: 'kimi', label: 'Kimi' },
  { id: 'qianwen', label: '千问' },
  { id: 'hunyuan', label: '混元' },
  { id: 'wenxin', label: '文心' },
  { id: 'doubao', label: '豆包' },
  { id: 'mimo', label: '小米 MiMo' },
] as const

export type AccountProvider = (typeof ACCOUNT_PROVIDERS)[number]['id']
export type AccountProviderFilter = 'all' | AccountProvider

export function getProviderLabel(provider: string): string {
  return ACCOUNT_PROVIDERS.find(({ id }) => id === provider)?.label ?? provider
}

const PROVIDER_ALIASES: Record<string, AccountProvider> = {
  deepseek: 'deepseek',
  'z.ai': 'zai',
  'z-ai': 'zai',
  zai: 'zai',
  kimi: 'kimi',
  moonshot: 'kimi',
  qianwen: 'qianwen',
  qwen: 'qianwen',
  tongyi: 'qianwen',
  hunyuan: 'hunyuan',
  tencent: 'hunyuan',
  wenxin: 'wenxin',
  ernie: 'wenxin',
  baidu: 'wenxin',
  doubao: 'doubao',
  bytedance: 'doubao',
  mimo: 'mimo',
  xiaomi: 'mimo',
}

const MODEL_PROVIDER_PATTERNS: Array<[RegExp, AccountProvider]> = [
  [/^(deepseek|ds)[-_]/i, 'deepseek'],
  [/^(glm|zai|z-ai|x-preview)/i, 'zai'],
  [/^(kimi|moonshot)/i, 'kimi'],
  [/^(qwen|qianwen|tongyi)/i, 'qianwen'],
  [/^(hunyuan|hy\d|tencent)/i, 'hunyuan'],
  [/^(wenxin|ernie|baidu)/i, 'wenxin'],
  [/^(doubao|seed)/i, 'doubao'],
  [/^(mimo|xiaomi)/i, 'mimo'],
]

export function normalizeProvider(provider?: string | null): AccountProvider | null {
  const normalized = provider?.trim().toLowerCase()
  if (!normalized) return null
  return PROVIDER_ALIASES[normalized] ?? null
}

export function resolveModelProvider({
  provider,
  ownedBy,
  modelId,
}: {
  provider?: string | null
  ownedBy?: string | null
  modelId?: string | null
}): AccountProvider | null {
  const explicit = normalizeProvider(provider) ?? normalizeProvider(ownedBy)
  if (explicit) return explicit

  const id = modelId?.trim() ?? ''
  return MODEL_PROVIDER_PATTERNS.find(([pattern]) => pattern.test(id))?.[1] ?? null
}

export const MIMO_REQUIRED_COOKIES = [
  'userId',
  'xiaomichatbot_serviceToken',
  'xiaomichatbot_ph',
] as const

export function parseMimoCookies(input: string) {
  const cookies = input.trim().replace(/^cookie\s*:\s*/i, '')
  const values = new Map<string, string>()
  for (const part of cookies.split(';')) {
    const separator = part.indexOf('=')
    if (separator < 0) continue
    const name = part.slice(0, separator).trim()
    const raw = part.slice(separator + 1).trim()
    const value = raw.startsWith('"') && raw.endsWith('"')
      ? raw.slice(1, -1).trim()
      : raw
    values.set(name, value)
  }
  return {
    // Preserve the original cookie values, including surrounding double quotes.
    cookies,
    userId: values.get('userId') ?? '',
    missing: MIMO_REQUIRED_COOKIES.filter((name) => !values.get(name)),
  }
}
