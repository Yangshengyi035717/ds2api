/**
 * TypeScript types matching the FastAPI admin + v1 endpoints.
 * Keep in sync with `admin.py` and `server.py`.
 */

export interface StatsResponse {
  total_requests: number
  success_requests: number
  failed_requests: number
  success_rate: number
  avg_latency_ms: number
  p50_latency_ms: number
  p95_latency_ms: number
  p99_latency_ms: number
  latency_window_size: number
  total_prompt_tokens: number
  total_completion_tokens: number
  uptime_secs: number
  models: Record<
    string,
    {
      requests: number
      prompt_tokens: number
      completion_tokens: number
      errors: number
    }
  >
}

export interface Account {
  id: string
  email: string
  source: string
  state: 'idle' | 'busy' | 'error' | string
  error_count: number
  last_error: string
  last_used: number
  created_at: number
  updated_at: number
  token_preview: string
  cookies_preview: string
  credential_fingerprint: string
  proxy_id: string
  proxy_label: string
  proxy_preview: string
  proxy_enabled: boolean
}

export interface AccountsResponse {
  accounts: Account[]
  total: number
  idle: number
  busy: number
  error: number
}

export interface HistoryPoint {
  /** Unix timestamp (seconds) of the snapshot. */
  t: number
  total: number
  success: number
  failed: number
  avg_latency_ms: number
}

export interface HistoryResponse {
  interval_secs: number
  points: HistoryPoint[]
}

export interface LoginRequest {
  password: string
}

export interface LoginResponse {
  token: string
}

export interface ReloginResponse {
  ok: boolean
  message: string
}

export interface ModelInfo {
  id: string
  object?: string
  created?: number
  owned_by?: string
}

export interface ModelsResponse {
  object?: string
  data: ModelInfo[]
}

export interface RuntimeConfigResponse {
  model_name: string
  source: 'webui' | 'env' | 'default' | string
  persisted: boolean
  default_model_name: string
  model_routes_configured: boolean
}

export interface ProxyConfig {
  id: string
  label: string
  url_preview: string
  enabled: boolean
  created_at: number
  updated_at: number
  last_check_at: number
  last_check_ok: boolean | null
  last_check_status: number | null
  last_check_latency_ms: number | null
  last_check_error: string
  bound_accounts: string[]
  bound_count: number
}

export interface ProxiesResponse {
  proxies: ProxyConfig[]
  total: number
  available: number
}

export interface ChatMessage {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string
  name?: string
  tool_call_id?: string
}

export interface PlaygroundRequest {
  model: string
  messages: ChatMessage[]
  temperature?: number
  max_tokens?: number
  stream?: boolean
  thinking_mode?: boolean
  search_enabled?: boolean
}

export interface ApiError {
  detail?: string
  error?: { type?: string; message?: string }
  message?: string
}
