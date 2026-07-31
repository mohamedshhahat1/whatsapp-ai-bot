// Thin typed wrapper over the /admin API.
//
// The admin key is held in sessionStorage rather than localStorage so it dies
// with the tab. This is still a browser-held credential: see docs/DASHBOARD.md
// for why that is acceptable for a single-operator tool and what to do when it
// stops being acceptable.

const KEY_STORAGE = "waai_admin_key"

// Fired when the server rejects the key. App listens for this and drops back
// to the sign-in screen. Without it the dashboard would sit on a permanent
// error string holding a credential it already knew was dead.
export const UNAUTHORIZED_EVENT = "waai:unauthorized"

// Nothing in this app had a timeout, so a hung connection hung the view with
// no way out but a manual reload.
const DEFAULT_TIMEOUT_MS = 15_000

// Reserved for failures that never reached the server, so callers can tell a
// dead network apart from a real HTTP status.
export const STATUS_NO_RESPONSE = 0

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

async function request<T>(
  path: string,
  init: RequestInit = {},
  timeoutMs = DEFAULT_TIMEOUT_MS,
): Promise<T> {
  // Built through the Headers constructor rather than by spreading
  // init.headers into an object literal: HeadersInit is a union of Headers,
  // string[][] and Record<string, string>, and spreading the first two
  // produces object types that are no longer assignable to HeadersInit.
  const headers = new Headers(init.headers)
  headers.set("Content-Type", "application/json")
  headers.set("X-API-Key", getApiKey())

  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)

  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      headers,
      signal: controller.signal,
    })
  } catch (exception) {
    // An aborted fetch and a refused connection both land here, and they mean
    // very different things to an operator.
    if (exception instanceof DOMException && exception.name === "AbortError") {
      throw new ApiError(
        STATUS_NO_RESPONSE,
        `The server did not respond within ${Math.round(timeoutMs / 1000)}s.`,
      )
    }
    throw new ApiError(
      STATUS_NO_RESPONSE,
      "Could not reach the server. It may be restarting.",
    )
  } finally {
    window.clearTimeout(timer)
  }

  if (!response.ok) {
    let detail = response.statusText
    try {
      const body = await response.json()
      if (body && typeof body.detail === "string") detail = body.detail
    } catch {
      // Non-JSON error body; the status text is the best we have.
    }
    if (response.status === 401) {
      // require_admin raises 401 for a bad or missing key and never 403, so
      // this is unambiguous: the credential is wrong. Drop it rather than let
      // every subsequent poll retry with it.
      clearApiKey()
      window.dispatchEvent(new Event(UNAUTHORIZED_EVENT))
    }
    throw new ApiError(response.status, detail)
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

// The backend types all of these as bare strings. Loose<T> keeps that
// tolerance -- an unknown value from a newer server still renders instead of
// breaking the build -- while giving autocomplete and catching typos.
//
// Record<never, never> rather than the more familiar `{}`: they are identical
// to the type checker, but `{}` trips @typescript-eslint/no-empty-object-type,
// which is an error in the recommended set this project lints with.
type Loose<T extends string> = T | (string & Record<never, never>)

export type ConversationMode = Loose<"bot" | "human">
export type ConversationStatus = Loose<"active" | "archived">
export type ConversationTag = Loose<"sales_lead">
export type MessageDirection = Loose<"inbound" | "outbound">
export type MessageStatus = Loose<
  "pending" | "sent" | "unconfirmed" | "failed"
>

export const TAG_SALES_LEAD = "sales_lead"
export const MODE_HUMAN = "human"

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

// GET /admin/users. Distinct from Customer, which is an activity aggregate.
export interface User {
  id: number
  wa_id: string
  name: string | null
  created_at: string
}

// GET /admin/stats. Lifetime counters, cheaper than the analytics overview.
export interface Stats {
  total_users: number
  total_conversations: number
  total_messages: number
  messages_last_24h: number
  total_tokens_used: number
}

export interface QuotaLimits {
  per_minute: number
  per_hour: number
  per_day: number
}

// GET /admin/quota. Every field below `available` is null when Redis is
// unreachable -- the server deliberately refuses to report zeros, because
// zeros render as a reassuring empty chart at the exact moment the spend
// guard has failed open.
export interface QuotaStats {
  available: boolean
  error: string | null
  date: string | null
  spend_usd: number | null
  spend_limit_usd: number | null
  spend_used_fraction: number | null
  tokens: number | null
  token_limit: number | null
  ai_disabled: boolean | null
  spend_guard_enabled: boolean | null
  customer_rate_limit_enabled: boolean | null
  blocked_customers: number | null
  limits: QuotaLimits | null
}

export interface AiToggleResponse {
  ai_disabled: boolean
}

export interface UnblockResponse {
  wa_id: string
  was_blocked: boolean
}

export interface Conversation {
  id: number
  user_id: number
  // status is lifecycle (active / archived); mode is ownership (bot / human).
  // They are independent: a conversation stays active the whole time a human
  // operator owns it.
  status: ConversationStatus
  mode: ConversationMode
  // Why the row is where it is. The repository sorts unclaimed sales leads to
  // the top, so without this the list reorders for an invisible reason.
  tag: ConversationTag | null
  assigned_operator: string | null
  handoff_at: string | null
  created_at: string
  updated_at: string
}

export interface Message {
  id: number
  wa_message_id: string | null
  direction: MessageDirection
  type: string
  // Null for media: the caption lives here only when the customer sent one.
  content: string | null
  media_id: string | null
  status: MessageStatus | null
  created_at: string
}

export interface ConversationDetail extends Conversation {
  messages: Message[]
}

export interface ManualReply {
  message_id: number
  conversation_id: number
  wa_message_id: string | null
  sent_at: string
}

export interface MessageHit {
  message_id: number
  conversation_id: number
  user_id: number
  wa_id: string
  name: string | null
  direction: MessageDirection
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

// The list endpoints return bare arrays with no total count, so a page is
// "full" exactly when it came back at the limit. Callers use that to decide
// whether a Next button is live.
export const PAGE_SIZE = 50

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
  customers: (limit = PAGE_SIZE, offset = 0) =>
    request<Customer[]>(
      `/admin/analytics/customers?limit=${limit}&offset=${offset}`,
    ),
  users: (limit = PAGE_SIZE, offset = 0) =>
    request<User[]>(`/admin/users?limit=${limit}&offset=${offset}`),
  stats: () => request<Stats>("/admin/stats"),
  pricing: () => request<ModelPricing[]>("/admin/pricing"),
  addPricing: (payload: NewModelPricing) =>
    request<ModelPricing>("/admin/pricing", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deletePricing: (id: number) =>
    request<void>(`/admin/pricing/${id}`, { method: "DELETE" }),
  conversations: (limit = PAGE_SIZE, offset = 0) =>
    request<Conversation[]>(
      `/admin/conversations?limit=${limit}&offset=${offset}`,
    ),
  conversation: (id: number) =>
    request<ConversationDetail>(`/admin/conversations/${id}`),
  deleteConversation: (id: number) =>
    request<void>(`/admin/conversations/${id}`, { method: "DELETE" }),
  reply: (id: number, text: string) =>
    request<ManualReply>(`/admin/conversations/${id}/reply`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  // Stops the bot answering this conversation. The operator name is a label
  // for other operators, not a credential.
  takeOver: (id: number, operator: string) =>
    request<Conversation>(`/admin/conversations/${id}/takeover`, {
      method: "POST",
      body: JSON.stringify({ operator: operator || null }),
    }),
  resumeAi: (id: number) =>
    request<Conversation>(`/admin/conversations/${id}/resume-ai`, {
      method: "POST",
    }),
  // /admin/search accepts only q and limit -- there is no offset on the
  // router, so this list is capped rather than paged.
  search: (q: string, limit = PAGE_SIZE) =>
    request<MessageHit[]>(
      `/admin/search?q=${encodeURIComponent(q)}&limit=${limit}`,
    ),
  knowledge: () => request<KnowledgeDocument[]>("/admin/knowledge"),
  knowledgeSearch: (q: string) =>
    request<KnowledgeHit[]>(
      `/admin/knowledge/search?q=${encodeURIComponent(q)}`,
    ),
  quota: () => request<QuotaStats>("/admin/quota"),
  setAiDisabled: (disabled: boolean) =>
    request<AiToggleResponse>("/admin/ai-toggle", {
      method: "POST",
      body: JSON.stringify({ disabled }),
    }),
  unblockCustomer: (waId: string) =>
    request<UnblockResponse>(
      `/admin/customers/${encodeURIComponent(waId)}/unblock`,
      { method: "POST" },
    ),
}
