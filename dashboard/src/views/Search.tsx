import { useState } from "react"

import { ApiError, PAGE_SIZE, api } from "../api"
import type { MessageHit } from "../api"
import { datetime } from "../format"

interface Props {
  // Every hit carries conversation_id and clicking one used to do nothing,
  // which made search a read-only dead end.
  onOpenConversation: (id: number) => void
}

export default function Search({ onOpenConversation }: Props) {
  const [query, setQuery] = useState("")
  const [hits, setHits] = useState<MessageHit[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run() {
    if (query.trim().length < 2) return
    setLoading(true)
    setError(null)
    try {
      setHits(await api.search(query.trim()))
    } catch (exception) {
      setError(
        exception instanceof ApiError ? exception.message : String(exception),
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <div className="page-header">
        <h1>Search</h1>
      </div>

      <div className="panel">
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault()
            run()
          }}
        >
          <input
            value={query}
            placeholder="Search all customer and bot messages..."
            aria-label="Search messages"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                setQuery("")
                setHits(null)
              }
            }}
          />
          <button className="primary" disabled={loading}>
            {loading ? "Searching..." : "Search"}
          </button>
        </form>
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          At least two characters. Press Escape to clear. Results are capped at
          the newest {PAGE_SIZE} matches - the search endpoint does not page.
        </p>
        {error && <p className="error">{error}</p>}
      </div>

      {hits && (
        <div className="panel">
          <h2>{hits.length} result(s)</h2>
          {hits.length === 0 && (
            <p className="muted">No message matched that text.</p>
          )}
          {hits.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Customer</th>
                    <th>Direction</th>
                    <th>Message</th>
                  </tr>
                </thead>
                <tbody>
                  {hits.map((hit) => (
                    <tr
                      key={hit.message_id}
                      className="clickable"
                      tabIndex={0}
                      role="button"
                      aria-label={`Open conversation ${hit.conversation_id}`}
                      onClick={() => onOpenConversation(hit.conversation_id)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault()
                          onOpenConversation(hit.conversation_id)
                        }
                      }}
                    >
                      <td className="muted">{datetime(hit.created_at)}</td>
                      <td>{hit.name || hit.wa_id}</td>
                      <td>
                        <span className="badge">{hit.direction}</span>
                      </td>
                      <td>{hit.content}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </>
  )
}
