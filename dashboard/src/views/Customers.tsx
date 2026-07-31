import { useState } from "react"

import { ApiError, PAGE_SIZE, api } from "../api"
import { Empty, Loader, Refreshing, useAsync } from "../components/Async"
import { datetime, number } from "../format"

const POLL_MS = 60_000

export default function Customers() {
  const [offset, setOffset] = useState(0)
  const [filter, setFilter] = useState("")
  const [busy, setBusy] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const customers = useAsync(
    () => api.customers(PAGE_SIZE, offset),
    [offset],
    POLL_MS,
  )

  const rows = customers.data ?? []
  const term = filter.trim().toLowerCase()
  // Filters the page that is already loaded. There is no customer search
  // endpoint on the server, so this cannot reach rows on other pages and the
  // label below says exactly that rather than implying a global search.
  const visible = term
    ? rows.filter(
        (customer) =>
          (customer.name ?? "").toLowerCase().includes(term) ||
          customer.wa_id.toLowerCase().includes(term),
      )
    : rows
  const hasNext = rows.length === PAGE_SIZE

  async function unblock(waId: string) {
    setBusy(waId)
    setError(null)
    setNotice(null)
    try {
      const result = await api.unblockCustomer(waId)
      setNotice(
        result.was_blocked
          ? `${result.wa_id} is no longer blocked.`
          : `${result.wa_id} was not blocked.`,
      )
    } catch (exception) {
      setError(
        exception instanceof ApiError ? exception.message : String(exception),
      )
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <div className="page-header">
        <h1>Customers</h1>
        <div className="row">
          <Refreshing active={customers.refreshing} />
          <button onClick={customers.reload}>Refresh</button>
        </div>
      </div>

      <div className="panel">
        <input
          value={filter}
          placeholder="Filter by name or number..."
          aria-label="Filter customers"
          onChange={(event) => setFilter(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Escape") setFilter("")
          }}
        />
        <p className="muted" style={{ fontSize: 12, marginBottom: 0 }}>
          Filters the {rows.length} customers on this page only. Press Escape
          to clear.
        </p>
      </div>

      <div className="panel">
        <Loader loading={customers.loading} error={customers.error}>
          {rows.length === 0 && <Empty>No customers yet.</Empty>}
          {rows.length > 0 && visible.length === 0 && (
            <Empty>Nothing on this page matches that filter.</Empty>
          )}
          {visible.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>WhatsApp number</th>
                    <th>Conversations</th>
                    <th>Messages</th>
                    <th>Last active</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {visible.map((customer) => (
                    <tr key={customer.user_id}>
                      <td>
                        {customer.name || <span className="muted">-</span>}
                      </td>
                      <td>{customer.wa_id}</td>
                      <td>{number(customer.conversations)}</td>
                      <td>{number(customer.messages)}</td>
                      <td className="muted">{datetime(customer.last_active)}</td>
                      <td>
                        <button
                          disabled={busy === customer.wa_id}
                          onClick={() => unblock(customer.wa_id)}
                          title="Lift an abuse block, if one is in place"
                        >
                          {busy === customer.wa_id ? "Working..." : "Unblock"}
                        </button>
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
        {error && <p className="error">{error}</p>}
        {notice && <p className="muted">{notice}</p>}
      </div>
    </>
  )
}
