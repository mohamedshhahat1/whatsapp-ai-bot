import { useState } from "react"

import { ApiError, api } from "../api"
import type { KnowledgeHit } from "../api"
import { Loader, useAsync } from "../components/Async"
import { datetime, number } from "../format"

export default function Knowledge() {
  const documents = useAsync(() => api.knowledge(), [])
  const [query, setQuery] = useState("")
  const [hits, setHits] = useState<KnowledgeHit[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function test() {
    if (query.trim().length < 2) return
    setError(null)
    try {
      setHits(await api.knowledgeSearch(query.trim()))
    } catch (exception) {
      setError(
        exception instanceof ApiError ? exception.message : String(exception),
      )
    }
  }

  return (
    <>
      <div className="page-header">
        <h1>Knowledge base</h1>
        <button onClick={documents.reload}>Refresh</button>
      </div>

      <div className="panel">
        <h2>Indexed documents</h2>
        <Loader loading={documents.loading} error={documents.error}>
          {documents.data && documents.data.length === 0 && (
            <p className="muted">
              Nothing indexed yet. Drop PDFs into knowledge/ and run
              scripts/ingest_knowledge.py.
            </p>
          )}
          {documents.data && documents.data.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Title</th>
                    <th>Source</th>
                    <th>Chunks</th>
                    <th>Updated</th>
                  </tr>
                </thead>
                <tbody>
                  {documents.data.map((document) => (
                    <tr key={document.id}>
                      <td>{document.title}</td>
                      <td className="muted">{document.source}</td>
                      <td>{number(document.chunk_count)}</td>
                      <td className="muted">{datetime(document.updated_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Loader>
      </div>

      <div className="panel">
        <h2>Test retrieval</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Shows the exact chunks the model would receive for a question.
        </p>
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault()
            test()
          }}
        >
          <input
            value={query}
            aria-label="Question to test retrieval with"
            placeholder="e.g. what surface preparation is done before painting"
            onChange={(event) => setQuery(event.target.value)}
          />
          <button className="primary">Test</button>
        </form>
        <p className="muted" style={{ fontSize: 12 }}>
          Pricing questions are deliberately not answerable: the assistant
          never quotes a figure and escalates to a person instead.
        </p>
        {error && <p className="error">{error}</p>}
        {hits &&
          hits.map((hit, index) => (
            <div key={index} className="card" style={{ marginTop: 12 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <strong>{hit.source}</strong>
                <span className="badge">score {hit.score.toFixed(3)}</span>
              </div>
              <p style={{ fontSize: 13, whiteSpace: "pre-wrap" }}>
                {hit.content}
              </p>
            </div>
          ))}
        {hits && hits.length === 0 && (
          <p className="muted">
            No chunk cleared the similarity threshold for that question.
          </p>
        )}
      </div>
    </>
  )
}
