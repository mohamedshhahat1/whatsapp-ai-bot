// Spend, abuse and the global AI switch.
//
// Every control on this screen already existed in the API and had no caller
// anywhere in the dashboard. That is the worst possible place for a circuit
// breaker to live: an operator who cannot see the breaker cannot know it has
// tripped, and an operator who cannot reach the kill switch has to redeploy to
// stop a misbehaving model.

import { useState } from "react"

import { ApiError, api } from "../api"
import { Card, Loader, Refreshing, useAsync } from "../components/Async"
import { money, number, percent } from "../format"

const POLL_MS = 30_000

function meterTone(fraction: number): string {
  if (fraction >= 0.9) return "meter-fill danger"
  if (fraction >= 0.7) return "meter-fill warn"
  return "meter-fill"
}

export default function Operations() {
  const quota = useAsync(() => api.quota(), [], POLL_MS)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [waId, setWaId] = useState("")

  function report(exception: unknown) {
    setNotice(null)
    setError(
      exception instanceof ApiError ? exception.message : String(exception),
    )
  }

  const data = quota.data
  const aiDisabled = data?.ai_disabled === true

  async function toggleAi() {
    const next = !aiDisabled
    if (
      next &&
      !window.confirm(
        "Stop automated replies for every customer?\n\nMessages will still " +
          "arrive and will still be stored and shown here, but nobody gets an " +
          "automatic answer until you switch it back on.",
      )
    ) {
      return
    }
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await api.setAiDisabled(next)
      setNotice(
        result.ai_disabled
          ? "Automated replies are now OFF for everyone."
          : "Automated replies are back ON.",
      )
      quota.reload()
    } catch (exception) {
      report(exception)
    } finally {
      setBusy(false)
    }
  }

  async function unblock() {
    const target = waId.trim()
    if (!target) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const result = await api.unblockCustomer(target)
      // Idempotent by design: not-blocked is information, not a failure.
      setNotice(
        result.was_blocked
          ? `${result.wa_id} is no longer blocked.`
          : `${result.wa_id} was not blocked to begin with.`,
      )
      setWaId("")
      quota.reload()
    } catch (exception) {
      report(exception)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="page-header">
        <h1>Operations</h1>
        <div className="row">
          <Refreshing active={quota.refreshing} />
          <button onClick={quota.reload}>Refresh</button>
        </div>
      </div>

      <Loader loading={quota.loading} error={quota.error}>
        {data && !data.available && (
          <div className="panel">
            <p className="error" style={{ marginTop: 0 }}>
              Redis is unreachable, so none of these figures can be read.
            </p>
            <p className="muted" style={{ fontSize: 13 }}>
              {data.error ??
                "No detail was returned by the server."}{" "}
              While this is the case the spend guard and the customer rate
              limiter are failing open: messages are being answered without
              being counted. This is deliberately not shown as zero spend.
            </p>
          </div>
        )}

        {data && data.available && (
          <>
            <div className="cards">
              <Card
                label="Spend today"
                value={money(data.spend_usd ?? 0)}
                hint={
                  data.spend_limit_usd
                    ? `ceiling ${money(data.spend_limit_usd)}`
                    : "no ceiling configured"
                }
              />
              <Card
                label="Tokens today"
                value={number(data.tokens ?? 0)}
                hint={
                  data.token_limit
                    ? `limit ${number(data.token_limit)}`
                    : "no limit configured"
                }
              />
              <Card
                label="Blocked customers"
                value={number(data.blocked_customers ?? 0)}
                hint="flood or spam heuristics"
              />
              <Card
                label="Automated replies"
                value={aiDisabled ? "OFF" : "ON"}
                hint={data.date ? `counters for ${data.date}` : undefined}
              />
            </div>

            {data.spend_used_fraction !== null && (
              <div className="panel">
                <h2>Daily spend</h2>
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <span className="muted" style={{ fontSize: 13 }}>
                    {percent(data.spend_used_fraction)} of today's ceiling used
                  </span>
                </div>
                <div className="meter">
                  <div
                    className={meterTone(data.spend_used_fraction)}
                    style={{
                      width: `${Math.min(100, data.spend_used_fraction * 100)}%`,
                    }}
                  />
                </div>
              </div>
            )}

            <div className="panel">
              <h2>Protections</h2>
              <div className="row">
                <span
                  className={
                    "badge " + (data.spend_guard_enabled ? "ok" : "danger")
                  }
                >
                  spend guard {data.spend_guard_enabled ? "on" : "off"}
                </span>
                <span
                  className={
                    "badge " +
                    (data.customer_rate_limit_enabled ? "ok" : "danger")
                  }
                >
                  customer rate limit{" "}
                  {data.customer_rate_limit_enabled ? "on" : "off"}
                </span>
              </div>
              {data.limits && (
                <p className="muted" style={{ fontSize: 13 }}>
                  Per customer: {number(data.limits.per_minute)}/min,{" "}
                  {number(data.limits.per_hour)}/hour,{" "}
                  {number(data.limits.per_day)}/day.
                </p>
              )}
            </div>
          </>
        )}
      </Loader>

      <div className="panel">
        <h2>Global AI switch</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Stops the assistant answering anyone. Customer messages are still
          received, stored and shown here, so nothing is lost - they simply
          wait for a person. The switch lives in Redis and survives a restart.
        </p>
        <button
          className={aiDisabled ? "primary" : "danger"}
          disabled={busy || quota.loading}
          onClick={toggleAi}
        >
          {busy
            ? "Working..."
            : aiDisabled
              ? "Turn automated replies back ON"
              : "Turn automated replies OFF"}
        </button>
      </div>

      <div className="panel">
        <h2>Unblock a customer</h2>
        <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
          Flood detection is a heuristic, and a customer sending six photos of
          a damaged ceiling looks a lot like a script. Lifting a block takes
          effect immediately.
        </p>
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault()
            unblock()
          }}
        >
          <input
            value={waId}
            placeholder="WhatsApp number, for example 201234567890"
            onChange={(event) => setWaId(event.target.value)}
          />
          <button className="primary" disabled={busy || !waId.trim()}>
            Unblock
          </button>
        </form>
      </div>

      {error && <p className="error">{error}</p>}
      {notice && <p className="muted">{notice}</p>}
    </>
  )
}
