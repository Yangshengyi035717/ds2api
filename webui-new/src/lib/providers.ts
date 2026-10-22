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
