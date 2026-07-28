import { useState } from "react"

import { ApiError, api } from "../api"
import type { MessageHit } from "../api"
import { datetime } from "../format"

export default function Search() {
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
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="primary" disabled={loading}>
            {loading ? "Searching..." : "Search"}
          </button>
        </form>
        {error && <p className="error">{error}</p>}
      </div>

      {hits && (
        <div className="panel">
          <h2>{hits.length} result(s)</h2>
          {hits.length > 0 && (
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
                  <tr key={hit.message_id}>
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
          )}
        </div>
      )}
    </>
  )
}
