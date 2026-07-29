import { useState } from "react"

import { ApiError, api } from "../api"
import { Empty, Loader, Refreshing, useAsync } from "../components/Async"
import { useEvents, useEventsStatus } from "../events"
import { datetime } from "../format"

// Polling is now the fallback, not the mechanism: these intervals apply only
// while the event stream is down, so a Redis outage makes the dashboard slower
// rather than blind.
const DETAIL_FALLBACK_POLL_MS = 10_000
const LIST_FALLBACK_POLL_MS = 30_000

const MODE_HUMAN = "human"

// Remembered so the question is asked once per browser. There are no operator
// accounts, so this is a label that keeps two operators from answering the same
// customer -- not an authenticated identity, and not a permission.
const OPERATOR_STORAGE = "waai_operator"

function operatorName(): string | null {
  const stored = localStorage.getItem(OPERATOR_STORAGE) ?? ""
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

interface Props {
  openId: number | null
  onOpen: (id: number) => void
  follow: boolean
  onFollowChange: (value: boolean) => void
}

function ConversationView({ id }: { id: number }) {
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

  function report(exception: unknown) {
    setError(
      exception instanceof ApiError ? exception.message : String(exception),
    )
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
      detail.reload()
    } catch (exception) {
      report(exception)
    } finally {
      setSending(false)
    }
  }

  async function takeOver() {
    const operator = operatorName()
    if (!operator) return
    setSwitching(true)
    setError(null)
    try {
      await api.takeOver(id, operator)
      detail.reload()
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
      detail.reload()
    } catch (exception) {
      report(exception)
    } finally {
      setSwitching(false)
    }
  }

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>Conversation #{id}</h2>
        <div className="row">
          {detail.data && (
            <span className="badge">
              {humanOwned
                ? "human" +
                  (detail.data.assigned_operator
                    ? " - " + detail.data.assigned_operator
                    : " - unassigned")
                : "bot"}
            </span>
          )}
          {detail.data && !humanOwned && (
            <button disabled={switching} onClick={takeOver}>
              {switching ? "Working..." : "Take Over"}
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
            {humanOwned && (
              <p className="muted" style={{ fontSize: 12 }}>
                The bot is not answering this conversation. Incoming messages
                are saved and shown here, but nothing is sent until you reply or
                press Resume AI.
              </p>
            )}
            <div className="chat">
              {detail.data.messages.map((message) => (
                <div
                  key={message.id}
                  className={"bubble " + message.direction}
                >
                  {message.content}
                  <span className="meta">
                    {datetime(message.created_at)}
                    {message.status ? " - " + message.status : ""}
                  </span>
                </div>
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
}: Props) {
  const { connected } = useEventsStatus()
  const conversations = useAsync(
    () => api.conversations(50),
    [],
    connected ? 0 : LIST_FALLBACK_POLL_MS,
  )

  useEvents((event) => {
    if (!isRelevant(event.type)) return
    // Any activity can reorder the list or add a row to it, and a handoff
    // elsewhere changes a badge here.
    conversations.reload()
  })

  return (
    <>
      <div className="page-header">
        <h1>Conversations</h1>
        <div className="row">
          <label className="muted" style={{ fontSize: 12 }}>
            <input
              type="checkbox"
              checked={follow}
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
          {conversations.data && conversations.data.length === 0 && (
            <Empty>No conversations yet.</Empty>
          )}
          {conversations.data && conversations.data.length > 0 && (
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
                {conversations.data.map((conversation) => (
                  <tr
                    key={conversation.id}
                    className={
                      "clickable" + (conversation.id === openId ? " active" : "")
                    }
                    onClick={() => onOpen(conversation.id)}
                  >
                    <td>#{conversation.id}</td>
                    <td>user {conversation.user_id}</td>
                    <td>
                      <span className="badge">{conversation.status}</span>
                    </td>
                    <td>
                      <span className="badge">{conversation.mode}</span>
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
          )}
        </Loader>
      </div>

      {openId !== null && <ConversationView id={openId} />}
    </>
  )
}
