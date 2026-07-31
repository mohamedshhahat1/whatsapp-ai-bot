// Transient notifications.
//
// The dashboard previously had no overlay or dialog component at all: the
// only interruptions available were window.prompt and window.confirm. A sales
// lead needs something that does not block the thread and does not require
// the operator to already be on the right screen.

import type { ReactNode } from "react"
import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react"

export type ToastTone = "info" | "lead" | "warn" | "error"

export interface ToastMessage {
  id: number
  title: string
  body?: string
  tone: ToastTone
  // Rendered as an action button when present.
  actionLabel?: string
  onAction?: () => void
}

interface ToastApi {
  push: (toast: Omit<ToastMessage, "id">) => void
}

const ToastContext = createContext<ToastApi>({ push: () => {} })

const DISMISS_MS = 12_000
// More than a handful on screen is noise, and the oldest are the least useful.
const MAX_VISIBLE = 4

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastMessage[]>([])
  const nextId = useRef(1)

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id))
  }, [])

  const push = useCallback(
    (toast: Omit<ToastMessage, "id">) => {
      const id = nextId.current
      nextId.current += 1
      setToasts((current) => [...current, { ...toast, id }].slice(-MAX_VISIBLE))
      window.setTimeout(() => dismiss(id), DISMISS_MS)
    },
    [dismiss],
  )

  // Memoised so every consumer does not re-render on each provider render.
  const value = useMemo(() => ({ push }), [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={"toast " + toast.tone}>
            <div
              className="row"
              style={{ justifyContent: "space-between", alignItems: "start" }}
            >
              <div>
                <div className="toast-title">{toast.title}</div>
                {toast.body && <div className="toast-body">{toast.body}</div>}
              </div>
              <button
                className="toast-close"
                aria-label="Dismiss notification"
                onClick={() => dismiss(toast.id)}
              >
                {"\u00d7"}
              </button>
            </div>
            {toast.actionLabel && (
              <button
                className="primary"
                style={{ marginTop: 10 }}
                onClick={() => {
                  toast.onAction?.()
                  dismiss(toast.id)
                }}
              >
                {toast.actionLabel}
              </button>
            )}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  return useContext(ToastContext)
}
