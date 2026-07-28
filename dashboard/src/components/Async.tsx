import type { ReactNode } from "react"
import { useCallback, useEffect, useState } from "react"

import { ApiError } from "../api"

interface State<T> {
  data: T | null
  error: string | null
  loading: boolean
}

// Small data-fetching hook. A dedicated query library would be overkill for a
// handful of read-only screens.
export function useAsync<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
): State<T> & { reload: () => void } {
  const [state, setState] = useState<State<T>>({
    data: null,
    error: null,
    loading: true,
  })

  const run = useCallback(() => {
    let cancelled = false
    setState((previous) => ({ ...previous, loading: true, error: null }))
    loader()
      .then((data) => {
        if (!cancelled) setState({ data, error: null, loading: false })
      })
      .catch((error: unknown) => {
        if (cancelled) return
        const message =
          error instanceof ApiError
            ? `${error.status}: ${error.message}`
            : String(error)
        setState({ data: null, error: message, loading: false })
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)

  useEffect(run, [run])

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
