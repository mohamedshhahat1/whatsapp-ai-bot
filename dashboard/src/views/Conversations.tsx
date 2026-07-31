import { useState } from "react"

import {
  ApiError,
  MODE_HUMAN,
  PAGE_SIZE,
  TAG_SALES_LEAD,
  api,
} from "../api"
import type { Conversation, Message } from "../api"
import { Empty, Loader, Refreshing, useAsync } from "../components/Async"
import { useEvents, useEventsStatus } from "../events"
import { datetime } from "../format"

// Polling is now the fallback, not the mechanism: these intervals apply only
// while the event stream is down, so a Redis outage makes the dashboard slower
// rather than blind.
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

// Both event kinds change what these views show: one adds messages, the other
// changes who owns the conversation.
function isRelevant(type: string): boolean {
  return type === "conversation.activity" || type === "conversation.handoff"
}

function isUnclaimedLead(conversation: Conversation): boolean {
  return (
    conversation.tag === TAG_SALES_LEAD &&
    conversation.mode === MODE_HUMAN &&
    !conversation.assigned_operator
  )
}

interface Props {
  openId: number | null
  onOpen: (id: number) => void
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

function ConversationView({
  id,
  onChanged,
}: {
  id: number
  onChanged?: () => void
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
  const [error, setError] = useState<string | null>(null)

  useEvents((event) => {
    if (!isRelevant(event.type)) return
    if (event.conversation_id !== id) return
    detail.reload()
  })

  const humanOwned = detail.data?.mode === MODE_HUMAN
  const owner = detail.data?.assigned_operator ?? null
  const isLead = detail.data?.tag === TAG_SALES_LEAD
  // No operator identity exists server side, so "someone else has this" is a
  // name comparison. It is worth doing anyway: the common accident is two
  // people answering the same customer, not a malicious takeover.
  const ownedByOther = humanOwned && owner !== null && owner !== currentOperator()

  function report(exception: unknown) {
    setError(
      exception instanceof ApiError ? exception.message : String(exception),
    )
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

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>
          Conversation #{id}{" "}
          {isLead && <span className="badge lead">Sales lead</span>}
        </h2>
        <div className="row">
          {detail.data && (
            <span className={"badge " + (humanOwned ? "human" : "bot")}>
              {humanOwned
                ? "human" + (owner ? " - " + owner : " - unassigned")
                : "bot"}
            </span>
          )}
          {detail.data && (!humanOwned || ownedByOther) && (
            <button disabled={switching} onClick={takeOver}>
              {switching
                ? "Working..."
                : ownedByOther
                  ? "Take over from " + owner
                  : "Take Over"}
            </button>
          )}
          {detail.data && humanOwned && (
            <button disabled={switching} onClick={resumeAi}>
              {switching ? "Working..." : "Resume AI"}
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
            </p>
            {isLead && (
              <p className="warn" style={{ fontSize: 12 }}>
                This customer asked about price or started negotiating. The bot
                never quotes a figure - a person has to.
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
                placeholder="Reply as a human operator..."
                onChange={(event) => setText(event.target.value)}
              />
              <div className="row" style={{ marginTop: 10 }}>
                <button
                  className="primary"
                  disabled={sending || !text.trim()}
                  onClick={send}
                >
                  {sending ? "Sending..." : "Send reply"}
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
  const conversations = useAsync(
    () => api.conversations(PAGE_SIZE, offset),
    [offset],
    connected ? 0 : LIST_FALLBACK_POLL_MS,
  )

  useEvents((event) => {
    if (!isRelevant(event.type)) return
    // Any activity can reorder the list or add a row to it, and a handoff
    // elsewhere changes a badge here.
    conversations.reload()
  })

  const rows = conversations.data ?? []
  // The endpoint returns a bare array with no total, so a full page is the
  // only evidence that another one exists.
  const hasNext = rows.length === PAGE_SIZE

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
          <Refreshing active={conversations.refreshing} />
          <button onClick={conversations.reload}>Refresh</button>
        </div>
      </div>

      <div className="panel">
        <Loader loading={conversations.loading} error={conversations.error}>
          {rows.length === 0 && <Empty>No conversations yet.</Empty>}
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
                      <td>user {conversation.user_id}</td>
                      <td>
                        <span className="badge">{conversation.status}</span>
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
        <ConversationView id={openId} onChanged={onChanged} />
      )}
    </>
  )
}
