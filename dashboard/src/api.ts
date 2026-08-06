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
// "closed", not "archived". This union previously read `"active" | "archived"`
// and the backend has never emitted "archived" in its life -- app.models.
// conversation defines STATUS_ACTIVE and STATUS_CLOSED and nothing else. The
// mistake survived because Loose<> widens to string, so the wrong literal
// type-checked perfectly; it simply meant the one value the dashboard
// actually receives was the one it offered no autocomplete for.
export type ConversationStatus = Loose<"active" | "closed">
export type ConversationTag = Loose<"sales_lead">
export type MessageDirection = Loose<"inbound" | "outbound">
export type MessageStatus = Loose<
  "pending" | "sent" | "unconfirmed" | "failed"
>

// Which app the customer wrote from. Mirrors app/channels/constants.py, which
// is append-only: these strings are stored in the database, so renaming one
// here would stop it matching rows that already exist.
export type Channel = Loose<
  | "whatsapp"
  | "messenger"
  | "instagram_dm"
  | "facebook_comment"
  | "instagram_comment"
>

// Computed server-side from (status, mode, last_activity_at, closing_sent_at)
// and never stored. WAITING_IDLE means past the idle timeout and due to be
// closed by the next sweep -- not yet closed. CLOSING means a worker has
// already claimed the goodbye and the close is moments away.
export type SessionState = Loose<
  "ACTIVE_BOT" | "ACTIVE_HUMAN" | "WAITING_IDLE" | "CLOSING" | "CLOSED"
>

export const TAG_SALES_LEAD = "sales_lead"
export const MODE_HUMAN = "human"
export const STATUS_ACTIVE = "active"
export const STATUS_CLOSED = "closed"

export const CHANNEL_WHATSAPP = "whatsapp"
export const CHANNEL_MESSENGER = "messenger"
export const CHANNEL_INSTAGRAM_DM = "instagram_dm"
export const CHANNEL_FACEBOOK_COMMENT = "facebook_comment"
export const CHANNEL_INSTAGRAM_COMMENT = "instagram_comment"

// Canonical ordering: the private threads first, then the public comment
// channels. Used to keep the filter menu in a stable order regardless of
// which channels happen to appear on the current page.
export const ALL_CHANNELS: Channel[] = [
  CHANNEL_WHATSAPP,
  CHANNEL_MESSENGER,
  CHANNEL_INSTAGRAM_DM,
  CHANNEL_FACEBOOK_COMMENT,
  CHANNEL_INSTAGRAM_COMMENT,
]

// Returned as `code` in the 409 body when an operator acts on a conversation
// whose customer has already started a newer session. Distinct from an
// ordinary conflict because the remedy is specific: open the newer session.
export const CODE_SUPERSEDED = "conversation_superseded"

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
  // SESSIONS, not customers. Since sessions close themselves, one returning
  // customer contributes several of these; total_users is the customer count.
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
  // Cost per SESSION. Sessions are shorter and more numerous than they were
  // before the lifecycle shipped, so this figure fell without spend changing.
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
  // Visit count. Reads as 1 for a customer who has been in touch once, and
  // climbs each time they come back after a session has closed.
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
  // Sessions, not customers -- see Overview.total_conversations.
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

// One SESSION, not one customer. A customer who comes back after their
// session closed gets a new row with a new id and its own transcript; the
// two are never merged, and user_id is the only stable per-customer key.
export interface Conversation {
  id: number
  user_id: number
  // Which app the customer wrote from. Optional for the same reason the
  // lifecycle fields below are: during a rolling deploy an older backend is
  // briefly live and sends no such field. Treat undefined as "whatsapp" --
  // every conversation from before channels existed was one.
  channel?: Channel
  // status is lifecycle (active / closed); mode is ownership (bot / human).
  // They are independent: a conversation stays active the whole time a human
  // operator owns it.
  status: ConversationStatus
  mode: ConversationMode
  // Why the row is where it is. The repository sorts unclaimed sales leads to
  // the top, so without this the list reorders for an invisible reason.
  tag: ConversationTag | null
  assigned_operator: string | null
  handoff_at: string | null
  // --- Session lifecycle -------------------------------------------------
  // All optional: a dashboard built from this file still works against a
  // backend deployed before the lifecycle shipped, which matters during a
  // rolling deploy where both versions are briefly live.
  //
  // When the idle countdown last restarted. Both directions of traffic reset
  // it, so this is not "last customer message".
  last_activity_at?: string | null
  // Non-null once this session has greeted its customer. Survives a reopen,
  // which is exactly why nobody is greeted twice in one session.
  welcome_sent_at?: string | null
  // Non-null once a worker has CLAIMED the goodbye -- not necessarily once
  // one has been delivered.
  closing_sent_at?: string | null
  closed_at?: string | null
  session_state?: SessionState
  // The server's effective configuration, so a countdown does not have to be
  // hardcoded here and drift from CONVERSATION_IDLE_TIMEOUT_MINUTES.
  idle_timeout_minutes?: number
  // False when CONVERSATION_CLOSE_AFTER_IDLE is off, in which case
  // WAITING_IDLE is a resting state rather than a countdown and the UI must
  // not promise the session is about to close.
  close_after_idle?: boolean
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
  // This session's messages only. Earlier visits are separate conversations
  // with their own ids -- see conversationHistory to find them.
  messages: Message[]
}

// One of a customer's other visits, for the operator history panel. No
// transcript: the panel lists several and loading every message of each
// would be a large payload for a sidebar nobody has clicked yet.
//
// Carries no channel, deliberately: every summary belongs to the same
// customer as the conversation that opened the panel, and a customer cannot
// change channel -- identity is (channel, external_id). CustomerHistory holds
// it once instead of repeating it down the list.
export interface ConversationSummary {
  id: number
  status: ConversationStatus
  mode: ConversationMode
  tag: ConversationTag | null
  created_at: string
  updated_at: string
  closed_at: string | null
}

// GET /admin/conversations/{id}/history. Operator-facing only: none of this
// is fed to the model, which still sees the current session alone.
export interface CustomerHistory {
  user_id: number
  // A phone number, OR AN EMPTY STRING. The backend sends "" for anyone who
  // did not arrive on WhatsApp, and deliberately does not write a page-scoped
  // id here -- anything rendering this field as a phone number would render a
  // Messenger id as one. Prefer external_id when displaying.
  wa_id: string
  channel?: Channel
  // Their id on `channel`: the phone number on WhatsApp, a page-scoped id on
  // Messenger. The only identity field populated for every channel.
  external_id?: string | null
  name: string | null
  total_conversations: number
  previous: ConversationSummary[]
}

export interface ManualReply {
  message_id: number
  conversation_id: number
  wa_message_id: string | null
  sent_at: string
}

export interface MessageHit {
  message_id: number
  // The session the hit belongs to. The same customer can appear several
  // times in one result set with different conversation ids -- they said it
  // in different visits.
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
  // `status` is optional and omitted by default, which returns every session
  // regardless of lifecycle -- the behaviour this call has always had.
  //
  // There is deliberately no `channel` parameter: the router does not accept
  // one. The dashboard filters by channel client-side, which narrows the
  // loaded page rather than fetching a full page of matches.
  conversations: (
    limit = PAGE_SIZE,
    offset = 0,
    status?: ConversationStatus | null,
  ) => {
    const query = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })
    if (status) query.set("status", status)
    return request<Conversation[]>(`/admin/conversations?${query}`)
  },
  conversation: (id: number) =>
    request<ConversationDetail>(`/admin/conversations/${id}`),
  // The customer behind a session and their previous ones. Sessions are not
  // merged; this is navigation between them.
  conversationHistory: (id: number, limit = 20) =>
    request<CustomerHistory>(
      `/admin/conversations/${id}/history?limit=${limit}`,
    ),
  deleteConversation: (id: number) =>
    request<void>(`/admin/conversations/${id}`, { method: "DELETE" }),
  // Reopens the session first if it has closed, so the reply and the
  // customer's answer stay in the same conversation. Throws ApiError 409 when
  // the customer has already started a newer session.
  reply: (id: number, text: string) =>
    request<ManualReply>(`/admin/conversations/${id}/reply`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  // Stops the bot answering this conversation. The operator name is a label
  // for other operators, not a credential. Also reopens a closed session.
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
