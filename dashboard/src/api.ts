// Thin typed wrapper over the /admin API.
//
// The admin key is held in sessionStorage rather than localStorage so it dies
// with the tab. This is still a browser-held credential: see docs/DASHBOARD.md
// for why that is acceptable for a single-operator tool and what to do when it
// stops being acceptable.

const KEY_STORAGE = "waai_admin_key"

export function getApiKey(): string {
  return sessionStorage.getItem(KEY_STORAGE) ?? ""
}

export function setApiKey(key: string): void {
  sessionStorage.setItem(KEY_STORAGE, key)
}

export function clearApiKey(): void {
  sessionStorage.removeItem(KEY_STORAGE)
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": getApiKey(),
      ...(init.headers ?? {}),
    },
  })

  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      if (body && typeof body.detail === "string") detail = body.detail
    } catch {
      // Non-JSON error body; the status text is the best we have.
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export interface CostBreakdown {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  input_cost_usd: number
  output_cost_usd: number
  total_cost_usd: number
}

export interface Overview {
  period_days: number
  since: string
  // Lifetime.
  total_users: number
  total_conversations: number
  total_messages: number
  // Scoped to period_days. Cost figures are window-scoped too, so these are
  // the only correct denominators to combine them with.
  new_users: number
  new_conversations: number
  active_conversations: number
  messages_in_period: number
  ai_requests: number
  ai_errors: number
  error_rate: number
  avg_latency_ms: number
  p95_latency_ms: number
  cost: CostBreakdown
  cost_per_conversation_usd: number
  projected_monthly_cost_usd: number
}

export interface DailyUsage {
  day: string
  requests: number
  messages: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  avg_latency_ms: number
  cost_usd: number
}

export interface TopQuestion {
  question: string
  count: number
  last_asked: string
}

export interface ModelCost {
  model: string
  requests: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cost_usd: number
}

export interface ModelPricing {
  id: number
  model: string
  input_price_per_1m: number
  output_price_per_1m: number
  effective_from: string
  note: string | null
  created_at: string
}

export interface NewModelPricing {
  model: string
  input_price_per_1m: number
  output_price_per_1m: number
  effective_from?: string
  note?: string
}

export interface Customer {
  user_id: number
  wa_id: string
  name: string | null
  conversations: number
  messages: number
  last_active: string | null
}

export interface Conversation {
  id: number
  user_id: number
  status: string
  created_at: string
  updated_at: string
}

export interface Message {
  id: number
  direction: string
  type: string
  content: string | null
  status: string | null
  created_at: string
}

export interface ConversationDetail extends Conversation {
  messages: Message[]
}

export interface MessageHit {
  message_id: number
  conversation_id: number
  user_id: number
  wa_id: string
  name: string | null
  direction: string
  content: string
  created_at: string
}

export interface KnowledgeDocument {
  id: number
  source: string
  title: string
  chunk_count: number
  updated_at: string
}

export interface KnowledgeHit {
  source: string
  score: number
  content: string
}

export const api = {
  overview: (days: number) =>
    request<Overview>(`/admin/analytics/overview?days=${days}`),
  daily: (days: number) =>
    request<DailyUsage[]>(`/admin/analytics/daily?days=${days}`),
  models: (days: number) =>
    request<ModelCost[]>(`/admin/analytics/models?days=${days}`),
  questions: (days: number, limit = 10) =>
    request<TopQuestion[]>(
      `/admin/analytics/questions?days=${days}&limit=${limit}`,
    ),
  customers: (limit = 100) =>
    request<Customer[]>(`/admin/analytics/customers?limit=${limit}`),
  pricing: () => request<ModelPricing[]>("/admin/pricing"),
  addPricing: (payload: NewModelPricing) =>
    request<ModelPricing>("/admin/pricing", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deletePricing: (id: number) =>
    request<void>(`/admin/pricing/${id}`, { method: "DELETE" }),
  conversations: (limit = 50) =>
    request<Conversation[]>(`/admin/conversations?limit=${limit}`),
  conversation: (id: number) =>
    request<ConversationDetail>(`/admin/conversations/${id}`),
  reply: (id: number, text: string) =>
    request<{ message_id: number }>(`/admin/conversations/${id}/reply`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  search: (q: string) =>
    request<MessageHit[]>(`/admin/search?q=${encodeURIComponent(q)}`),
  knowledge: () => request<KnowledgeDocument[]>("/admin/knowledge"),
  knowledgeSearch: (q: string) =>
    request<KnowledgeHit[]>(
      `/admin/knowledge/search?q=${encodeURIComponent(q)}`,
    ),
}
