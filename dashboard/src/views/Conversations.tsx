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
  const [error, setError] = useState<string | null>(null)

  useEvents((event) => {
    if (event.type !== "conversation.activity") return
    if (event.conversation_id !== id) return
    detail.reload()
  })

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
      setError(
        exception instanceof ApiError
          ? exception.message
          : String(exception),
      )
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>Conversation #{id}</h2>
        <Refreshing active={detail.refreshing} />
      </div>
      <Loader loading={detail.loading} error={detail.error}>
        {detail.data && (
          <>
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
                Free-form replies are only delivered within 24 hours of the
                customer's last message. Outside that window the API returns
                409 and the message is not sent.
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
    if (event.type !== "conversation.activity") return
    // Any activity can reorder the list or add a row to it.
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
