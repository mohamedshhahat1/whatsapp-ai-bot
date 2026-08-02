import { useState } from "react"

import {
  ApiError,
  MODE_HUMAN,
  PAGE_SIZE,
  STATUS_CLOSED,
  TAG_SALES_LEAD,
  api,
} from "../api"
import type {
  Conversation,
  ConversationStatus,
  Message,
  SessionState,
} from "../api"
import { Empty, Loader, Refreshing, useAsync } from "../components/Async"
import { useEvents, useEventsStatus } from "../events"
import { datetime } from "../format"

// Polling is now the fallback, not the mechanism: these intervals apply only
// while the event stream is down, so a Redis outage makes the dashboard slower
// rather than blind.
//
// Note the consequence for anything the stream does NOT report: there is no
// safety-net poll to catch it. A transition with no matching event in
// isRelevant below is invisible forever on a healthy system, which is exactly
// how closed sessions used to go unnoticed.
const DETAIL_FALLBACK_POLL_MS = 10_000
const LIST_FALLBACK_POLL_MS = 30_000

// Remembered so the question is asked once per browser. There are no operator
// accounts, so this is a label that keeps two operators from answering the same
// customer -- not an authenticated identity, and not a permission.
const OPERATOR_STORAGE = "waai_operator"

function currentOperator(): string {
  return localStorage.getItem(OPERATOR_STORAGE) ?? ""
}

function operatorName(): string | null {
  const stored = currentOperator()
  if (stored) return stored
  const entered =
    window.prompt("Your name (shown to other operators)")?.trim() ?? ""
  if (!entered) return null
  localStorage.setItem(OPERATOR_STORAGE, entered)
  return entered
}

// Every event that changes what these views show. Adding to this list is not
// optional when a new lifecycle event ships: because polling is disabled while
// the stream is connected, an event missing from here is not "slower to
// appear", it never appears at all.
//
// activity  - a message was added
// handoff   - ownership changed between bot and human
// closed    - the idle sweep ended a session
// reopened  - a customer came back, or an operator revived it
const RELEVANT_EVENTS = [
  "conversation.activity",
  "conversation.handoff",
  "conversation.closed",
  "conversation.reopened",
]

function isRelevant(type: string): boolean {
  return RELEVANT_EVENTS.includes(type)
}

function isUnclaimedLead(conversation: Conversation): boolean {
  return (
    conversation.tag === TAG_SALES_LEAD &&
    conversation.mode === MODE_HUMAN &&
    !conversation.assigned_operator
  )
}

function isClosed(conversation: Pick<Conversation, "status">): boolean {
  return conversation.status === STATUS_CLOSED
}

// Plain-language rendering of the computed server-side state. The raw values
// are shouty constants meant for machines, and "WAITING_IDLE" tells an
// operator nothing about what is about to happen to the conversation.
function sessionLabel(state: SessionState | undefined): string | null {
  switch (state) {
    case "ACTIVE_BOT":
      return null // The default and least interesting case; no badge.
    case "ACTIVE_HUMAN":
      return null // Already shown by the mode badge next to it.
    case "WAITING_IDLE":
      return "idle"
    case "CLOSING":
      return "closing"
    case "CLOSED":
      return "closed"
    default:
      return state ?? null
  }
}

interface Props {
  openId: number | null
  // Accepts null so the transcript pane can clear itself after a delete. App
  // holds this as number | null already.
  onOpen: (id: number | null) => void
  follow: boolean
  onFollowChange: (value: boolean) => void
  // Lets the shell refresh its unclaimed-lead counter when ownership changes
  // here, rather than waiting for the next poll.
  onChanged?: () => void
}

// A media message carries a media_id and usually no text at all, so rendering
// only `content` produced an empty bubble with no hint that anything was sent.
function MessageBubble({ message }: { message: Message }) {
  const hasMedia = message.media_id !== null
  const hasText = Boolean(message.content)

  return (
    <div className={"bubble " + message.direction}>
      {hasText && message.content}
      {hasMedia && (
        <span className="attachment">
          [{message.type}] attachment - not downloadable from this dashboard
        </span>
      )}
      {!hasText && !hasMedia && (
        <span className="attachment">[{message.type}]</span>
      )}
      <span className="meta">
        {datetime(message.created_at)}
        {message.status ? " - " + message.status : ""}
      </span>
    </div>
  )
}

// The customer behind this session, and their earlier ones.
//
// Sessions are deliberately not merged: the gaps between them are the point of
// the lifecycle, and stitching four visits into one transcript would
// misrepresent what happened. So the operator gets navigation instead -- and
// the count, which is the part that changes how you talk to someone. Their
// fifth visit about the same ceiling is a different conversation to their
// first.
//
// None of this reaches the model. Prompt context is still built from the
// current session alone.
function CustomerHistoryPanel({
  conversationId,
  onOpen,
}: {
  conversationId: number
  onOpen: (id: number) => void
}) {
  const history = useAsync(
    () => api.conversationHistory(conversationId),
    [conversationId],
  )

  if (history.error || !history.data) return null
  const { data } = history
  const label = data.name ? `${data.name} (${data.wa_id})` : data.wa_id

  return (
    <div style={{ marginTop: 4, marginBottom: 12 }}>
      <p className="muted" style={{ fontSize: 12, margin: 0 }}>
        {label} - {data.total_conversations}{" "}
        {data.total_conversations === 1 ? "conversation" : "conversations"} in
        total
      </p>
      {data.previous.length > 0 && (
        <details style={{ marginTop: 6 }}>
          <summary className="muted" style={{ fontSize: 12 }}>
            {data.previous.length} earlier{" "}
            {data.previous.length === 1 ? "session" : "sessions"}
          </summary>
          <div className="table-scroll" style={{ marginTop: 6 }}>
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Started</th>
                  <th>Ended</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {data.previous.map((previous) => (
                  <tr
                    key={previous.id}
                    className="clickable"
                    tabIndex={0}
                    role="button"
                    aria-label={`Open earlier conversation ${previous.id}`}
                    onClick={() => onOpen(previous.id)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") {
                        event.preventDefault()
                        onOpen(previous.id)
                      }
                    }}
                  >
                    <td>
                      #{previous.id}{" "}
                      {previous.tag === TAG_SALES_LEAD && (
                        <span className="badge lead">Lead</span>
                      )}
                    </td>
                    <td className="muted">{datetime(previous.created_at)}</td>
                    <td className="muted">
                      {previous.closed_at ? datetime(previous.closed_at) : "-"}
                    </td>
                    <td>
                      <span
                        className={
                          "badge" + (isClosed(previous) ? " closed" : "")
                        }
                      >
                        {previous.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  )
}

function ConversationView({
  id,
  onOpen,
  onChanged,
  onDeleted,
}: {
  id: number
  onOpen: (id: number) => void
  onChanged?: () => void
  onDeleted: () => void
}) {
  const { connected } = useEventsStatus()
  const detail = useAsync(
    () => api.conversation(id),
    [id],
    connected ? 0 : DETAIL_FALLBACK_POLL_MS,
  )
  const [text, setText] = useState("")
  const [sending, setSending] = useState(false)
  const [switching, setSwitching] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Set only once the server has actually refused an action with 409
  // conversation_superseded. Until then a closed conversation is still
  // writable -- the backend reopens it -- so pre-emptively disabling the
  // buttons would block the normal path.
  const [superseded, setSuperseded] = useState(false)

  useEvents((event) => {
    if (!isRelevant(event.type)) return
    if (event.conversation_id !== id) return
    detail.reload()
  })

  const humanOwned = detail.data?.mode === MODE_HUMAN
  const owner = detail.data?.assigned_operator ?? null
  const isLead = detail.data?.tag === TAG_SALES_LEAD
  const closed = detail.data ? isClosed(detail.data) : false
  const state = detail.data?.session_state
  // No operator identity exists server side, so "someone else has this" is a
  // name comparison. It is worth doing anyway: the common accident is two
  // people answering the same customer, not a malicious takeover.
  const ownedByOther = humanOwned && owner !== null && owner !== currentOperator()

  function report(exception: unknown) {
    if (exception instanceof ApiError) {
      // 409 from these endpoints means the customer has already started a
      // newer session, so this one can never be written to again. That is
      // permanent, unlike an ordinary failure, so the buttons go away.
      if (exception.status === 409 && /superseded|newer/i.test(exception.message)) {
        setSuperseded(true)
      }
      setError(exception.message)
      return
    }
    setError(String(exception))
  }

  function changed() {
    detail.reload()
    onChanged?.()
  }

  async function send() {
    if (!text.trim()) return
    setSending(true)
    setError(null)
    try {
      await api.reply(id, text.trim())
      setText("")
      // The server also publishes an event, but reloading here means the sent
      // message appears even if the stream is down.
      changed()
    } catch (exception) {
      report(exception)
    } finally {
      setSending(false)
    }
  }

  async function takeOver() {
    if (
      ownedByOther &&
      !window.confirm(
        `${owner} is currently answering this conversation. Take it over anyway?`,
      )
    ) {
      return
    }
    const operator = operatorName()
    if (!operator) return
    setSwitching(true)
    setError(null)
    try {
      await api.takeOver(id, operator)
      changed()
    } catch (exception) {
      report(exception)
    } finally {
      setSwitching(false)
    }
  }

  async function resumeAi() {
    setSwitching(true)
    setError(null)
    try {
      await api.resumeAi(id)
      changed()
    } catch (exception) {
      report(exception)
    } finally {
      setSwitching(false)
    }
  }

  async function remove() {
    if (
      !window.confirm(
        "Delete this conversation and every message in it?\n\nThe transcript " +
          "is removed permanently and cannot be recovered from this dashboard.",
      )
    ) {
      return
    }
    setDeleting(true)
    setError(null)
    try {
      await api.deleteConversation(id)
      onDeleted()
    } catch (exception) {
      report(exception)
      setDeleting(false)
    }
  }

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>
          Conversation #{id}{" "}
          {isLead && <span className="badge lead">Sales lead</span>}
        </h2>
        <div className="row">
          {detail.data && closed && <span className="badge closed">closed</span>}
          {detail.data && !closed && sessionLabel(state) && (
            <span className="badge">{sessionLabel(state)}</span>
          )}
          {detail.data && (
            <span className={"badge " + (humanOwned ? "human" : "bot")}>
              {humanOwned
                ? "human" + (owner ? " - " + owner : " - unassigned")
                : "bot"}
            </span>
          )}
          {detail.data && !superseded && (!humanOwned || ownedByOther) && (
            <button disabled={switching} onClick={takeOver}>
              {switching
                ? "Working..."
                : ownedByOther
                  ? "Take over from " + owner
                  : closed
                    ? "Reopen & take over"
                    : "Take Over"}
            </button>
          )}
          {detail.data && !superseded && humanOwned && (
            <button disabled={switching} onClick={resumeAi}>
              {switching
                ? "Working..."
                : closed
                  ? "Reopen & resume AI"
                  : "Resume AI"}
            </button>
          )}
          {detail.data && (
            <button className="danger" disabled={deleting} onClick={remove}>
              {deleting ? "Deleting..." : "Delete"}
            </button>
          )}
          <Refreshing active={detail.refreshing} />
        </div>
      </div>
      <Loader loading={detail.loading} error={detail.error}>
        {detail.data && (
          <>
            <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
              Started {datetime(detail.data.created_at)}
              {detail.data.handoff_at
                ? " - handed to a human " + datetime(detail.data.handoff_at)
                : ""}
              {detail.data.closed_at
                ? " - closed " + datetime(detail.data.closed_at)
                : ""}
            </p>
            <CustomerHistoryPanel conversationId={id} onOpen={onOpen} />
            {isLead && (
              <p className="warn" style={{ fontSize: 12 }}>
                This customer asked about price or started negotiating. The bot
                never quotes a figure - a person has to.
              </p>
            )}
            {superseded && (
              <p className="error" style={{ fontSize: 12 }}>
                This customer has already started a newer conversation, so this
                one can no longer be replied to or taken over. Anything sent
                here would be filed under a session they have moved on from.
                Open their current conversation instead - it is at the top of
                the list.
              </p>
            )}
            {closed && !superseded && (
              <p className="warn" style={{ fontSize: 12 }}>
                This session has ended. Replying or taking over will reopen it,
                keeping the whole transcript together, so the customer sees one
                continuous conversation rather than a message out of nowhere.
                The customer is not greeted again.
              </p>
            )}
            {state === "WAITING_IDLE" && detail.data.close_after_idle && (
              <p className="muted" style={{ fontSize: 12 }}>
                Quiet for longer than the{" "}
                {detail.data.idle_timeout_minutes ?? "configured"}-minute idle
                timeout. The next sweep will close it.
              </p>
            )}
            {state === "CLOSING" && (
              <p className="muted" style={{ fontSize: 12 }}>
                A closing message is being sent and this session is about to
                end.
              </p>
            )}
            {humanOwned && (
              <p className="muted" style={{ fontSize: 12 }}>
                The bot is not answering this conversation. Incoming messages
                are saved and shown here, but nothing is sent until you reply or
                press Resume AI.
              </p>
            )}
            <div className="chat">
              {detail.data.messages.map((message) => (
                <MessageBubble key={message.id} message={message} />
              ))}
              {detail.data.messages.length === 0 && (
                <Empty>No messages.</Empty>
              )}
            </div>

            <div style={{ marginTop: 16 }}>
              <textarea
                rows={3}
                value={text}
                disabled={superseded}
                placeholder={
                  superseded
                    ? "This conversation has been superseded by a newer one."
                    : "Reply as a human operator..."
                }
                onChange={(event) => setText(event.target.value)}
              />
              <div className="row" style={{ marginTop: 10 }}>
                <button
                  className="primary"
                  disabled={sending || superseded || !text.trim()}
                  onClick={send}
                >
                  {sending
                    ? "Sending..."
                    : closed
                      ? "Reopen & send reply"
                      : "Send reply"}
                </button>
                {error && <span className="error">{error}</span>}
              </div>
              <p className="muted" style={{ fontSize: 12 }}>
                Sending a reply does not stop the bot on its own - press Take
                Over for that. Free-form replies are only delivered within 24
                hours of the customer's last message. Outside that window the
                API returns 409 and the message is not sent.
              </p>
            </div>
          </>
        )}
      </Loader>
    </div>
  )
}

export default function Conversations({
  openId,
  onOpen,
  follow,
  onFollowChange,
  onChanged,
}: Props) {
  const { connected } = useEventsStatus()
  const [offset, setOffset] = useState(0)
  // null means every status, which is what this list has always shown.
  const [statusFilter, setStatusFilter] = useState<ConversationStatus | null>(
    null,
  )
  const conversations = useAsync(
    () => api.conversations(PAGE_SIZE, offset, statusFilter),
    [offset, statusFilter],
    connected ? 0 : LIST_FALLBACK_POLL_MS,
  )

  useEvents((event) => {
    if (!isRelevant(event.type)) return
    // Any activity can reorder the list or add a row to it, a handoff
    // elsewhere changes a badge here, and a close or reopen changes both the
    // status shown and whether the row belongs in the current filter.
    conversations.reload()
  })

  const rows = conversations.data ?? []
  // The endpoint returns a bare array with no total, so a full page is the
  // only evidence that another one exists.
  const hasNext = rows.length === PAGE_SIZE

  function changeFilter(value: string) {
    setOffset(0) // Page 3 of "all" is rarely page 3 of "active".
    setStatusFilter(value === "" ? null : (value as ConversationStatus))
  }

  return (
    <>
      <div className="page-header">
        <h1>Conversations</h1>
        <div className="row">
          <label className="muted" style={{ fontSize: 12 }}>
            <input
              type="checkbox"
              checked={follow}
              style={{ width: "auto" }}
              onChange={(event) => onFollowChange(event.target.checked)}
            />{" "}
            Open new customer messages automatically
          </label>
          <select
            aria-label="Filter by status"
            value={statusFilter ?? ""}
            style={{ width: "auto" }}
            onChange={(event) => changeFilter(event.target.value)}
          >
            <option value="">All sessions</option>
            <option value="active">Active only</option>
            <option value="closed">Closed only</option>
          </select>
          <Refreshing active={conversations.refreshing} />
          <button onClick={conversations.reload}>Refresh</button>
        </div>
      </div>

      <div className="panel">
        <Loader loading={conversations.loading} error={conversations.error}>
          {rows.length === 0 && (
            <Empty>
              {statusFilter
                ? `No ${statusFilter} conversations.`
                : "No conversations yet."}
            </Empty>
          )}
          {rows.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Customer</th>
                    <th>Status</th>
                    <th>Answered by</th>
                    <th>Last update</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((conversation) => (
                    <tr
                      key={conversation.id}
                      className={
                        "clickable" +
                        (conversation.id === openId ? " active" : "") +
                        (isUnclaimedLead(conversation) ? " lead-row" : "")
                      }
                      tabIndex={0}
                      role="button"
                      aria-label={`Open conversation ${conversation.id}`}
                      onClick={() => onOpen(conversation.id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault()
                          onOpen(conversation.id)
                        }
                      }}
                    >
                      <td>
                        #{conversation.id}{" "}
                        {conversation.tag === TAG_SALES_LEAD && (
                          <span className="badge lead">Lead</span>
                        )}
                      </td>
                      {/* One customer has many sessions, so this is not a
                          unique row identity -- the same user_id appearing
                          several times is correct, not a duplicate. */}
                      <td>user {conversation.user_id}</td>
                      <td>
                        <span
                          className={
                            "badge" + (isClosed(conversation) ? " closed" : "")
                          }
                        >
                          {conversation.status}
                        </span>
                        {!isClosed(conversation) &&
                          sessionLabel(conversation.session_state) && (
                            <span className="muted" style={{ fontSize: 12 }}>
                              {" "}
                              {sessionLabel(conversation.session_state)}
                            </span>
                          )}
                      </td>
                      <td>
                        <span
                          className={
                            "badge " +
                            (conversation.mode === MODE_HUMAN ? "human" : "bot")
                          }
                        >
                          {conversation.mode}
                        </span>
                        {conversation.mode === MODE_HUMAN && (
                          <span className="muted" style={{ fontSize: 12 }}>
                            {" "}
                            {conversation.assigned_operator ?? "unassigned"}
                          </span>
                        )}
                      </td>
                      <td className="muted">
                        {datetime(conversation.updated_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="pager">
            <button
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </button>
            <span className="muted" style={{ fontSize: 12 }}>
              {offset + 1} - {offset + rows.length}
            </span>
            <button
              disabled={!hasNext}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </button>
          </div>
        </Loader>
      </div>

      {openId !== null && (
        <ConversationView
          id={openId}
          onOpen={onOpen}
          onChanged={onChanged}
          onDeleted={() => {
            onOpen(null)
            conversations.reload()
            onChanged?.()
          }}
        />
      )}
    </>
  )
}
