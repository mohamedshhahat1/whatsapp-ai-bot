import { useState } from "react"

import { ApiError, api } from "../api"
import { Loader, useAsync } from "../components/Async"
import { datetime } from "../format"

function ConversationView({ id }: { id: number }) {
  const detail = useAsync(() => api.conversation(id), [id])
  const [text, setText] = useState("")
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function send() {
    if (!text.trim()) return
    setSending(true)
    setError(null)
    try {
      await api.reply(id, text.trim())
      setText("")
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
      <h2>Conversation #{id}</h2>
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
                <p className="muted">No messages.</p>
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
                customer's last message.
              </p>
            </div>
          </>
        )}
      </Loader>
    </div>
  )
}

export default function Conversations() {
  const conversations = useAsync(() => api.conversations(50), [])
  const [selected, setSelected] = useState<number | null>(null)

  return (
    <>
      <div className="page-header">
        <h1>Conversations</h1>
        <button onClick={conversations.reload}>Refresh</button>
      </div>

      <div className="panel">
        <Loader loading={conversations.loading} error={conversations.error}>
          {conversations.data && conversations.data.length === 0 && (
            <p className="muted">No conversations yet.</p>
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
                    className="clickable"
                    onClick={() => setSelected(conversation.id)}
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

      {selected !== null && <ConversationView id={selected} />}
    </>
  )
}
