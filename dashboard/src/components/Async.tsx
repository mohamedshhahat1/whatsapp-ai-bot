import type { ReactNode } from "react"
import { useCallback, useEffect, useRef, useState } from "react"

import { ApiError } from "../api"

interface State<T> {
  data: T | null
  error: string | null
  // No data has ever arrived: the caller should render a placeholder.
  loading: boolean
  // A background refresh is in flight while data is already on screen.
  refreshing: boolean
}

// Small data-fetching hook. A dedicated query library would be overkill for a
// handful of read-only screens.
//
// Pass pollMs to auto refresh. The conversation screens now pass 0 while the
// WebSocket event stream is connected (see events.tsx) and fall back to an
// interval when it drops, so polling is the degradation path rather than the
// normal one. The analytics screens still poll: their figures are aggregates
// over days, so a per-message push would buy nothing.
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  pollMs = 0,
): State<T> & { reload: () => void } {
  const [state, setState] = useState<State<T>>({
    data: null,
    error: null,
    loading: true,
    refreshing: false,
  })

  // Guards against out-of-order responses: with polling or a burst of events,
  // a slow request can resolve after a newer one and would otherwise
  // overwrite fresh data.
  const requestId = useRef(0)

  const run = useCallback(() => {
    const id = ++requestId.current
    setState((previous) =>
      previous.data === null
        ? { ...previous, loading: true, error: null }
        : { ...previous, refreshing: true },
    )
    loader()
      .then((data) => {
        if (id !== requestId.current) return
        setState({ data, error: null, loading: false, refreshing: false })
      })
      .catch((error: unknown) => {
        if (id !== requestId.current) return
        const message =
          error instanceof ApiError
            ? `${error.status}: ${error.message}`
            : String(error)
        // Keep whatever is already on screen. A single failed poll should
        // surface an error, not wipe a working dashboard.
        setState((previous) => ({
          data: previous.data,
          error: message,
          loading: false,
          refreshing: false,
        }))
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps])

  useEffect(() => {
    run()
  }, [run])

  useEffect(() => {
    if (pollMs <= 0) return
    const timer = window.setInterval(run, pollMs)
    return () => window.clearInterval(timer)
  }, [run, pollMs])

  return { ...state, reload: run }
}

export function Loader({
  loading,
  error,
  children,
}: {
  loading: boolean
  error: string | null
  children: ReactNode
}) {
  if (loading) return <p className="muted">Loading...</p>
  if (error) return <p className="error">{error}</p>
  return <>{children}</>
}

// Empty states were being written inline as <p className="muted"> in every
// view; this keeps them consistent.
export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted">{children}</p>
}

// "Updating..." next to a page title, so an auto refresh is visible without
// the content jumping.
export function Refreshing({ active }: { active: boolean }) {
  if (!active) return null
  return (
    <span className="muted" style={{ fontSize: 12 }}>
      Updating...
    </span>
  )
}

export function Card({
  label,
  value,
  hint,
}: {
  label: string
  value: string
  hint?: string
}) {
  return (
    <div className="card">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}
